from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_sprite_studio.contracts import (
    ArtifactRef,
    CellSpec,
    JobCommand,
    JobRecord,
    ProjectConfig,
    StateSpec,
)


def test_project_defaults_are_the_locked_top_down_preset():
    project = ProjectConfig()

    assert project.schema_version == 1
    assert project.directions == ["down", "right", "up", "left"]
    assert project.side_policy == "mirror"
    assert project.cell == CellSpec()
    assert project.palette_size == 12
    assert project.binary_alpha is True
    assert project.chroma == "#00FF00"
    assert project.candidates_per_request == 1
    assert [(state.id, state.frames, state.fps, state.loop, state.generator) for state in project.states] == [
        ("idle", 4, 6, True, "gpt_poseboard"),
        ("walk", 8, 10, True, "higgsfield_i2v"),
        ("attack", 4, 12, False, "gpt_poseboard"),
        ("hurt", 4, 10, False, "gpt_poseboard"),
        ("jump", 4, 10, False, "gpt_poseboard"),
        ("death", 6, 8, False, "gpt_poseboard"),
    ]


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"width": 128}, "width"),
        ({"height": 128}, "height"),
        ({"logical_size": 32}, "logical_size"),
        ({"anchor_x": 0}, "anchor_x"),
        ({"anchor_y": 0}, "anchor_y"),
    ],
)
def test_cell_spec_rejects_any_unlocked_dimensions_or_anchor(kwargs, field):
    with pytest.raises(ValidationError, match=field):
        CellSpec(**kwargs)


@pytest.mark.parametrize(
    "relative_path",
    ["", "/tmp/artifact.png", "../artifact.png", "candidates/../artifact.png", "C:/outside.png"],
)
def test_artifacts_reject_unsafe_relative_paths(relative_path):
    with pytest.raises(ValidationError, match="safe relative path"):
        ArtifactRef(
            id=uuid4(),
            kind="base",
            relative_path=relative_path,
            sha256="a" * 64,
            media_type="image/png",
            width=None,
            height=None,
            variant="raw",
            source_job_id=None,
            dependencies=[],
        )


def test_project_rejects_artifacts_with_unknown_dependencies():
    artifact = ArtifactRef(
        id=uuid4(),
        kind="base",
        relative_path="candidates/base/example/raw.png",
        sha256="a" * 64,
        media_type="image/png",
        width=None,
        height=None,
        variant="raw",
        source_job_id=None,
        dependencies=[uuid4()],
    )

    with pytest.raises(ValidationError, match="unknown artifact"):
        ProjectConfig(artifacts=[artifact])


def test_core_states_cannot_drift_from_the_preset():
    with pytest.raises(ValidationError, match="idle"):
        StateSpec(
            id="idle",
            label="Idle",
            frames=5,
            fps=6,
            loop=True,
            generator="gpt_poseboard",
            motion="breathing in place",
        )


def test_projects_keep_the_six_core_states_when_custom_states_are_added():
    custom = StateSpec(
        id="celebrate",
        label="Celebrate",
        frames=2,
        fps=8,
        loop=False,
        generator="upload",
        motion="raises both hands",
    )

    with pytest.raises(ValidationError, match="core states"):
        ProjectConfig(states=[custom])


@pytest.mark.parametrize(
    ("frames", "fps"),
    [(1, 1), (12, 30)],
)
def test_custom_states_allow_the_documented_boundaries(frames, fps):
    state = StateSpec(
        id="celebrate",
        label="Celebrate",
        frames=frames,
        fps=fps,
        loop=False,
        generator="upload",
        motion="raises both hands",
    )

    assert state.frames == frames
    assert state.fps == fps


@pytest.mark.parametrize(("frames", "fps"), [(0, 10), (129, 10), (2, 0), (2, 31)])
def test_custom_states_reject_outside_the_documented_boundaries(frames, fps):
    with pytest.raises(ValidationError):
        StateSpec(
            id="celebrate",
            label="Celebrate",
            frames=frames,
            fps=fps,
            loop=True,
            generator="upload",
            motion="raises both hands",
        )


def test_public_records_require_uuid_ids_and_locked_job_commands():
    artifact = ArtifactRef(
        id=uuid4(),
        kind="base",
        relative_path="candidates/base/example/raw.png",
        sha256="a" * 64,
        media_type="image/png",
        width=256,
        height=256,
        variant="raw",
        source_job_id=None,
        dependencies=[],
    )
    record = JobRecord(
        id=uuid4(),
        project_id=uuid4(),
        command=JobCommand.GENERATE_BASE,
        status="queued",
        payload={},
        input_hash="b" * 64,
        provider_request_ids=[],
        output_artifact_ids=[artifact.id],
        progress=0,
        error=None,
    )

    assert record.command is JobCommand.GENERATE_BASE
    with pytest.raises(ValidationError):
        ArtifactRef(
            id="not-a-uuid",
            kind="base",
            relative_path="candidates/base/example/raw.png",
            sha256="a" * 64,
            media_type="image/png",
            width=None,
            height=None,
            variant="raw",
            source_job_id=None,
            dependencies=[],
        )


def test_job_command_and_status_vocabularies_are_complete():
    assert [command.value for command in JobCommand] == [
        "normalize_input",
        "generate_base",
        "edit_candidate",
        "snap_candidate",
        "generate_directions",
        "generate_poseboards",
        "generate_walk",
        "ingest_walk_video",
        "recover_frames",
        "save_curation",
        "ai_edit_frame",
        "run_qa",
        "export_pack",
    ]
    for status in (
        "queued",
        "running",
        "waiting_approval",
        "succeeded",
        "failed",
        "cancel_requested",
        "canceled",
        "attention_required",
    ):
        JobRecord(
            id=uuid4(),
            project_id=uuid4(),
            command=JobCommand.RUN_QA,
            status=status,
            payload={},
            input_hash="b" * 64,
            provider_request_ids=[],
            output_artifact_ids=[],
            progress=0,
            error=None,
        )
