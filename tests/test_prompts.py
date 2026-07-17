from hashlib import sha256
from io import BytesIO
import re
from uuid import uuid4

from PIL import Image
import pytest

import ai_sprite_studio.prompts as prompt_module
from ai_sprite_studio.contracts import ProjectConfig
from ai_sprite_studio.project_store import ProjectStore, ProjectStoreError
from ai_sprite_studio.prompts import (
    PromptError,
    RenderedPrompt,
    STAGES,
    guide_for_stage,
    list_stages,
    load_source,
    parse_provenance,
    persist_prompt,
    render_prompt,
)


def test_registry_exposes_the_eight_locked_stages() -> None:
    assert tuple(STAGES) == (
        "base_generation",
        "base_snap",
        "directional_anchors",
        "action_poseboards",
        "frame_recovery",
        "walk_i2v",
        "per_frame_snap",
        "runtime_normalize",
    )
    assert [stage.source_path for stage in list_stages()] == [
        "prompts/01-south-anchor.md",
        "prompts/02-pixel-snap.md",
        "prompts/03-directional-anchors.md",
        "prompts/04-action-spritesheet.md",
        "prompts/05-frame-recovery.md",
        "prompts/06-walk-cycle-i2v.md",
        "prompts/07-per-frame-snap.md",
        "prompts/08-runtime-normalize-and-align.md",
    ]


def test_locked_sources_load_from_package_assets_with_their_pinned_hashes() -> None:
    for stage in list_stages():
        source = load_source(stage.key)
        assert sha256(source.encode()).hexdigest() == stage.source_sha256


def test_rendered_base_prompt_has_locked_context_and_provenance() -> None:
    project = ProjectConfig(chroma="#123456")

    rendered = render_prompt(
        "base_generation",
        project,
        {
            "CORE_IDENTITY": "a cheerful courier",
            "COSTUME_AND_PALETTE": "blue coat and brass buttons",
            "SILHOUETTE_NOTES": "wide satchel and short boots",
        },
        direction="down",
        state="idle",
    )

    assert "a cheerful courier" in rendered.text
    assert "blue coat and brass buttons" in rendered.text
    assert "wide satchel and short boots" in rendered.text
    assert "{CORE_IDENTITY}" not in rendered.text
    assert rendered.provenance["stage_key"] == "base_generation"
    assert rendered.provenance["source_sha256"] == STAGES["base_generation"].source_sha256
    assert rendered.provenance["canonical_substitutions"] == {
        "CORE_IDENTITY": "a cheerful courier",
        "COSTUME_AND_PALETTE": "blue coat and brass buttons",
        "SILHOUETTE_NOTES": "wide satchel and short boots",
    }
    assert rendered.provenance["locked_context"]["chroma"] == "#123456"
    assert rendered.provenance["locked_context"]["state"]["motion"] == "breathing in place"
    assert parse_provenance(rendered.text) == rendered.provenance
    assert rendered.provenance["rendered_sha256"] == rendered.sha256


def test_cross_reference_corrections_are_rendered_without_editing_sources() -> None:
    project = ProjectConfig()

    directions = render_prompt(
        "directional_anchors",
        project,
        {
            "DIRECTION": "right",
            "DIRECTION_DESCRIPTION": "profile view, facing screen-right",
        },
        direction="right",
    )
    snapped = render_prompt("base_snap", project, {})
    recovery = render_prompt("frame_recovery", project, {}, state="attack")

    assert directions.provenance["correction_ids"] == ["direction-policy-right-up-left"]
    assert "Generate right and up anchors from the snapped south." in directions.text
    assert "Generate west and north anchors from the snapped south." not in directions.text
    assert "Directional anchors NSEW" not in directions.text
    assert "screen-left" not in directions.text
    assert "For mirrored projects, left is a horizontal flip of right" in directions.text
    assert "For independent-side projects, generate left separately." in directions.text
    assert "![W canonical reference]" not in directions.text
    assert "![Right canonical reference]" in directions.text
    assert snapped.provenance["correction_ids"] == ["direction-policy-right-up-left"]
    assert "N/S/E/W" not in snapped.text
    assert "For mirrored projects, left is derived by mirroring right." in snapped.text
    assert recovery.provenance["correction_ids"] == ["frame-recovery-next-stage"]
    assert "Feed these to file 07 (per-frame chroma-layout snap)." in recovery.text
    assert "Feed these to file 06 (per-frame chroma-layout snap)." in load_source("frame_recovery")


def test_required_guides_are_rgb_metadata_free_checkerboards() -> None:
    for stage_key, size in {
        "directional_anchors": (1024, 1024),
        "action_poseboards": (2048, 1536),
    }.items():
        guide = guide_for_stage(stage_key)

        assert guide is not None
        assert (guide.width, guide.height) == size
        prompt_module._checkerboard_guide.cache_clear()
        assert guide.data == guide_for_stage(stage_key).data
        assert guide.sha256 == sha256(guide.data).hexdigest()

        with Image.open(BytesIO(guide.data)) as image:
            image.load()
            assert image.mode == "RGB"
            assert image.size == size
            assert image.info == {}
            assert image.getpixel((0, 0)) == (0, 0, 0)
            assert image.getpixel((1, 0)) == (255, 255, 255)
            assert image.getpixel((0, 1)) == (255, 255, 255)


def test_rendered_guided_stages_record_the_guide_digest() -> None:
    project = ProjectConfig()
    rendered = render_prompt(
        "directional_anchors",
        project,
        {
            "DIRECTION": "right",
            "DIRECTION_DESCRIPTION": "profile view, facing screen-right",
        },
        direction="right",
    )
    guide = guide_for_stage("directional_anchors")

    assert rendered.provenance["guide"] == {
        "width": guide.width,
        "height": guide.height,
        "sha256": guide.sha256,
    }


def test_rendering_rejects_non_string_unknown_substitution_keys() -> None:
    with pytest.raises(PromptError, match="unknown prompt substitutions"):
        render_prompt("base_snap", ProjectConfig(), {1: "unused"})  # type: ignore[dict-item]


def test_rendering_rejects_missing_and_unused_substitutions() -> None:
    with pytest.raises(PromptError, match="missing prompt substitutions"):
        render_prompt("base_generation", ProjectConfig(), {})
    with pytest.raises(PromptError, match="unknown prompt substitutions"):
        render_prompt("base_snap", ProjectConfig(), {"CORE_IDENTITY": "unused"})


def test_rendered_prompt_persists_as_one_immutable_contained_record(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    rendered = render_prompt("base_snap", project, {})

    record = persist_prompt(store, project.id, uuid4(), rendered)

    assert record["stage_key"] == "base_snap"
    assert store.load_prompt(project.id, record["job_id"]) == record
    with pytest.raises(ProjectStoreError, match="overwrite"):
        store.save_prompt(project.id, record["job_id"], record)


def test_persist_prompt_rejects_a_self_consistent_forged_project_context(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    rendered = render_prompt("base_snap", project, {})
    body = rendered.text.partition("\n")[2]
    forged_provenance = {
        **rendered.provenance,
        "locked_context": {**rendered.provenance["locked_context"], "chroma": "#123456"},
    }
    forged = RenderedPrompt(
        text=(
            prompt_module._HEADER_PREFIX
            + prompt_module._json(forged_provenance)
            + prompt_module._HEADER_SUFFIX
            + "\n"
            + body
        ),
        provenance=forged_provenance,
        sha256=rendered.sha256,
    )

    with pytest.raises(PromptError, match="canonical"):
        persist_prompt(store, project.id, uuid4(), forged)

    assert not list((tmp_path / "projects" / str(project.id) / "prompts").glob("*.json"))


def test_persist_prompt_rejects_reserved_provenance_keys(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    rendered = render_prompt("base_snap", project, {})
    body = rendered.text.partition("\n")[2]
    forged_provenance = {**rendered.provenance, "text": "forged record text"}
    forged = RenderedPrompt(
        text=(
            prompt_module._HEADER_PREFIX
            + prompt_module._json(forged_provenance)
            + prompt_module._HEADER_SUFFIX
            + "\n"
            + body
        ),
        provenance=forged_provenance,
        sha256=rendered.sha256,
    )

    with pytest.raises(PromptError, match="reserved"):
        persist_prompt(store, project.id, uuid4(), forged)


def test_direction_substitutions_match_context_and_mirror_policy() -> None:
    mirrored = ProjectConfig(side_policy="mirror")
    independent = ProjectConfig(side_policy="independent")

    with pytest.raises(PromptError, match="must match"):
        render_prompt(
            "walk_i2v",
            mirrored,
            {"DURATION": 4, "DIRECTION": "right"},
            direction="up",
            state="walk",
        )
    with pytest.raises(PromptError, match="derive left"):
        render_prompt(
            "directional_anchors",
            mirrored,
            {
                "DIRECTION": "left",
                "DIRECTION_DESCRIPTION": "profile view, facing screen-left",
            },
            direction="left",
        )
    with pytest.raises(PromptError, match="derive left"):
        render_prompt(
            "action_poseboards",
            mirrored,
            {
                "ACTION": "attack",
                "DIRECTION_DESCRIPTION": "profile view, facing left",
                "COLS": 4,
                "ROWS": 3,
                "N": 4,
                "CANVAS_W": 2048,
                "CANVAS_H": 1536,
                "FRAME_BY_FRAME_DESCRIPTION": "Frame 1: ready stance. Frame 2: strike.",
                "ACTION_SPECIFIC_CONSTRAINTS": "Keep the weapon readable.",
            },
            direction="left",
            state="attack",
        )
    independent_left = render_prompt(
        "directional_anchors",
        independent,
        {
            "DIRECTION": "left",
            "DIRECTION_DESCRIPTION": "profile view, facing left",
        },
        direction="left",
    )

    assert independent_left.provenance["locked_context"]["direction"] == "left"
    assert "For independent-side projects, generate left separately." in independent_left.text


def test_action_poseboards_require_and_canonicalize_their_direction() -> None:
    independent = ProjectConfig(side_policy="independent")
    substitutions = {
        "ACTION": "attack",
        "DIRECTION_DESCRIPTION": "profile view, facing screen-right",
        "COLS": 4,
        "ROWS": 3,
        "N": 4,
        "CANVAS_W": 2048,
        "CANVAS_H": 1536,
        "FRAME_BY_FRAME_DESCRIPTION": "Frame 1: ready stance. Frame 2: strike.",
        "ACTION_SPECIFIC_CONSTRAINTS": "Keep the weapon readable.",
    }

    with pytest.raises(PromptError, match="explicit direction"):
        render_prompt("action_poseboards", independent, substitutions, state="attack")
    with pytest.raises(PromptError, match="description"):
        render_prompt(
            "action_poseboards",
            independent,
            {**substitutions, "DIRECTION_DESCRIPTION": "profile view, facing screen-left"},
            direction="right",
            state="attack",
        )

    rendered = render_prompt(
        "action_poseboards",
        independent,
        {**substitutions, "DIRECTION_DESCRIPTION": "profile view, facing screen-left"},
        direction="left",
        state="attack",
    )

    assert rendered.provenance["locked_context"]["direction"] == "left"


@pytest.mark.parametrize(
    "record",
    [
        [],
        {"unsafe": object()},
        {"unsafe": float("nan")},
        {1: "unsafe key"},
    ],
)
def test_prompt_store_rejects_non_object_or_unsafe_records(tmp_path, record) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())

    with pytest.raises(ProjectStoreError):
        store.save_prompt(project.id, uuid4(), record)  # type: ignore[arg-type]
    with pytest.raises(ProjectStoreError, match="invalid job ID"):
        store.save_prompt(project.id, "not-a-uuid", {})


def test_rendered_prompt_records_canonical_artifact_input_pairs(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig())
    source = store.put_artifact(
        project.id,
        b"identity",
        kind="input",
        media_type="image/png",
        variant="raw",
    )

    rendered = render_prompt(
        "base_generation",
        store.load(project.id),
        {
            "CORE_IDENTITY": "a courier",
            "COSTUME_AND_PALETTE": "blue coat",
            "SILHOUETTE_NOTES": "wide satchel",
        },
        artifact_inputs=[source],
    )

    assert rendered.provenance["artifact_inputs"] == [
        {"id": str(source.id), "sha256": source.sha256}
    ]


def test_all_stage_renderings_have_stable_semantic_snapshots() -> None:
    project = ProjectConfig(chroma="#123456")
    requests = {
        "base_generation": (
            {
                "CORE_IDENTITY": "a cheerful courier",
                "COSTUME_AND_PALETTE": "blue coat and brass buttons",
                "SILHOUETTE_NOTES": "wide satchel and short boots",
            },
            "down",
            "idle",
        ),
        "base_snap": ({}, "down", "idle"),
        "directional_anchors": (
            {
                "DIRECTION": "right",
                "DIRECTION_DESCRIPTION": "profile view, facing screen-right",
            },
            "right",
            "idle",
        ),
        "action_poseboards": (
            {
                "ACTION": "attack",
                "DIRECTION_DESCRIPTION": "profile view, facing screen-right",
                "COLS": 4,
                "ROWS": 3,
                "N": 4,
                "CANVAS_W": 2048,
                "CANVAS_H": 1536,
                "FRAME_BY_FRAME_DESCRIPTION": "Frame 1: ready stance. Frame 2: strike.",
                "ACTION_SPECIFIC_CONSTRAINTS": "Keep the weapon readable.",
            },
            "right",
            "attack",
        ),
        "frame_recovery": ({}, "right", "attack"),
        "walk_i2v": ({"DURATION": 4, "DIRECTION": "right"}, "right", "walk"),
        "per_frame_snap": ({}, "right", "attack"),
        "runtime_normalize": ({}, "right", "attack"),
    }

    actual: dict[str, str] = {}
    for stage_key, (substitutions, direction, state) in requests.items():
        rendered = render_prompt(
            stage_key,
            project,
            substitutions,
            direction=direction,
            state=state,
        )
        assert not re.search(r"\{[A-Z][A-Z0-9_]*\}", rendered.text)
        assert rendered.provenance["source_sha256"] == STAGES[stage_key].source_sha256
        actual[stage_key] = rendered.sha256

    assert actual == {
        "base_generation": "10995a4a5c59aac8be97304b9931929d99e17cf674fe0191cdaebd1606d484e7",
        "base_snap": "e47e852431f7279090a1b081a942973515edf8b62190d8f9b957ac273e0e9a29",
        "directional_anchors": "cbfdc615eaec5030bbbf817cb9fb9594f9dc2435062525c35249ac1e746b72cb",
        "action_poseboards": "2c244bd33bb8a48171c510081bc70fb14e8624026f6aa5463fbf3cd55e97fd48",
        "frame_recovery": "c7622d314cbdbc40eafa37cc1c11cd222317bd78dc4329a5c9ef191252638f71",
        "walk_i2v": "4b11cdb5fa5c29e7303e9bd5a0f5e463ba526cc1a252319b79a205b1ed8e9425",
        "per_frame_snap": "d7ae298047bd71209d540868c01fbee314bd46c68f8e97b288a4f3b5fa6d3e0e",
        "runtime_normalize": "bd078b781c9fa78994e96b2718b3592f9adde200e7d66f52a9419775c31637a0",
    }
