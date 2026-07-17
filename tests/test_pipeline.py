import sys
from types import ModuleType
from uuid import uuid4

import pytest

from ai_sprite_studio.contracts import ProjectConfig, StateSpec
from ai_sprite_studio.pipeline import PipelineError, approve_gate, preflight_requests, put_stage_artifact
from ai_sprite_studio.project_store import ProjectStore, ProjectStoreError


def _base_snap(store, project_id, source_id, data):
    generated = put_stage_artifact(
        store,
        project_id,
        "base_generation",
        data + b" generation",
        media_type="image/png",
        dependencies=[source_id],
    )
    return put_stage_artifact(
        store,
        project_id,
        "base_snap",
        data + b" snap",
        media_type="image/png",
        dependencies=[generated.id],
    )


def _directions(store, project_id, snapped, data):
    return put_stage_artifact(
        store,
        project_id,
        "directional_anchors",
        data,
        media_type="image/png",
        dependencies=[snapped.id],
    )


def test_stage_artifacts_record_their_required_direct_dependencies(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )

    generated = put_stage_artifact(
        store,
        project.id,
        "base_generation",
        b"base",
        media_type="image/png",
        dependencies=[source.id],
    )
    snapped = put_stage_artifact(
        store,
        project.id,
        "base_snap",
        b"snapped",
        media_type="image/png",
        dependencies=[generated.id],
    )
    directions = put_stage_artifact(
        store,
        project.id,
        "directional_anchors",
        b"directions",
        media_type="image/png",
        dependencies=[snapped.id],
    )
    walk = put_stage_artifact(
        store,
        project.id,
        "walk_i2v",
        b"walk",
        media_type="video/mp4",
        dependencies=[directions.id],
    )
    walk_frames = put_stage_artifact(
        store,
        project.id,
        "per_frame_snap",
        b"walk frames",
        media_type="image/png",
        dependencies=[walk.id],
    )

    assert generated.kind == "base_generation"
    assert generated.dependencies == [source.id]
    assert "candidates/base_generation/" in generated.relative_path
    assert snapped.kind == "base_snap"
    assert walk.dependencies == [directions.id]
    assert walk_frames.dependencies == [walk.id]
    with pytest.raises(PipelineError, match="unknown input"):
        put_stage_artifact(
            store,
            project.id,
            "base_snap",
            b"unknown",
            media_type="image/png",
            dependencies=[uuid4()],
        )
    with pytest.raises(PipelineError, match="invalid input artifact ID"):
        put_stage_artifact(
            store,
            project.id,
            "base_snap",
            b"malformed",
            media_type="image/png",
            dependencies=["not-a-uuid"],
        )


def test_stage_artifacts_reject_stale_inputs_without_deleting_them(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    generated = put_stage_artifact(
        store,
        project.id,
        "base_generation",
        b"base",
        media_type="image/png",
        dependencies=[source.id],
    )
    store.invalidate_dependants(project.id, [source.id])

    with pytest.raises(PipelineError, match="stale input"):
        put_stage_artifact(
            store,
            project.id,
            "base_snap",
            b"snap",
            media_type="image/png",
            dependencies=[generated.id],
        )
    assert store.get_artifact(project.id, generated.id).read_bytes() == b"base"


def test_base_then_directions_gates_require_live_expected_stage_outputs(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    generated = put_stage_artifact(
        store,
        project.id,
        "base_generation",
        b"base",
        media_type="image/png",
        dependencies=[source.id],
    )
    snapped = put_stage_artifact(
        store,
        project.id,
        "base_snap",
        b"snap",
        media_type="image/png",
        dependencies=[generated.id],
    )
    directions = put_stage_artifact(
        store,
        project.id,
        "directional_anchors",
        b"directions",
        media_type="image/png",
        dependencies=[snapped.id],
    )

    with pytest.raises(PipelineError, match="base approval"):
        approve_gate(store, project.id, "directions", [directions.id])
    with pytest.raises(PipelineError, match="invalid input artifact ID"):
        approve_gate(store, project.id, "base", ["not-a-uuid"])

    base_approval = approve_gate(store, project.id, "base", [snapped.id])
    directions_approval = approve_gate(store, project.id, "directions", [directions.id])

    assert base_approval.artifact_hashes == [snapped.sha256]
    assert directions_approval.artifact_hashes == [directions.sha256]


def test_all_gates_and_base_replacement_only_stale_transitive_dependants(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    generated = put_stage_artifact(
        store,
        project.id,
        "base_generation",
        b"base",
        media_type="image/png",
        dependencies=[source.id],
    )
    snapped = put_stage_artifact(
        store,
        project.id,
        "base_snap",
        b"snap",
        media_type="image/png",
        dependencies=[generated.id],
    )
    directions = put_stage_artifact(
        store,
        project.id,
        "directional_anchors",
        b"directions",
        media_type="image/png",
        dependencies=[snapped.id],
    )
    poseboard = put_stage_artifact(
        store,
        project.id,
        "action_poseboards",
        b"poseboard",
        media_type="image/png",
        dependencies=[directions.id],
    )
    recovered = put_stage_artifact(
        store,
        project.id,
        "frame_recovery",
        b"recovered",
        media_type="image/png",
        dependencies=[poseboard.id],
    )
    per_frame = put_stage_artifact(
        store,
        project.id,
        "per_frame_snap",
        b"frame snap",
        media_type="image/png",
        dependencies=[recovered.id],
    )
    runtime = put_stage_artifact(
        store,
        project.id,
        "runtime_normalize",
        b"runtime",
        media_type="image/png",
        dependencies=[per_frame.id],
    )
    curation = store.put_artifact(
        project.id,
        b"curation",
        kind="curation",
        stage="curation",
        media_type="application/json",
        variant="raw",
        dependencies=[recovered.id],
    )
    qa = store.put_artifact(
        project.id,
        b"qa",
        kind="qa",
        stage="qa",
        media_type="application/json",
        variant="raw",
        dependencies=[curation.id],
    )
    unrelated = store.put_artifact(
        project.id,
        b"unrelated",
        kind="input",
        media_type="image/png",
        variant="raw",
    )

    with pytest.raises(PipelineError, match="base approval"):
        approve_gate(store, project.id, "directions", [directions.id])
    approve_gate(store, project.id, "base", [snapped.id])
    with pytest.raises(PipelineError, match="directions approval"):
        approve_gate(store, project.id, "frames", [recovered.id])
    approve_gate(store, project.id, "directions", [directions.id])
    with pytest.raises(PipelineError, match="frames approval"):
        approve_gate(store, project.id, "motion", [curation.id])
    approve_gate(store, project.id, "frames", [recovered.id])
    with pytest.raises(PipelineError, match="motion approval"):
        approve_gate(store, project.id, "export", [qa.id])
    approve_gate(store, project.id, "motion", [curation.id])
    with pytest.raises(PipelineError, match="wrong stage"):
        approve_gate(store, project.id, "export", [runtime.id])
    approve_gate(store, project.id, "export", [qa.id])

    replacement_generated = put_stage_artifact(
        store,
        project.id,
        "base_generation",
        b"replacement base",
        media_type="image/png",
        dependencies=[source.id],
    )
    replacement_snap = put_stage_artifact(
        store,
        project.id,
        "base_snap",
        b"replacement snap",
        media_type="image/png",
        dependencies=[replacement_generated.id],
    )

    approve_gate(store, project.id, "base", [replacement_snap.id])

    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}
    assert not artifacts[source.id].stale
    assert not artifacts[snapped.id].stale
    assert artifacts[directions.id].stale
    assert artifacts[poseboard.id].stale
    assert artifacts[recovered.id].stale
    assert artifacts[per_frame.id].stale
    assert artifacts[runtime.id].stale
    assert artifacts[curation.id].stale
    assert artifacts[qa.id].stale
    assert not artifacts[unrelated.id].stale
    assert not artifacts[replacement_snap.id].stale
    with pytest.raises(PipelineError, match="stale"):
        approve_gate(store, project.id, "directions", [directions.id])


def test_preflight_counts_only_requested_provider_work_without_fallback(monkeypatch) -> None:
    jobs_sentinel = ModuleType("ai_sprite_studio.jobs")

    def fail_if_touched(_: str) -> None:
        raise AssertionError("preflight must not import or use JobRunner")

    jobs_sentinel.__getattr__ = fail_if_touched  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ai_sprite_studio.jobs", jobs_sentinel)

    mirrored = preflight_requests(
        ProjectConfig(side_policy="mirror"),
        state_ids=["idle", "walk"],
        directions=["down", "right", "up", "left"],
    )
    independent = preflight_requests(
        ProjectConfig(side_policy="independent"),
        state_ids=["idle", "walk"],
        directions=["down", "right", "up", "left"],
    )
    unavailable = preflight_requests(
        ProjectConfig(side_policy="mirror"),
        state_ids=["idle", "walk"],
        directions=["down", "right", "up", "left"],
        motion_available=False,
    )

    mirrored_lines = {line.stage: line for line in mirrored.lines}
    independent_lines = {line.stage: line for line in independent.lines}
    unavailable_lines = {line.stage: line for line in unavailable.lines}
    assert mirrored.total == 9
    assert mirrored_lines["directional_anchors"].count == 2
    assert mirrored_lines["action_poseboards"].count == 3
    assert mirrored_lines["walk_i2v"].count == 3
    assert independent.total == 12
    assert independent_lines["directional_anchors"].count == 3
    assert independent_lines["action_poseboards"].count == 4
    assert independent_lines["walk_i2v"].count == 4
    assert unavailable.total == 6
    assert unavailable_lines["walk_i2v"].count == 0
    assert unavailable_lines["walk_i2v"].unavailable == 3
    assert not unavailable_lines["walk_i2v"].available


def test_preflight_treats_upload_and_local_stages_as_zero_requests() -> None:
    upload = StateSpec(
        id="emote",
        label="Emote",
        frames=3,
        fps=8,
        loop=False,
        generator="upload",
        motion="a wave",
    )
    project = ProjectConfig(states=[*ProjectConfig().states, upload])

    plan = preflight_requests(
        project,
        state_ids=["emote"],
        directions=[],
        include_base=False,
    )

    lines = {line.stage: line for line in plan.lines}
    assert plan.total == 0
    assert lines["upload"].count == 0
    assert all(lines[stage].count == 0 for stage in ("base_snap", "frame_recovery", "per_frame_snap", "runtime_normalize"))


def test_gate_rejects_a_predecessor_with_mismatched_stored_hash(monkeypatch, tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    generated = put_stage_artifact(
        store,
        project.id,
        "base_generation",
        b"base",
        media_type="image/png",
        dependencies=[source.id],
    )
    snapped = put_stage_artifact(
        store,
        project.id,
        "base_snap",
        b"snap",
        media_type="image/png",
        dependencies=[generated.id],
    )
    directions = put_stage_artifact(
        store,
        project.id,
        "directional_anchors",
        b"directions",
        media_type="image/png",
        dependencies=[snapped.id],
    )
    approve_gate(store, project.id, "base", [snapped.id])
    current = store.load(project.id)
    approval = current.approvals[0].model_copy(update={"artifact_hashes": ["0" * 64]})
    malformed = current.model_copy(update={"approvals": [approval]})
    monkeypatch.setattr(store, "load", lambda _: malformed)

    with pytest.raises(PipelineError, match="hash does not match"):
        approve_gate(store, project.id, "directions", [directions.id])


def test_gate_revalidates_generic_predecessor_approvals_against_pipeline_policy(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    snapped = _base_snap(store, project.id, source.id, b"valid")
    directions = _directions(store, project.id, snapped, b"directions")
    store.approve(project.id, "base", [source.id])

    with pytest.raises(PipelineError, match="base approval.*wrong stage"):
        approve_gate(store, project.id, "directions", [directions.id])

    wrong_project = store.create(ProjectConfig())
    wrong_source = store.put_artifact(
        wrong_project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    wrong_snap = store.put_artifact(
        wrong_project.id,
        b"wrong snap",
        kind="base_snap",
        stage="base_snap",
        media_type="image/png",
        variant="raw",
        dependencies=[wrong_source.id],
    )
    wrong_directions = _directions(store, wrong_project.id, wrong_snap, b"directions")
    store.approve(wrong_project.id, "base", [wrong_snap.id])

    with pytest.raises(PipelineError, match="base approval.*direct input"):
        approve_gate(store, wrong_project.id, "directions", [wrong_directions.id])


def test_stage_persistence_rechecks_dependencies_after_pipeline_preflight(monkeypatch, tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    generated = put_stage_artifact(
        store,
        project.id,
        "base_generation",
        b"base",
        media_type="image/png",
        dependencies=[source.id],
    )
    put_artifact = store.put_artifact

    def invalidate_after_preflight(*args, **kwargs):
        store.invalidate_dependants(project.id, [source.id])
        return put_artifact(*args, **kwargs)

    monkeypatch.setattr(store, "put_artifact", invalidate_after_preflight)

    with pytest.raises(ProjectStoreError, match="stale dependency"):
        put_stage_artifact(
            store,
            project.id,
            "base_snap",
            b"snap",
            media_type="image/png",
            dependencies=[generated.id],
        )


def test_replacement_failure_keeps_existing_approval_and_dependants_live(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    old_snap = _base_snap(store, project.id, source.id, b"old")
    old_directions = _directions(store, project.id, old_snap, b"old directions")
    old_approval = approve_gate(store, project.id, "base", [old_snap.id])
    replacement = _base_snap(store, project.id, source.id, b"replacement")
    store.get_artifact(project.id, replacement.id).write_bytes(b"corrupt replacement")

    with pytest.raises(ProjectStoreError, match="hash"):
        approve_gate(store, project.id, "base", [replacement.id])

    loaded = store.load(project.id)
    artifacts = {artifact.id: artifact for artifact in loaded.artifacts}
    assert loaded.approvals == [old_approval]
    assert not artifacts[old_directions.id].stale


def test_reordered_approval_selection_does_not_invalidate_descendants(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    first_snap = _base_snap(store, project.id, source.id, b"first")
    second_snap = _base_snap(store, project.id, source.id, b"second")
    first_directions = _directions(store, project.id, first_snap, b"first directions")
    second_directions = _directions(store, project.id, second_snap, b"second directions")
    approve_gate(store, project.id, "base", [first_snap.id, second_snap.id])

    approve_gate(store, project.id, "base", [second_snap.id, first_snap.id])

    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}
    assert not artifacts[first_directions.id].stale
    assert not artifacts[second_directions.id].stale


def test_partial_approval_replacement_invalidates_only_removed_roots(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )
    removed_snap = _base_snap(store, project.id, source.id, b"removed")
    retained_snap = _base_snap(store, project.id, source.id, b"retained")
    replacement_snap = _base_snap(store, project.id, source.id, b"replacement")
    removed_directions = _directions(store, project.id, removed_snap, b"removed directions")
    retained_directions = _directions(store, project.id, retained_snap, b"retained directions")
    approve_gate(store, project.id, "base", [removed_snap.id, retained_snap.id])

    approve_gate(store, project.id, "base", [retained_snap.id, replacement_snap.id])

    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}
    assert artifacts[removed_directions.id].stale
    assert not artifacts[retained_directions.id].stale
