"""Atomic, contained filesystem storage for sprite projects."""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Iterable
from uuid import UUID, uuid4

from pydantic import ValidationError

from .contracts import Approval, ApprovalGate, ArtifactRef, ArtifactVariant, ProjectConfig


_UNSUPPORTED_FSYNC_ERRNOS = {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


class ProjectStoreError(ValueError):
    """A project file or requested storage operation is unsafe or invalid."""


class ProjectStore:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.projects = self.workspace / "projects"

    def create(self, project: ProjectConfig | None = None) -> ProjectConfig:
        project = self._validate_project(project or ProjectConfig())
        if project.artifacts or project.approvals:
            raise ProjectStoreError("new projects cannot include externally supplied artifacts or approvals")

        projects_root = self._projects_root()
        target = projects_root / str(project.id)
        if os.path.lexists(target):
            raise ProjectStoreError("project already exists")

        payload = self._project_json(project)
        staging = Path(tempfile.mkdtemp(prefix=f".{project.id}.", dir=projects_root))
        try:
            for directory in ("inputs", "candidates", "run", "videos", "prompts", "jobs", "exports"):
                (staging / directory).mkdir()
            self._atomic_write(staging, staging / "events.ndjson", b"", overwrite=False)
            self._atomic_write(staging, staging / "project.json", payload, overwrite=False)
            self._contained(staging, projects_root)
            self._contained(target.parent, projects_root)
            os.replace(staging, target)
            self._fsync_directory(projects_root)
        except OSError as exc:
            raise ProjectStoreError(f"could not create project: {exc}") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return project

    def load(self, project_id: UUID | str) -> ProjectConfig:
        project_id = self._uuid(project_id, "project ID")
        root = self._project_root(project_id)
        project_path = self._safe_path(root, "project.json", require_exists=True)
        try:
            raw = json.loads(project_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectStoreError("malformed project JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ProjectStoreError("unsupported project schema version")
        try:
            project = ProjectConfig.model_validate(raw)
        except ValidationError as exc:
            raise ProjectStoreError(f"invalid project JSON: {exc}") from exc
        if project.id != project_id:
            raise ProjectStoreError("project ID does not match its directory")
        return project

    def save(self, project: ProjectConfig) -> ProjectConfig:
        project = self._validate_project(project)
        current = self.load(project.id)
        if project.artifacts != current.artifacts or project.approvals != current.approvals:
            raise ProjectStoreError("artifacts and approvals are managed by ProjectStore")
        saved = project.model_copy(
            update={"created_at": current.created_at, "updated_at": self._now()}
        )
        self._write_project(self._project_root(saved.id), saved)
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
        project = self.load(project_id)
        root = self._project_root(project_id)
        dependency_ids = [self._uuid(item, "dependency ID") for item in dependencies]
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ProjectStoreError("duplicate dependency ID")
        known_ids = {artifact.id for artifact in project.artifacts}
        if any(item not in known_ids for item in dependency_ids):
            raise ProjectStoreError("unknown dependency artifact")

        extension = self._extension_for(media_type)
        if kind == "input":
            stage_name = None
        else:
            stage_name = self._safe_component(stage or kind, "stage")
        for _ in range(16):
            artifact_id = uuid4()
            if artifact_id in known_ids:
                continue
            relative_path = (
                f"inputs/{artifact_id}.{extension}"
                if stage_name is None
                else f"candidates/{stage_name}/{artifact_id}/{variant}.{extension}"
            )
            artifact_path = self._safe_path(root, relative_path)
            if not os.path.lexists(artifact_path):
                break
        else:
            raise ProjectStoreError("could not allocate a unique artifact ID")
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

        project_path = self._safe_path(root, "project.json", require_exists=True)
        updated = project.model_copy(
            update={"artifacts": [*project.artifacts, artifact], "updated_at": self._now()}
        )
        payload = self._project_json(updated)
        # Both destinations have been validated before either file is changed.
        self._ensure_parent(root, artifact_path)
        try:
            self._atomic_write(root, artifact_path, data, overwrite=False)
            self._atomic_write(root, project_path, payload, overwrite=True)
        except Exception:
            self._remove_new_artifact(root, artifact_path)
            raise
        return artifact

    def get_artifact(self, project_id: UUID | str, artifact_id: UUID | str) -> Path:
        project_id = self._uuid(project_id, "project ID")
        artifact_id = self._uuid(artifact_id, "artifact ID")
        project = self.load(project_id)
        artifact = next((item for item in project.artifacts if item.id == artifact_id), None)
        if artifact is None:
            raise ProjectStoreError("unknown artifact")
        root = self._project_root(project_id)
        return self._artifact_path(root, artifact)

    def approve(
        self,
        project_id: UUID | str,
        gate: ApprovalGate | Approval,
        artifact_ids: Iterable[UUID | str] | None = None,
        *,
        note: str = "",
    ) -> Approval:
        project_id = self._uuid(project_id, "project ID")
        project = self.load(project_id)
        root = self._project_root(project_id)
        if isinstance(gate, Approval):
            if artifact_ids is not None:
                raise ProjectStoreError("artifact IDs must be part of the approval record")
            approval = gate
        else:
            ids = [self._uuid(item, "artifact ID") for item in artifact_ids or ()]
            try:
                approval = Approval(
                    gate=gate,
                    artifact_ids=ids,
                    artifact_hashes=["0" * 64 for _ in ids],
                    note=note,
                )
            except ValidationError as exc:
                raise ProjectStoreError(f"invalid approval: {exc}") from exc

        artifacts = {artifact.id: artifact for artifact in project.artifacts}
        current_hashes: list[str] = []
        for artifact_id in approval.artifact_ids:
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                raise ProjectStoreError("unknown artifact")
            if artifact.stale:
                raise ProjectStoreError("cannot approve a stale artifact")
            current_hashes.append(self._live_hash(root, artifact))

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
        self._write_project(root, updated)
        return approval

    def invalidate_dependants(
        self, project_id: UUID | str, artifact_ids: Iterable[UUID | str]
    ) -> list[UUID]:
        project_id = self._uuid(project_id, "project ID")
        project = self.load(project_id)
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
        if ordered:
            updated = project.model_copy(
                update={
                    "artifacts": [
                        artifact.model_copy(update={"stale": True})
                        if artifact.id in invalidated
                        else artifact
                        for artifact in project.artifacts
                    ],
                    "updated_at": self._now(),
                }
            )
            self._write_project(self._project_root(project_id), updated)
        return ordered

    def _projects_root(self) -> Path:
        try:
            self.projects.mkdir(parents=True, exist_ok=True)
            root = self.projects.resolve()
        except OSError as exc:
            raise ProjectStoreError(f"could not create workspace: {exc}") from exc
        if not root.is_dir():
            raise ProjectStoreError("workspace projects path is not a directory")
        try:
            root.relative_to(self.workspace)
        except ValueError as exc:
            raise ProjectStoreError("workspace projects path resolves outside workspace") from exc
        return root

    def _project_root(self, project_id: UUID) -> Path:
        projects_root = self._projects_root()
        root = projects_root / str(project_id)
        resolved = self._contained(root, projects_root)
        if not resolved.is_dir():
            raise ProjectStoreError("unknown project")
        return resolved

    def _write_project(self, root: Path, project: ProjectConfig) -> None:
        self._atomic_write(
            root,
            self._safe_path(root, "project.json", require_exists=True),
            self._project_json(project),
            overwrite=True,
        )

    @classmethod
    def _safe_path(cls, root: Path, relative_path: str, *, require_exists: bool = False) -> Path:
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
        path = root.joinpath(*relative.parts)
        resolved = cls._contained(path, root)
        if require_exists and not resolved.exists():
            raise ProjectStoreError("stored path is missing")
        return resolved

    @staticmethod
    def _contained(path: Path, root: Path) -> Path:
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(root.resolve())
        except (OSError, ValueError) as exc:
            raise ProjectStoreError("path resolves outside the project") from exc
        return resolved

    @classmethod
    def _artifact_path(cls, root: Path, artifact: ArtifactRef) -> Path:
        path = cls._safe_path(root, artifact.relative_path, require_exists=True)
        if not path.is_file():
            raise ProjectStoreError("stored artifact is not a file")
        cls._contained(path, root)
        return path

    @classmethod
    def _live_hash(cls, root: Path, artifact: ArtifactRef) -> str:
        path = cls._artifact_path(root, artifact)
        digest = hashlib.sha256()
        try:
            cls._contained(path, root)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
        except (OSError, ProjectStoreError) as exc:
            raise ProjectStoreError("could not safely read artifact") from exc
        value = digest.hexdigest()
        if value != artifact.sha256:
            raise ProjectStoreError("artifact hash does not match its record")
        return value

    @classmethod
    def _ensure_parent(cls, root: Path, target: Path) -> None:
        cls._contained(target.parent, root)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectStoreError(f"could not prepare artifact directory: {exc}") from exc
        cls._contained(target.parent, root)

    @classmethod
    def _atomic_write(cls, root: Path, target: Path, data: bytes, *, overwrite: bool) -> None:
        cls._contained(target.parent, root)
        if not overwrite and os.path.lexists(target):
            raise ProjectStoreError("refusing to overwrite an immutable candidate")
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        except OSError as exc:
            raise ProjectStoreError(f"could not create temporary storage file: {exc}") from exc
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as file:
                cls._contained(temporary_path.parent, root)
                cls._contained(target.parent, root)
                file.write(data)
                file.flush()
                cls._fsync(file.fileno())
            if not overwrite and os.path.lexists(target):
                raise ProjectStoreError("refusing to overwrite an immutable candidate")
            cls._contained(target.parent, root)
            try:
                os.replace(temporary_path, target)
            except OSError as exc:
                raise ProjectStoreError(f"could not replace storage file: {exc}") from exc
            cls._fsync_directory(target.parent)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @classmethod
    def _remove_new_artifact(cls, root: Path, path: Path) -> None:
        cls._contained(path.parent, root)
        try:
            if os.path.lexists(path):
                path.unlink()
                cls._fsync_directory(path.parent)
        except OSError as exc:
            raise ProjectStoreError(f"could not roll back artifact: {exc}") from exc

    @staticmethod
    def _fsync(descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_FSYNC_ERRNOS:
                raise ProjectStoreError(f"could not fsync storage: {exc}") from exc

    @classmethod
    def _fsync_directory(cls, directory: Path) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise ProjectStoreError(f"could not open storage directory for fsync: {exc}") from exc
        try:
            cls._fsync(descriptor)
        finally:
            os.close(descriptor)

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
    def _validate_project(project: ProjectConfig) -> ProjectConfig:
        try:
            return ProjectConfig.model_validate(project.model_dump(mode="python"))
        except (AttributeError, ValidationError) as exc:
            raise ProjectStoreError("invalid project") from exc
