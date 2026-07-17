import sys
from types import ModuleType
from uuid import uuid4

import pytest

from ai_sprite_studio.contracts import ProjectConfig, StateSpec
from ai_sprite_studio.pipeline import PipelineError, approve_gate, preflight_requests, put_stage_artifact
from ai_sprite_studio.project_store import ProjectStore


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
