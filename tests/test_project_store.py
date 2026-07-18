import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest

from ai_sprite_studio.contracts import Approval, ProjectConfig
from ai_sprite_studio.project_store import (
    ProjectStore,
    ProjectStoreConflictError,
    ProjectStoreError,
)


@pytest.fixture
def store(tmp_path):
    return ProjectStore(tmp_path)


def _project(store):
    return store.create(ProjectConfig(name="Test ranger"))


def _artifact(store, project_id, *, dependencies=(), stage="base", data=b"pixel bytes"):
    return store.put_artifact(
        project_id,
        data,
        kind="candidate",
        media_type="image/png",
        variant="raw",
        stage=stage,
        width=256,
        height=256,
        dependencies=list(dependencies),
    )


def test_create_builds_the_exact_uuid_project_layout(store, tmp_path):
    project = _project(store)
    root = tmp_path / "projects" / str(project.id)

    assert root.is_dir()
    assert (root / "project.json").is_file()
    assert (root / "events.ndjson").read_bytes() == b""
    assert {path.name for path in root.iterdir()} == {
        "project.json",
        "inputs",
        "candidates",
        "run",
        "videos",
        "prompts",
        "jobs",
        "events.ndjson",
        "exports",
    }
    assert store.load(project.id).id == project.id


def test_create_never_replaces_a_project_directory_that_appears_during_creation(
    tmp_path, monkeypatch
):
    projects = tmp_path / "projects"
    projects.mkdir()
    project = ProjectConfig(name="Racing ranger")
    target = projects / str(project.id)
    real_mkdir = os.mkdir
    raced = False

    def create_target_then_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if not raced:
            real_mkdir(target, 0o755)
            raced = True
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr("ai_sprite_studio.project_store.os.mkdir", create_target_then_mkdir)

    with pytest.raises(ProjectStoreError, match="project already exists"):
        ProjectStore(tmp_path).create(project)

    assert raced
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_save_atomically_replaces_project_json_without_temp_residue(store, tmp_path, monkeypatch):
    project = _project(store)
    project_path = tmp_path / "projects" / str(project.id) / "project.json"
    replacements = []
    real_replace = os.replace

    def record_replace(source, destination, *args, **kwargs):
        replacements.append((source, destination, kwargs))
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr("ai_sprite_studio.project_store.os.replace", record_replace)
    project.name = "Renamed ranger"
    store.save(project)

    assert store.load(project.id).name == "Renamed ranger"
    assert any(
        destination == "project.json" and kwargs["dst_dir_fd"] >= 0
        for _, destination, kwargs in replacements
    )
    assert not list(project_path.parent.glob(".project.json.*"))


def test_atomic_save_fsyncs_temp_then_parent_after_replace(store, monkeypatch):
    project = _project(store)
    events = []
    real_replace = os.replace

    def record_fsync(_descriptor):
        events.append("fsync")

    def record_replace(source, destination, *args, **kwargs):
        events.append("replace")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr("ai_sprite_studio.project_store.os.fsync", record_fsync)
    monkeypatch.setattr("ai_sprite_studio.project_store.os.replace", record_replace)
    project.name = "Durable ranger"
    store.save(project)

    assert events == ["fsync", "replace", "fsync"]


def test_create_fsyncs_projects_root_after_initializing_final_directory(tmp_path, monkeypatch):
    events = []

    def record_fsync(descriptor):
        events.append(Path(os.readlink(f"/proc/self/fd/{descriptor}")))

    monkeypatch.setattr("ai_sprite_studio.project_store.os.fsync", record_fsync)
    project = ProjectStore(tmp_path).create(ProjectConfig(name="Durable ranger"))

    assert events[-1] == tmp_path / "projects"
    assert tmp_path / "projects" / str(project.id) in events


def test_atomic_save_tolerates_explicitly_unsupported_fsync_errors(store, monkeypatch):
    project = _project(store)

    def unsupported_fsync(_descriptor):
        raise OSError(errno.EINVAL, "unsupported")

    monkeypatch.setattr("ai_sprite_studio.project_store.os.fsync", unsupported_fsync)
    project.name = "Portable ranger"

    store.save(project)

    assert store.load(project.id).name == "Portable ranger"


def test_atomic_save_propagates_supported_fsync_failures(store, monkeypatch):
    project = _project(store)

    def fail_fsync(_descriptor):
        raise OSError(errno.EIO, "disk failure")

    monkeypatch.setattr("ai_sprite_studio.project_store.os.fsync", fail_fsync)
    project.name = "Must not persist"

    with pytest.raises(ProjectStoreError, match="fsync"):
        store.save(project)

    assert store.load(project.id).name == "Test ranger"


def test_put_artifact_keeps_its_file_when_manifest_fsync_fails_after_publish(
    store, tmp_path, monkeypatch
):
    project = _project(store)
    artifact_id = uuid4()
    project_root = tmp_path / "projects" / str(project.id)
    real_fsync = os.fsync
    real_replace = os.replace
    manifest_published = False

    def fail_manifest_directory_fsync(descriptor):
        if manifest_published and Path(os.readlink(f"/proc/self/fd/{descriptor}")) == project_root:
            raise OSError(errno.EIO, "manifest directory sync failed")
        return real_fsync(descriptor)

    def record_manifest_replace(source, destination, *args, **kwargs):
        nonlocal manifest_published
        result = real_replace(source, destination, *args, **kwargs)
        if destination == "project.json":
            manifest_published = True
        return result

    monkeypatch.setattr("ai_sprite_studio.project_store.uuid4", lambda: artifact_id)
    monkeypatch.setattr(
        "ai_sprite_studio.project_store.os.fsync", fail_manifest_directory_fsync
    )
    monkeypatch.setattr("ai_sprite_studio.project_store.os.replace", record_manifest_replace)

    with pytest.raises(ProjectStoreError, match="fsync"):
        _artifact(store, project.id)

    loaded = store.load(project.id)
    assert [artifact.id for artifact in loaded.artifacts] == [artifact_id]
    assert store.get_artifact(project.id, artifact_id).read_bytes() == b"pixel bytes"


def test_load_keeps_the_original_project_when_projects_is_swapped_before_read(
    store, tmp_path, monkeypatch
):
    project = _project(store)
    projects = tmp_path / "projects"
    original = projects / str(project.id) / "project.json"
    outside = tmp_path / "outside"
    outside_project = outside / str(project.id)
    outside_project.mkdir(parents=True)
    attacker = json.loads(original.read_text())
    attacker["name"] = "Attacker ranger"
    (outside_project / "project.json").write_text(json.dumps(attacker))
    real_open = os.open
    swapped = False

    def swap_before_read(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and path == "project.json":
            projects.rename(tmp_path / "projects-real")
            projects.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("ai_sprite_studio.project_store.os.open", swap_before_read)

    assert store.load(project.id).name == "Test ranger"
    assert swapped


def test_save_uses_the_pinned_project_directory_after_a_projects_swap(
    store, tmp_path, monkeypatch
):
    project = _project(store)
    projects = tmp_path / "projects"
    outside = tmp_path / "outside"
    outside_project = outside / str(project.id)
    outside_project.mkdir(parents=True)
    attacker = json.loads((projects / str(project.id) / "project.json").read_text())
    attacker["name"] = "Attacker ranger"
    (outside_project / "project.json").write_text(json.dumps(attacker))
    real_replace = os.replace
    swapped = False

    def swap_before_replace(source, destination, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            projects.rename(tmp_path / "projects-real")
            projects.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr("ai_sprite_studio.project_store.os.replace", swap_before_replace)
    project.name = "Trusted ranger"

    store.save(project)

    persisted = json.loads(
        (tmp_path / "projects-real" / str(project.id) / "project.json").read_text()
    )
    assert persisted["name"] == "Trusted ranger"
    assert json.loads((outside_project / "project.json").read_text())["name"] == "Attacker ranger"


def test_projects_root_rejects_a_workspace_escaping_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "projects").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStoreError, match="outside workspace"):
        ProjectStore(workspace).create(ProjectConfig(name="Unsafe ranger"))

    assert not list(outside.iterdir())


def test_load_rejects_malformed_json_and_unknown_schema_version(store, tmp_path):
    project = _project(store)
    project_path = tmp_path / "projects" / str(project.id) / "project.json"

    project_path.write_text("{not json")
    with pytest.raises(ProjectStoreError, match="malformed"):
        store.load(project.id)

    project_path.write_text(json.dumps({"schema_version": 2}))
    with pytest.raises(ProjectStoreError, match="schema"):
        store.load(project.id)


def test_put_artifact_generates_uuid_path_and_never_reuses_a_candidate(store):
    project = _project(store)
    first = _artifact(store, project.id)
    second = _artifact(store, project.id)

    assert first.id != second.id
    assert first.relative_path == f"candidates/base/{first.id}/raw.png"
    assert second.relative_path == f"candidates/base/{second.id}/raw.png"
    assert first.sha256 == hashlib.sha256(b"pixel bytes").hexdigest()
    assert store.get_artifact(project.id, first.id).read_bytes() == b"pixel bytes"
    assert store.get_artifact(project.id, second.id).read_bytes() == b"pixel bytes"


def test_put_artifact_validates_dependencies_before_writing_any_candidate(store, tmp_path):
    project = _project(store)
    candidate_root = tmp_path / "projects" / str(project.id) / "candidates"

    with pytest.raises(ProjectStoreError, match="unknown dependency"):
        _artifact(store, project.id, dependencies=[uuid4()])

    assert not list(candidate_root.rglob("*"))
    assert store.load(project.id).artifacts == []


def test_put_artifact_rechecks_stale_dependencies_from_the_fresh_manifest(store):
    project = _project(store)
    root = _artifact(store, project.id, stage="root")
    stale_dependency = _artifact(store, project.id, dependencies=[root.id], stage="child")
    store.invalidate_dependants(project.id, [root.id])

    with pytest.raises(ProjectStoreError, match="stale dependency"):
        _artifact(store, project.id, dependencies=[stale_dependency.id], stage="grandchild")

    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}
    assert artifacts[stale_dependency.id].stale


def test_put_artifact_rejects_non_string_media_type_at_its_public_boundary(store, tmp_path):
    project = _project(store)
    candidate_root = tmp_path / "projects" / str(project.id) / "candidates"

    with pytest.raises(ProjectStoreError, match="media type"):
        store.put_artifact(
            project.id,
            b"pixels",
            kind="candidate",
            media_type=None,
            variant="raw",
        )

    assert not list(candidate_root.rglob("*"))


def test_put_artifact_rolls_back_when_manifest_publish_fails(store, tmp_path, monkeypatch):
    project = _project(store)
    candidate_root = tmp_path / "projects" / str(project.id) / "candidates"
    real_replace = os.replace

    def fail_manifest_replace(source, destination, *args, **kwargs):
        if Path(destination).name == "project.json":
            raise OSError(errno.EIO, "manifest failure")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr("ai_sprite_studio.project_store.os.replace", fail_manifest_replace)

    with pytest.raises(ProjectStoreError, match="replace"):
        _artifact(store, project.id)

    assert not [path for path in candidate_root.rglob("*") if path.is_file()]
    assert store.load(project.id).artifacts == []


def test_put_artifact_rolls_back_if_candidate_directory_sync_fails(store, tmp_path, monkeypatch):
    project = _project(store)
    candidate_root = tmp_path / "projects" / str(project.id) / "candidates"
    real_fsync_directory = ProjectStore._fsync_directory_fd
    calls = 0

    def fail_once(_cls, descriptor):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProjectStoreError("candidate directory sync failed")
        return real_fsync_directory(descriptor)

    monkeypatch.setattr(ProjectStore, "_fsync_directory_fd", classmethod(fail_once))

    with pytest.raises(ProjectStoreError, match="directory sync"):
        _artifact(store, project.id)

    assert not [path for path in candidate_root.rglob("*") if path.is_file()]
    assert store.load(project.id).artifacts == []


def test_put_artifact_does_not_commit_a_manifest_when_a_created_parent_sync_fails(
    store, tmp_path, monkeypatch
):
    project = _project(store)
    candidates = tmp_path / "projects" / str(project.id) / "candidates"
    real_fsync_directory = ProjectStore._fsync_directory_fd

    def fail_created_parent_sync(_cls, descriptor):
        if Path(os.readlink(f"/proc/self/fd/{descriptor}")) == candidates:
            raise ProjectStoreError("created candidate parent sync failed")
        return real_fsync_directory(descriptor)

    monkeypatch.setattr(
        ProjectStore, "_fsync_directory_fd", classmethod(fail_created_parent_sync)
    )

    with pytest.raises(ProjectStoreError, match="created candidate parent sync"):
        _artifact(store, project.id)

    assert store.load(project.id).artifacts == []


def test_put_artifact_resyncs_a_parent_left_by_a_failed_attempt_before_retrying(
    store, tmp_path, monkeypatch
):
    project = _project(store)
    candidates = tmp_path / "projects" / str(project.id) / "candidates"
    real_fsync_directory = ProjectStore._fsync_directory_fd
    real_replace = os.replace
    events = []
    fail_once = True

    def record_parent_sync(_cls, descriptor):
        nonlocal fail_once
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        events.append(("fsync", path))
        if fail_once and path == candidates:
            fail_once = False
            raise ProjectStoreError("first candidate parent sync failed")
        return real_fsync_directory(descriptor)

    def record_manifest_publish(source, destination, *args, **kwargs):
        if destination == "project.json":
            events.append(("manifest", None))
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(ProjectStore, "_fsync_directory_fd", classmethod(record_parent_sync))
    monkeypatch.setattr("ai_sprite_studio.project_store.os.replace", record_manifest_publish)

    with pytest.raises(ProjectStoreError, match="first candidate parent sync"):
        _artifact(store, project.id)

    retry_start = len(events)
    artifact = _artifact(store, project.id)
    manifest_index = events.index(("manifest", None), retry_start)

    assert ("fsync", candidates) in events[retry_start:manifest_index]
    assert [item.id for item in store.load(project.id).artifacts] == [artifact.id]


def test_put_artifact_never_overwrites_a_target_created_during_immutable_publish(
    store, tmp_path, monkeypatch
):
    project = _project(store)
    artifact_id = uuid4()
    target = (
        tmp_path / "projects" / str(project.id) / "candidates" / "base" / str(artifact_id) / "raw.png"
    )
    real_link = os.link

    def create_target_then_link(source, destination, *args, **kwargs):
        target.write_bytes(b"attacker bytes")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr("ai_sprite_studio.project_store.uuid4", lambda: artifact_id)
    monkeypatch.setattr("ai_sprite_studio.project_store.os.link", create_target_then_link)

    with pytest.raises(ProjectStoreError):
        _artifact(store, project.id)

    assert target.read_bytes() == b"attacker bytes"
    assert store.load(project.id).artifacts == []


def test_put_artifact_retries_a_generated_uuid_collision(store, monkeypatch):
    project = _project(store)
    existing = _artifact(store, project.id)
    replacement_id = uuid4()
    generated = iter([existing.id, replacement_id])
    monkeypatch.setattr("ai_sprite_studio.project_store.uuid4", lambda: next(generated))

    replacement = _artifact(store, project.id)

    assert replacement.id == replacement_id
    assert [artifact.id for artifact in store.load(project.id).artifacts] == [
        existing.id,
        replacement_id,
    ]


def test_put_artifact_revalidates_a_parent_swapped_to_an_escaping_symlink(
    store, tmp_path, monkeypatch
):
    project = _project(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    candidates = tmp_path / "projects" / str(project.id) / "candidates"
    moved = candidates.with_name("candidates-moved")
    real_open = os.open
    swapped = False

    def swap_parent(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and path == "base":
            candidates.rename(moved)
            candidates.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("ai_sprite_studio.project_store.os.open", swap_parent)

    artifact = _artifact(store, project.id)

    assert swapped
    assert not list(outside.iterdir())
    assert (moved / "base" / str(artifact.id) / "raw.png").read_bytes() == b"pixel bytes"


def test_live_hash_rejects_an_artifact_swapped_to_a_symlink_after_lookup(
    store, tmp_path, monkeypatch
):
    project = _project(store)
    artifact = _artifact(store, project.id)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"pixel bytes")
    path = tmp_path / "projects" / str(project.id) / artifact.relative_path
    real_open = os.open
    swapped = False

    def swap_artifact(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and name == path.name:
            path.unlink()
            path.symlink_to(outside)
            swapped = True
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("ai_sprite_studio.project_store.os.open", swap_artifact)

    with pytest.raises(ProjectStoreError, match="outside"):
        store.approve(project.id, "base", [artifact.id])

    assert swapped


@pytest.mark.parametrize("stage", ["../outside", "C:outside"])
def test_put_artifact_rejects_unsafe_stage_before_writing(store, tmp_path, stage):
    project = _project(store)
    candidate_root = tmp_path / "projects" / str(project.id) / "candidates"

    with pytest.raises(ProjectStoreError, match="unsafe stage"):
        _artifact(store, project.id, stage=stage)

    assert not list(candidate_root.rglob("*"))


def test_get_artifact_rejects_unknown_ids_and_symlinks_that_escape_project(store, tmp_path):
    project = _project(store)
    artifact = _artifact(store, project.id)

    with pytest.raises(ProjectStoreError, match="unknown artifact"):
        store.get_artifact(project.id, uuid4())

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    path = tmp_path / "projects" / str(project.id) / artifact.relative_path
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(ProjectStoreError, match="outside"):
        store.get_artifact(project.id, artifact.id)


def test_approve_records_live_hashes_and_rejects_stale_or_mismatched_records(store):
    project = _project(store)
    artifact = _artifact(store, project.id)

    approval = store.approve(project.id, "base", [artifact.id], note="looks right")

    assert approval.artifact_hashes == [artifact.sha256]
    assert store.load(project.id).approvals == [approval]

    mismatched = Approval(
        gate="base",
        artifact_ids=[artifact.id],
        artifact_hashes=["0" * 64],
    )
    with pytest.raises(ProjectStoreError, match="hash"):
        store.approve(project.id, mismatched)

    store.invalidate_dependants(project.id, [artifact.id])
    # The root is still current; only its dependants become stale.
    assert store.approve(project.id, "base", [artifact.id]).artifact_ids == [artifact.id]

    store.get_artifact(project.id, artifact.id).write_bytes(b"changed outside the store")
    with pytest.raises(ProjectStoreError, match="hash"):
        store.approve(project.id, "base", [artifact.id])


def test_replace_approval_invalidates_every_dropped_prior_root(store):
    project = _project(store)
    first = _artifact(store, project.id, stage="first")
    dropped = _artifact(store, project.id, stage="dropped")
    replacement = _artifact(store, project.id, stage="replacement")
    dropped_child = _artifact(store, project.id, dependencies=[dropped.id], stage="child")
    store.approve(project.id, "base", [first.id, dropped.id])

    store.replace_approval_and_invalidate_dependants(
        project.id,
        "base",
        [replacement.id],
        previous_artifact_ids=[first.id, dropped.id],
    )

    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}
    assert artifacts[dropped_child.id].stale


def test_approval_publications_reject_a_stale_manifest_revision(store):
    project = _project(store)
    approved = _artifact(store, project.id, stage="approved")
    replacement = _artifact(store, project.id, stage="replacement")
    store.approve(project.id, "base", [approved.id])
    _, revision = store.load_with_manifest_revision(project.id)
    _artifact(store, project.id, stage="unrelated")

    with pytest.raises(ProjectStoreConflictError, match="manifest changed"):
        store.approve(
            project.id,
            "base",
            [approved.id],
            expected_manifest_revision=revision,
        )
    with pytest.raises(ProjectStoreConflictError, match="manifest changed"):
        store.replace_approval_and_invalidate_dependants(
            project.id,
            "base",
            [replacement.id],
            previous_artifact_ids=[approved.id],
            expected_manifest_revision=revision,
        )

    assert store.load(project.id).approvals[0].artifact_ids == [approved.id]


def test_invalidate_dependants_marks_only_transitive_children_stale_and_keeps_files(store):
    project = _project(store)
    root = _artifact(store, project.id, stage="base")
    child = _artifact(store, project.id, stage="directions", dependencies=[root.id])
    grandchild = _artifact(store, project.id, stage="frames", dependencies=[child.id])
    sibling = _artifact(store, project.id, stage="other")
    child_path = store.get_artifact(project.id, child.id)
    grandchild_path = store.get_artifact(project.id, grandchild.id)

    invalidated = store.invalidate_dependants(project.id, [root.id])
    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}

    assert invalidated == [child.id, grandchild.id]
    assert artifacts[root.id].stale is False
    assert artifacts[child.id].stale is True
    assert artifacts[grandchild.id].stale is True
    assert artifacts[sibling.id].stale is False
    assert child_path.is_file()
    assert grandchild_path.is_file()
    with pytest.raises(ProjectStoreError, match="stale"):
        store.approve(project.id, "directions", [child.id])


def test_manifest_mutations_hold_a_project_lock_until_the_new_manifest_is_published(
    store, tmp_path, monkeypatch
):
    project = _project(store)
    root = _artifact(store, project.id, stage="root")
    child = _artifact(store, project.id, dependencies=[root.id], stage="child")
    project_root = tmp_path / "projects" / str(project.id)
    lock_path = project_root / ".project.lock"
    writer_ready = Event()
    release_writer = Event()
    invalidation_started = Event()
    invalidation_finished = Event()
    errors: list[BaseException] = []
    real_write = store._write_project_at
    paused = False

    def pause_first_manifest_write(project_fd, updated):
        nonlocal paused
        if not paused:
            paused = True
            writer_ready.set()
            if not release_writer.wait(5):
                raise RuntimeError("writer release timed out")
        return real_write(project_fd, updated)

    def capture(operation):
        try:
            operation()
        except BaseException as exc:  # pragma: no cover - assertion reports it below
            errors.append(exc)

    monkeypatch.setattr(store, "_write_project_at", pause_first_manifest_write)
    writer = Thread(
        target=lambda: capture(
            lambda: _artifact(store, project.id, stage="writer", data=b"writer")
        )
    )
    writer.start()
    try:
        assert writer_ready.wait(5)
        assert lock_path.is_file()
        descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

        def invalidate() -> None:
            invalidation_started.set()
            capture(lambda: store.invalidate_dependants(project.id, [root.id]))
            invalidation_finished.set()

        invalidator = Thread(target=invalidate)
        invalidator.start()
        assert invalidation_started.wait(5)
        assert not invalidation_finished.wait(0.1)
    finally:
        release_writer.set()
        writer.join(5)
        if "invalidator" in locals():
            invalidator.join(5)

    assert not writer.is_alive()
    assert not invalidator.is_alive()
    assert not errors
    assert {artifact.id: artifact for artifact in store.load(project.id).artifacts}[child.id].stale
