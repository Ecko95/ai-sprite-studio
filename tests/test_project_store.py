import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from ai_sprite_studio.contracts import Approval, ProjectConfig
from ai_sprite_studio.project_store import ProjectStore, ProjectStoreError


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


def test_save_atomically_replaces_project_json_without_temp_residue(store, tmp_path, monkeypatch):
    project = _project(store)
    project_path = tmp_path / "projects" / str(project.id) / "project.json"
    replacements = []
    real_replace = os.replace

    def record_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr("ai_sprite_studio.project_store.os.replace", record_replace)
    project.name = "Renamed ranger"
    store.save(project)

    assert store.load(project.id).name == "Renamed ranger"
    assert any(destination == project_path for _, destination in replacements)
    assert not list(project_path.parent.glob(".project.json.*"))


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
