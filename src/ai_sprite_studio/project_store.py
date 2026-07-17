"""Atomic, contained filesystem storage for sprite projects.

The store requires POSIX descriptor-relative operations and keeps directory
descriptors open while it walks a workspace.  That makes a rename or a symlink
swap of a pathname harmless for the operation already in progress.  The
returned :class:`~pathlib.Path` from ``get_artifact`` necessarily has a
post-return race; callers that need a stable file handle should read it before
handing control to an untrusted local writer.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
from typing import Any, Iterable, Iterator
from uuid import UUID, uuid4

from pydantic import ValidationError

from .contracts import (
    Approval,
    ApprovalGate,
    ArtifactRef,
    ArtifactVariant,
    JobRecord,
    ProjectConfig,
)


_UNSUPPORTED_FSYNC_ERRNOS = {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
_DIRFD_STORAGE_AVAILABLE = (
    os.name == "posix"
    and all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))
    and all(
        operation in os.supports_dir_fd
        for operation in (os.open, os.mkdir, os.stat, os.unlink, os.rmdir, os.rename, os.link)
    )
    and os.link in os.supports_follow_symlinks
)


class ProjectStoreError(ValueError):
    """A project file or requested storage operation is unsafe or invalid."""


class ProjectStoreConflictError(ProjectStoreError):
    """A guarded mutation observed a newer project manifest."""


class _WriteFailure(ProjectStoreError):
    """A write failure annotated with whether its destination was published."""

    def __init__(self, message: str, *, published: bool, collision: bool = False):
        super().__init__(message)
        self.published = published
        self.collision = collision


class ProjectStore:
    def __init__(self, workspace: str | Path):
        if not _DIRFD_STORAGE_AVAILABLE:
            raise ProjectStoreError("secure descriptor-relative storage is unavailable on this platform")
        self.workspace = Path(workspace).expanduser().resolve()
        self.projects = self.workspace / "projects"

    def create(self, project: ProjectConfig | None = None) -> ProjectConfig:
        project = self._validate_project(project or ProjectConfig())
        if project.artifacts or project.approvals:
            raise ProjectStoreError("new projects cannot include externally supplied artifacts or approvals")
        return self._create_at(project)

    def load(self, project_id: UUID | str) -> ProjectConfig:
        project_id = self._uuid(project_id, "project ID")
        with self._project_fd(project_id) as project_fd:
            return self._load_at(project_fd, project_id)

    def load_with_manifest_revision(
        self, project_id: UUID | str
    ) -> tuple[ProjectConfig, str]:
        """Load a validated project and digest its exact manifest bytes."""

        project_id = self._uuid(project_id, "project ID")
        with self._project_fd(project_id) as project_fd:
            return self._load_with_manifest_revision_at(project_fd, project_id)

    def save(self, project: ProjectConfig) -> ProjectConfig:
        project = self._validate_project(project)
        with self._project_fd(project.id) as project_fd:
            with self._project_mutation_lock(project_fd):
                current = self._load_at(project_fd, project.id)
                if project.artifacts != current.artifacts or project.approvals != current.approvals:
                    raise ProjectStoreError("artifacts and approvals are managed by ProjectStore")
                saved = project.model_copy(
                    update={"created_at": current.created_at, "updated_at": self._now()}
                )
                self._write_project_at(project_fd, saved)
                return saved

    def put_artifact(
        self,
        project_id: UUID | str,
        data: bytes,
        *,
        kind: str,
        media_type: str,
        variant: ArtifactVariant,
        stage: str | None = None,
        width: int | None = None,
        height: int | None = None,
        source_job_id: UUID | str | None = None,
        dependencies: Iterable[UUID | str] = (),
    ) -> ArtifactRef:
        if not isinstance(data, bytes):
            raise ProjectStoreError("artifact data must be bytes")
        if not isinstance(media_type, str) or not media_type.strip():
            raise ProjectStoreError("media type must be a non-empty string")
        project_id = self._uuid(project_id, "project ID")
        dependency_ids = [self._uuid(item, "dependency ID") for item in dependencies]
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ProjectStoreError("duplicate dependency ID")
        return self._put_artifact_at(
            project_id,
            data,
            kind=kind,
            media_type=media_type,
            variant=variant,
            stage=stage,
            width=width,
            height=height,
            source_job_id=source_job_id,
            dependency_ids=dependency_ids,
        )

    def get_artifact(self, project_id: UUID | str, artifact_id: UUID | str) -> Path:
        project_id = self._uuid(project_id, "project ID")
        artifact_id = self._uuid(artifact_id, "artifact ID")
        with self._project_fd(project_id) as project_fd:
            project = self._load_at(project_fd, project_id)
            artifact = next((item for item in project.artifacts if item.id == artifact_id), None)
            if artifact is None:
                raise ProjectStoreError("unknown artifact")
            # Verify this exact entry through the pinned directory before
            # returning the compatibility Path object.
            self._read_artifact_at(project_fd, artifact, digest=False)
            return self.projects.joinpath(str(project_id), *self._relative_parts(artifact.relative_path))

    def run_dir(self, project_id: UUID | str) -> Path:
        """Return the validated upstream-owned run directory for one project.

        ``sprite-gen`` requires a real path, so this has the same post-return
        pathname race as :meth:`get_artifact`; callers must only use fixed,
        engine-owned names inside it.
        """

        project_id = self._uuid(project_id, "project ID")
        with self._project_fd(project_id) as project_fd:
            self._load_at(project_fd, project_id)
            with self._opened_directory_at(project_fd, "run", label="run"):
                pass
        return self.projects / str(project_id) / "run"

    def read_artifact_bytes(self, project_id: UUID | str, artifact_id: UUID | str) -> bytes:
        """Read one stored artifact through its pinned descriptor and verify its digest."""

        project_id = self._uuid(project_id, "project ID")
        artifact_id = self._uuid(artifact_id, "artifact ID")
        with self._project_fd(project_id) as project_fd:
            project = self._load_at(project_fd, project_id)
            artifact = next((item for item in project.artifacts if item.id == artifact_id), None)
            if artifact is None:
                raise ProjectStoreError("unknown artifact")
            return self._read_artifact_bytes_at(project_fd, artifact)

    def verify_artifact(self, project_id: UUID | str, artifact_id: UUID | str) -> ArtifactRef:
        """Rehash one stored artifact through its pinned project descriptor."""

        project_id = self._uuid(project_id, "project ID")
        artifact_id = self._uuid(artifact_id, "artifact ID")
        with self._project_fd(project_id) as project_fd:
            project = self._load_at(project_fd, project_id)
            artifact = next((item for item in project.artifacts if item.id == artifact_id), None)
            if artifact is None:
                raise ProjectStoreError("unknown artifact")
            self._read_artifact_at(project_fd, artifact, digest=True)
            return artifact

    def approve(
        self,
        project_id: UUID | str,
        gate: ApprovalGate | Approval,
        artifact_ids: Iterable[UUID | str] | None = None,
        *,
        note: str = "",
        expected_manifest_revision: str | None = None,
    ) -> Approval:
        project_id = self._uuid(project_id, "project ID")
        with self._project_fd(project_id) as project_fd:
            with self._project_mutation_lock(project_fd):
                project, revision = self._load_with_manifest_revision_at(
                    project_fd, project_id
                )
                self._require_manifest_revision(expected_manifest_revision, revision)
                approval = self._approval_for(gate, artifact_ids, note)
                artifacts = {artifact.id: artifact for artifact in project.artifacts}
                current_hashes = self._approval_hashes_at(project_fd, approval, artifacts)
                if isinstance(gate, Approval):
                    if approval.artifact_hashes != current_hashes:
                        raise ProjectStoreError("approval hashes do not match current artifacts")
                else:
                    approval = approval.model_copy(update={"artifact_hashes": current_hashes})
                updated = project.model_copy(
                    update={
                        "approvals": [item for item in project.approvals if item.gate != approval.gate]
                        + [approval],
                        "updated_at": self._now(),
                    }
                )
                self._write_project_at(project_fd, updated)
                return approval

    def replace_approval_and_invalidate_dependants(
        self,
        project_id: UUID | str,
        gate: ApprovalGate,
        artifact_ids: Iterable[UUID | str],
        *,
        previous_artifact_ids: Iterable[UUID | str],
        note: str = "",
        expected_manifest_revision: str | None = None,
    ) -> Approval:
        """Replace one approval and stale dependants of removed roots in one manifest write."""

        project_id = self._uuid(project_id, "project ID")
        expected_ids = [self._uuid(item, "previous artifact ID") for item in previous_artifact_ids]
        if not expected_ids or len(expected_ids) != len(set(expected_ids)):
            raise ProjectStoreError("previous approval artifacts must be unique and non-empty")
        if not isinstance(note, str):
            raise ProjectStoreError("approval note must be text")
        with self._project_fd(project_id) as project_fd:
            with self._project_mutation_lock(project_fd):
                project, revision = self._load_with_manifest_revision_at(
                    project_fd, project_id
                )
                self._require_manifest_revision(expected_manifest_revision, revision)
                previous = next((item for item in project.approvals if item.gate == gate), None)
                if previous is None:
                    raise ProjectStoreError("approval to replace does not exist")
                if set(previous.artifact_ids) != set(expected_ids):
                    raise ProjectStoreError("approval changed before replacement")

                approval = self._approval_for(gate, artifact_ids, note)
                removed_ids = [
                    artifact_id
                    for artifact_id in previous.artifact_ids
                    if artifact_id not in approval.artifact_ids
                ]
                artifacts = {artifact.id: artifact for artifact in project.artifacts}
                hashes = self._approval_hashes_at(project_fd, approval, artifacts)
                approval = approval.model_copy(update={"artifact_hashes": hashes})

                invalidated, stale_project = self._invalidate(project, removed_ids)
                if set(invalidated).intersection(approval.artifact_ids):
                    raise ProjectStoreError("replacement approval depends on a removed artifact")
                updated_project = stale_project or project
                updated = updated_project.model_copy(
                    update={
                        "approvals": [
                            item for item in updated_project.approvals if item.gate != approval.gate
                        ]
                        + [approval],
                        "updated_at": self._now(),
                    }
                )
                self._write_project_at(project_fd, updated)
                return approval

    def invalidate_dependants(
        self, project_id: UUID | str, artifact_ids: Iterable[UUID | str]
    ) -> list[UUID]:
        project_id = self._uuid(project_id, "project ID")
        with self._project_fd(project_id) as project_fd:
            with self._project_mutation_lock(project_fd):
                project = self._load_at(project_fd, project_id)
                ordered, updated = self._invalidate(project, artifact_ids)
                if updated is not None:
                    self._write_project_at(project_fd, updated)
                return ordered

    def list_projects(self) -> list[ProjectConfig]:
        try:
            with self._projects_fd(create=False) as projects_fd:
                projects: list[ProjectConfig] = []
                for name in os.listdir(projects_fd):
                    try:
                        project_id = UUID(name)
                    except ValueError:
                        continue
                    with self._opened_directory_at(
                        projects_fd, name, label="project"
                    ) as project_fd:
                        # A crash can leave the UUID directory behind before
                        # its manifest is first published.  Once a manifest
                        # exists, keep surfacing every malformed project.
                        if not self._entry_exists_at(project_fd, "project.json"):
                            continue
                        projects.append(self._load_at(project_fd, project_id))
        except FileNotFoundError:
            return []
        except ProjectStoreError as exc:
            if str(exc) == "workspace projects directory is missing":
                return []
            raise
        return sorted(projects, key=lambda project: str(project.id))

    def acquire_runner_lock(self) -> int:
        with self._workspace_fd(create=True) as workspace_fd:
            try:
                descriptor = os.open(
                    ".runner.lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=workspace_fd,
                )
            except OSError as exc:
                raise ProjectStoreError("could not lock workspace") from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ProjectStoreError("could not lock workspace")
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise ProjectStoreError("workspace is already in use") from exc
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor

    @staticmethod
    def release_runner_lock(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def save_job(self, job: JobRecord) -> JobRecord:
        try:
            job = JobRecord.model_validate(job.model_dump(mode="python"))
        except (AttributeError, ValidationError) as exc:
            raise ProjectStoreError("invalid job") from exc
        with self._project_fd(job.project_id) as project_fd:
            self._load_at(project_fd, job.project_id)
            with self._opened_directory_at(project_fd, "jobs", label="jobs") as jobs_fd:
                self._atomic_write_at(jobs_fd, f"{job.id}.json", self._job_json(job), overwrite=True)
        return job

    def load_job(self, project_id: UUID | str, job_id: UUID | str) -> JobRecord:
        project_id = self._uuid(project_id, "project ID")
        job_id = self._uuid(job_id, "job ID")
        with self._project_fd(project_id) as project_fd:
            with self._opened_directory_at(project_fd, "jobs", label="jobs") as jobs_fd:
                return self._load_job_at(jobs_fd, project_id, job_id)

    def save_prompt(
        self, project_id: UUID | str, job_id: UUID | str, record: dict[str, Any]
    ) -> dict[str, Any]:
        """Atomically save one immutable JSON prompt record for a job."""

        project_id = self._uuid(project_id, "project ID")
        job_id = self._uuid(job_id, "job ID")
        encoded = self._prompt_json(record)
        with self._project_fd(project_id) as project_fd:
            self._load_at(project_fd, project_id)
            with self._opened_directory_at(project_fd, "prompts", label="prompts") as prompts_fd:
                self._atomic_write_at(prompts_fd, f"{job_id}.json", encoded, overwrite=False)
        return json.loads(encoded)

    def load_prompt(self, project_id: UUID | str, job_id: UUID | str) -> dict[str, Any]:
        """Load one validated immutable JSON prompt record for a job."""

        project_id = self._uuid(project_id, "project ID")
        job_id = self._uuid(job_id, "job ID")
        with self._project_fd(project_id) as project_fd:
            with self._opened_directory_at(project_fd, "prompts", label="prompts") as prompts_fd:
                try:
                    record = json.loads(self._read_file_at(prompts_fd, f"{job_id}.json"))
                except (ProjectStoreError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProjectStoreError("malformed prompt JSON") from exc
        self._prompt_json(record)
        return record

    def list_jobs(self, project_id: UUID | str) -> list[JobRecord]:
        project_id = self._uuid(project_id, "project ID")
        with self._project_fd(project_id) as project_fd:
            with self._opened_directory_at(project_fd, "jobs", label="jobs") as jobs_fd:
                jobs: list[JobRecord] = []
                for name in os.listdir(jobs_fd):
                    if not name.endswith(".json"):
                        continue
                    try:
                        job_id = UUID(name.removesuffix(".json"))
                    except ValueError:
                        continue
                    jobs.append(self._load_job_at(jobs_fd, project_id, job_id))
        return sorted(jobs, key=lambda job: str(job.id))

    def append_job_event(
        self, project_id: UUID | str, event: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        project_id = self._uuid(project_id, "project ID")
        if not isinstance(event, str) or not event.strip() or "\n" in event:
            raise ProjectStoreError("invalid job event")
        if not isinstance(data, dict):
            raise ProjectStoreError("invalid job event")
        with self._project_fd(project_id) as project_fd:
            events = self._job_events_at(project_fd)
            record = {"id": len(events) + 1, "event": event, "data": data}
            try:
                # ponytail: This rewrites O(n²) total event bytes over a job's
                # lifetime; use a database or segmented log only if event
                # volume makes that added machinery worthwhile.
                encoded = b"".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    ).encode("utf-8")
                    + b"\n"
                    for item in [*events, record]
                )
            except (TypeError, ValueError) as exc:
                raise ProjectStoreError("invalid job event") from exc
            self._atomic_write_at(project_fd, "events.ndjson", encoded, overwrite=True)
        return record

    def job_events(self, project_id: UUID | str, *, after_id: int = 0) -> list[dict[str, Any]]:
        project_id = self._uuid(project_id, "project ID")
        if not isinstance(after_id, int) or isinstance(after_id, bool) or after_id < 0:
            raise ProjectStoreError("invalid event ID")
        with self._project_fd(project_id) as project_fd:
            return [event for event in self._job_events_at(project_fd) if event["id"] > after_id]

    # POSIX descriptor-relative implementation ---------------------------------

    @classmethod
    def _directory_flags(cls) -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    @contextmanager
    def _workspace_fd(self, *, create: bool) -> Iterator[int]:
        descriptor = self._open_workspace_fd(create=create)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def _open_workspace_fd(self, *, create: bool) -> int:
        path = self.workspace
        if not path.is_absolute():
            path = path.absolute()
        descriptor = os.open(path.anchor or "/", self._directory_flags())
        try:
            for part in path.parts:
                if part == path.anchor:
                    continue
                child = self._open_directory_at(
                    descriptor, part, create=create, label="workspace"
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def _projects_fd(self, *, create: bool) -> Iterator[int]:
        with self._workspace_fd(create=create) as workspace_fd:
            try:
                descriptor = self._open_directory_at(
                    workspace_fd, "projects", create=create, label="projects"
                )
            except FileNotFoundError as exc:
                raise ProjectStoreError("workspace projects directory is missing") from exc
            except ProjectStoreError as exc:
                raise ProjectStoreError("workspace projects path resolves outside workspace") from exc
            try:
                yield descriptor
            finally:
                os.close(descriptor)

    @contextmanager
    def _project_fd(self, project_id: UUID) -> Iterator[int]:
        with self._projects_fd(create=False) as projects_fd:
            try:
                descriptor = self._open_directory_at(
                    projects_fd, str(project_id), create=False, label="project"
                )
            except FileNotFoundError as exc:
                raise ProjectStoreError("unknown project") from exc
            try:
                yield descriptor
            finally:
                os.close(descriptor)

    @contextmanager
    def _project_mutation_lock(self, project_fd: int) -> Iterator[None]:
        descriptor = -1
        locked = False
        try:
            try:
                descriptor = os.open(
                    ".project.lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=project_fd,
                )
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ProjectStoreError("could not lock project mutation")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
            except OSError as exc:
                raise ProjectStoreError("could not lock project mutation") from exc
            yield
        finally:
            if descriptor >= 0:
                try:
                    if locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    @classmethod
    def _open_directory_at(
        cls, parent_fd: int, name: str, *, create: bool, label: str
    ) -> int:
        try:
            descriptor = os.open(name, cls._directory_flags(), dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
        except OSError as exc:
            raise ProjectStoreError(f"unsafe {label} directory: {exc}") from exc
        else:
            if create:
                try:
                    cls._fsync_directory_fd(parent_fd)
                except BaseException:
                    os.close(descriptor)
                    raise
            return descriptor

        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProjectStoreError(f"could not create {label} directory: {exc}") from exc
        # Persist a new entry, or repair durability after a failed prior
        # attempt that left the child in place, before opening that child.
        cls._fsync_directory_fd(parent_fd)
        try:
            return os.open(name, cls._directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise ProjectStoreError(f"unsafe {label} directory: {exc}") from exc

    @contextmanager
    def _parent_fd_at(
        self, project_fd: int, relative_path: str, *, create: bool
    ) -> Iterator[tuple[int, str]]:
        parts = self._relative_parts(relative_path)
        descriptor = os.dup(project_fd)
        try:
            for part in parts[:-1]:
                child = self._open_directory_at(
                    descriptor, part, create=create, label="project"
                )
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    @classmethod
    def _entry_exists_at(cls, parent_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ProjectStoreError(f"could not inspect stored path: {exc}") from exc
        return True

    @classmethod
    @contextmanager
    def _regular_file_fd_at(cls, parent_fd: int, name: str) -> Iterator[int]:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            raise ProjectStoreError("stored path resolves outside the project or is missing") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ProjectStoreError("stored artifact is not a file")
            yield descriptor
        finally:
            os.close(descriptor)

    @classmethod
    def _read_file_at(cls, parent_fd: int, name: str) -> bytes:
        with cls._regular_file_fd_at(parent_fd, name) as descriptor:
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
        return b"".join(chunks)

    def _load_at(self, project_fd: int, project_id: UUID) -> ProjectConfig:
        return self._load_with_manifest_revision_at(project_fd, project_id)[0]

    def _load_with_manifest_revision_at(
        self, project_fd: int, project_id: UUID
    ) -> tuple[ProjectConfig, str]:
        try:
            raw_bytes = self._read_file_at(project_fd, "project.json")
            raw = json.loads(raw_bytes)
        except (ProjectStoreError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectStoreError("malformed project JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ProjectStoreError("unsupported project schema version")
        try:
            project = ProjectConfig.model_validate(raw)
        except ValidationError as exc:
            raise ProjectStoreError(f"invalid project JSON: {exc}") from exc
        if project.id != project_id:
            raise ProjectStoreError("project ID does not match its directory")
        return project, hashlib.sha256(raw_bytes).hexdigest()

    @staticmethod
    def _require_manifest_revision(expected: str | None, current: str) -> None:
        if expected is not None and expected != current:
            raise ProjectStoreConflictError("project manifest changed before approval")

    @classmethod
    def _load_job_at(cls, jobs_fd: int, project_id: UUID, job_id: UUID) -> JobRecord:
        try:
            raw = json.loads(cls._read_file_at(jobs_fd, f"{job_id}.json"))
        except (ProjectStoreError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectStoreError("malformed job JSON") from exc
        try:
            job = JobRecord.model_validate(raw)
        except ValidationError as exc:
            raise ProjectStoreError("invalid job JSON") from exc
        if job.id != job_id or job.project_id != project_id:
            raise ProjectStoreError("job does not match its stored project")
        return job

    @classmethod
    def _job_events_at(cls, project_fd: int) -> list[dict[str, Any]]:
        try:
            raw = cls._read_file_at(project_fd, "events.ndjson")
        except ProjectStoreError as exc:
            raise ProjectStoreError("malformed job events") from exc
        if not raw:
            return []
        events: list[dict[str, Any]] = []
        for expected_id, line in enumerate(raw.splitlines(), start=1):
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProjectStoreError("malformed job events") from exc
            if (
                not isinstance(event, dict)
                or event.get("id") != expected_id
                or isinstance(event.get("id"), bool)
                or not isinstance(event.get("event"), str)
                or not event["event"].strip()
                or not isinstance(event.get("data"), dict)
            ):
                raise ProjectStoreError("malformed job events")
            events.append(event)
        return events

    @classmethod
    def _temporary_name(cls, target_name: str) -> str:
        return f".{target_name}.{secrets.token_hex(16)}"

    @classmethod
    def _atomic_write_at(
        cls, parent_fd: int, target_name: str, data: bytes, *, overwrite: bool
    ) -> None:
        temporary_name: str | None = None
        descriptor = -1
        published = False
        try:
            for _ in range(16):
                candidate = cls._temporary_name(target_name)
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise _WriteFailure(
                        f"could not create temporary storage file: {exc}", published=False
                    ) from exc
                temporary_name = candidate
                break
            else:
                raise _WriteFailure("could not allocate temporary storage file", published=False)

            try:
                with os.fdopen(descriptor, "wb") as file:
                    descriptor = -1
                    file.write(data)
                    file.flush()
                    cls._fsync(file.fileno())
            except ProjectStoreError as exc:
                raise _WriteFailure(str(exc), published=False) from exc
            except OSError as exc:
                raise _WriteFailure(f"could not write storage file: {exc}", published=False) from exc

            try:
                if overwrite:
                    os.replace(
                        temporary_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    temporary_name = None
                    published = True
                else:
                    # link(2) is the immutable publication point: it fails if
                    # another writer wins the race, unlike replace(2).
                    os.link(
                        temporary_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    published = True
                    os.unlink(temporary_name, dir_fd=parent_fd)
                    temporary_name = None
            except FileExistsError as exc:
                raise _WriteFailure(
                    "refusing to overwrite an immutable candidate",
                    published=False,
                    collision=not overwrite,
                ) from exc
            except OSError as exc:
                action = "replace" if overwrite else "publish"
                raise _WriteFailure(
                    f"could not {action} storage file: {exc}", published=published
                ) from exc

            try:
                cls._fsync_directory_fd(parent_fd)
            except ProjectStoreError as exc:
                # A successful rename/link is visible even if its durability
                # barrier fails, so callers must treat it as published.
                raise _WriteFailure(str(exc), published=published) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    # Do not hide the write error; a later operation can clean
                    # an abandoned private temporary file.
                    pass

    @classmethod
    def _remove_entry_at(cls, parent_fd: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ProjectStoreError(f"could not roll back artifact: {exc}") from exc
        cls._fsync_directory_fd(parent_fd)

    @classmethod
    def _fsync_directory_fd(cls, descriptor: int) -> None:
        cls._fsync(descriptor)

    def _create_at(self, project: ProjectConfig) -> ProjectConfig:
        with self._projects_fd(create=True) as projects_fd:
            target_name = str(project.id)
            if self._entry_exists_at(projects_fd, target_name):
                raise ProjectStoreError("project already exists")
            try:
                # mkdir(2) is the no-replace publication point for project
                # directories.  Python has no portable rename-no-replace API.
                os.mkdir(target_name, 0o700, dir_fd=projects_fd)
            except FileExistsError as exc:
                raise ProjectStoreError("project already exists") from exc
            except OSError as exc:
                raise ProjectStoreError(f"could not create project: {exc}") from exc

            manifest_published = False
            try:
                with self._opened_directory_at(projects_fd, target_name, label="project") as project_fd:
                    for directory in (
                        "inputs",
                        "candidates",
                        "run",
                        "videos",
                        "prompts",
                        "jobs",
                        "exports",
                    ):
                        os.mkdir(directory, 0o755, dir_fd=project_fd)
                    self._atomic_write_at(project_fd, "events.ndjson", b"", overwrite=False)
                    try:
                        self._atomic_write_at(
                            project_fd, "project.json", self._project_json(project), overwrite=False
                        )
                    except _WriteFailure as exc:
                        manifest_published = exc.published
                        raise
                    manifest_published = True
                self._fsync_directory_fd(projects_fd)
            except _WriteFailure as exc:
                raise ProjectStoreError(str(exc)) from exc
            except OSError as exc:
                raise ProjectStoreError(f"could not create project: {exc}") from exc
            finally:
                if not manifest_published:
                    self._remove_tree_at(projects_fd, target_name)
        return project

    @contextmanager
    def _opened_directory_at(self, parent_fd: int, name: str, *, label: str) -> Iterator[int]:
        descriptor = self._open_directory_at(parent_fd, name, create=False, label=label)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    @classmethod
    def _remove_tree_at(cls, parent_fd: int, name: str) -> None:
        try:
            descriptor = cls._open_directory_at(parent_fd, name, create=False, label="project")
        except (FileNotFoundError, ProjectStoreError):
            return
        try:
            for child in os.listdir(descriptor):
                try:
                    mode = os.stat(child, dir_fd=descriptor, follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):
                        cls._remove_tree_at(descriptor, child)
                    else:
                        os.unlink(child, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
        finally:
            os.close(descriptor)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass

    def _put_artifact_at(
        self,
        project_id: UUID,
        data: bytes,
        *,
        kind: str,
        media_type: str,
        variant: ArtifactVariant,
        stage: str | None,
        width: int | None,
        height: int | None,
        source_job_id: UUID | str | None,
        dependency_ids: list[UUID],
    ) -> ArtifactRef:
        with self._project_fd(project_id) as project_fd:
            with self._project_mutation_lock(project_fd):
                project = self._load_at(project_fd, project_id)
                artifacts = {artifact.id: artifact for artifact in project.artifacts}
                if any(item not in artifacts for item in dependency_ids):
                    raise ProjectStoreError("unknown dependency artifact")
                if any(artifacts[item].stale for item in dependency_ids):
                    raise ProjectStoreError("stale dependency artifact")
                known_ids = set(artifacts)
                extension = self._extension_for(media_type)
                stage_name = None if kind == "input" else self._safe_component(stage or kind, "stage")

                for _ in range(16):
                    artifact_id = uuid4()
                    if artifact_id in known_ids:
                        continue
                    relative_path = (
                        f"inputs/{artifact_id}.{extension}"
                        if stage_name is None
                        else f"candidates/{stage_name}/{artifact_id}/{variant}.{extension}"
                    )
                    try:
                        artifact = ArtifactRef(
                            id=artifact_id,
                            kind=kind,
                            relative_path=relative_path,
                            sha256=hashlib.sha256(data).hexdigest(),
                            media_type=media_type,
                            width=width,
                            height=height,
                            variant=variant,
                            source_job_id=source_job_id,
                            dependencies=dependency_ids,
                        )
                    except ValidationError as exc:
                        raise ProjectStoreError(f"invalid artifact: {exc}") from exc

                    updated = project.model_copy(
                        update={"artifacts": [*project.artifacts, artifact], "updated_at": self._now()}
                    )
                    with self._parent_fd_at(project_fd, relative_path, create=True) as (
                        parent_fd,
                        name,
                    ):
                        if self._entry_exists_at(parent_fd, name):
                            continue
                        try:
                            self._atomic_write_at(parent_fd, name, data, overwrite=False)
                        except _WriteFailure as exc:
                            if exc.collision:
                                continue
                            if exc.published:
                                self._remove_entry_at(parent_fd, name)
                            raise

                        try:
                            self._write_project_at(project_fd, updated)
                        except _WriteFailure as exc:
                            # Only remove the candidate when we know project.json
                            # did not cross its rename publication point.
                            if not exc.published:
                                self._remove_entry_at(parent_fd, name)
                            raise
                    return artifact
        raise ProjectStoreError("could not allocate a unique artifact ID")

    def _write_project_at(self, project_fd: int, project: ProjectConfig) -> None:
        self._atomic_write_at(project_fd, "project.json", self._project_json(project), overwrite=True)

    def _read_artifact_at(self, project_fd: int, artifact: ArtifactRef, *, digest: bool) -> str | None:
        with self._parent_fd_at(project_fd, artifact.relative_path, create=False) as (parent_fd, name):
            with self._regular_file_fd_at(parent_fd, name) as descriptor:
                if not digest:
                    return None
                value = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    value.update(chunk)
        result = value.hexdigest()
        if result != artifact.sha256:
            raise ProjectStoreError("artifact hash does not match its record")
        return result

    def _read_artifact_bytes_at(self, project_fd: int, artifact: ArtifactRef) -> bytes:
        with self._parent_fd_at(project_fd, artifact.relative_path, create=False) as (parent_fd, name):
            data = self._read_file_at(parent_fd, name)
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ProjectStoreError("artifact hash does not match its record")
        return data

    def _approval_hashes_at(
        self, project_fd: int, approval: Approval, artifacts: dict[UUID, ArtifactRef]
    ) -> list[str]:
        current_hashes: list[str] = []
        for artifact_id in approval.artifact_ids:
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                raise ProjectStoreError("unknown artifact")
            if artifact.stale:
                raise ProjectStoreError("cannot approve a stale artifact")
            current_hashes.append(self._read_artifact_at(project_fd, artifact, digest=True) or "")
        return current_hashes

    def _approval_for(
        self,
        gate: ApprovalGate | Approval,
        artifact_ids: Iterable[UUID | str] | None,
        note: str,
    ) -> Approval:
        if isinstance(gate, Approval):
            if artifact_ids is not None:
                raise ProjectStoreError("artifact IDs must be part of the approval record")
            return gate
        ids = [self._uuid(item, "artifact ID") for item in artifact_ids or ()]
        try:
            return Approval(
                gate=gate,
                artifact_ids=ids,
                artifact_hashes=["0" * 64 for _ in ids],
                note=note,
            )
        except ValidationError as exc:
            raise ProjectStoreError(f"invalid approval: {exc}") from exc

    def _invalidate(
        self, project: ProjectConfig, artifact_ids: Iterable[UUID | str]
    ) -> tuple[list[UUID], ProjectConfig | None]:
        changed = {self._uuid(item, "artifact ID") for item in artifact_ids}
        known_ids = {artifact.id for artifact in project.artifacts}
        if not changed.issubset(known_ids):
            raise ProjectStoreError("unknown artifact")
        invalidated: set[UUID] = set()
        frontier = set(changed)
        while frontier:
            descendants = {
                artifact.id
                for artifact in project.artifacts
                if artifact.id not in changed | invalidated
                and any(dependency in frontier for dependency in artifact.dependencies)
            }
            invalidated.update(descendants)
            frontier = descendants
        ordered = [artifact.id for artifact in project.artifacts if artifact.id in invalidated]
        if not ordered:
            return ordered, None
        return (
            ordered,
            project.model_copy(
                update={
                    "artifacts": [
                        artifact.model_copy(update={"stale": True})
                        if artifact.id in invalidated
                        else artifact
                        for artifact in project.artifacts
                    ],
                    "updated_at": self._now(),
                }
            ),
        )

    @classmethod
    def _relative_parts(cls, relative_path: str) -> tuple[str, ...]:
        try:
            relative = PurePosixPath(relative_path)
        except TypeError as exc:
            raise ProjectStoreError("unsafe relative path") from exc
        if (
            not relative_path
            or "\\" in relative_path
            or relative.is_absolute()
            or str(relative) != relative_path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ProjectStoreError("unsafe relative path")
        return relative.parts

    @staticmethod
    def _fsync(descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_FSYNC_ERRNOS:
                raise ProjectStoreError(f"could not fsync storage: {exc}") from exc

    @staticmethod
    def _extension_for(media_type: str) -> str:
        extensions = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "video/mp4": "mp4",
            "application/json": "json",
        }
        return extensions.get(media_type.lower().split(";", 1)[0], "bin")

    @staticmethod
    def _safe_component(value: str, label: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or ":" in value
            or "\x00" in value
        ):
            raise ProjectStoreError(f"unsafe {label}")
        return value

    @staticmethod
    def _uuid(value: UUID | str, label: str) -> UUID:
        try:
            return value if isinstance(value, UUID) else UUID(str(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProjectStoreError(f"invalid {label}") from exc

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _project_json(project: ProjectConfig) -> bytes:
        return (
            json.dumps(
                project.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    @staticmethod
    def _job_json(job: JobRecord) -> bytes:
        return (
            json.dumps(
                job.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    @classmethod
    def _prompt_json(cls, record: dict[str, Any]) -> bytes:
        if not isinstance(record, dict):
            raise ProjectStoreError("prompt record must be a JSON object")
        try:
            cls._validate_json_value(record, set())
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError) as exc:
            raise ProjectStoreError("prompt record must contain safe JSON values") from exc
        return encoded + b"\n"

    @classmethod
    def _validate_json_value(cls, value: Any, seen: set[int]) -> None:
        if value is None or isinstance(value, (str, bool, int)):
            return
        if isinstance(value, float):
            if math.isfinite(value):
                return
            raise ValueError("non-finite JSON number")
        if isinstance(value, list):
            if id(value) in seen:
                raise ValueError("recursive JSON value")
            seen.add(id(value))
            try:
                for item in value:
                    cls._validate_json_value(item, seen)
            finally:
                seen.remove(id(value))
            return
        if isinstance(value, dict):
            if id(value) in seen:
                raise ValueError("recursive JSON value")
            seen.add(id(value))
            try:
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("JSON object key is not a string")
                    cls._validate_json_value(item, seen)
            finally:
                seen.remove(id(value))
            return
        raise TypeError("not a JSON value")

    @staticmethod
    def _validate_project(project: ProjectConfig) -> ProjectConfig:
        try:
            return ProjectConfig.model_validate(project.model_dump(mode="python"))
        except (AttributeError, ValidationError) as exc:
            raise ProjectStoreError("invalid project") from exc
