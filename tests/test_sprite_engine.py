import io
import inspect
import json

import pytest
from PIL import Image, ImageDraw

from ai_sprite_studio.contracts import ProjectConfig
from ai_sprite_studio.project_store import ProjectStore
from ai_sprite_studio.sprite_engine import SpriteEngine, SpriteEngineError


MAGENTA = (255, 0, 255, 255)


def _strip_png(*, frames=1, fused=False, detached=False):
    image = Image.new("RGBA", (64 * frames, 64), MAGENTA)
    draw = ImageDraw.Draw(image)
    colors = [
        (16, 72, 24, 255),
        (24, 100, 32, 255),
        (32, 128, 40, 255),
        (40, 156, 48, 255),
        (48, 184, 56, 255),
        (56, 212, 64, 255),
        (72, 96, 168, 255),
        (96, 112, 192, 255),
        (120, 128, 216, 255),
        (144, 144, 240, 255),
        (168, 96, 72, 255),
        (192, 112, 80, 255),
        (216, 128, 88, 255),
        (240, 144, 96, 255),
        (248, 184, 104, 255),
    ]
    for frame in range(frames):
        left = frame * 64 + 14 + (frame % 2) * 2
        for y in range(4, 60, 2):
            for x in range(left, left + 40, 2):
                draw.rectangle(
                    (x, y, x + 1, y + 1),
                    fill=colors[((x - left) // 2 + (y - 4) // 2) % len(colors)],
                )
        # A chroma-adjacent fringe must be cleaned without turning alpha soft.
        for y in range(8, 58, 2):
            image.putpixel((left - 1, y), (255, 96, 255, 255))
    if fused:
        draw.rectangle((54, 46, 80, 47), fill=colors[0])
    if detached:
        draw.rectangle((0, 18, 4, 23), fill=(1, 2, 3, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _project_engine(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Upload ranger"))
    return store, project, SpriteEngine(store)


def _prepared_engine(tmp_path, *, frames=2, fused=False, detached=False):
    store, project, engine = _project_engine(tmp_path)
    source = _strip_png(frames=frames, fused=fused, detached=detached)
    uploaded = engine.ingest_upload(
        project.id, source, media_type="image/png", filename="ranger.png"
    )
    prepared = engine.prepare(project.id, uploaded.id, frames=frames)
    return store, project, engine, source, uploaded, prepared


def _rgba(data):
    with Image.open(io.BytesIO(data)) as opened:
        return opened.convert("RGBA")


def _assert_canonical(frame):
    assert frame.mode == "RGBA"
    assert frame.size == (256, 256)
    assert set(frame.getchannel("A").get_flattened_data()) <= {0, 255}
    opaque = {pixel[:3] for pixel in frame.get_flattened_data() if pixel[3] == 255}
    assert 1 <= len(opaque) <= 12
    assert max(y for y in range(frame.height) if any(
        frame.getpixel((x, y))[3] for x in range(frame.width)
    )) == 255
    for y in range(0, 256, 2):
        for x in range(0, 256, 2):
            block = [
                frame.getpixel((x + dx, y + dy))
                for dy in range(2)
                for dx in range(2)
            ]
            if any(pixel[3] for pixel in block):
                assert len(set(block)) == 1


def test_local_upload_snap_curate_atlas_and_inspect_preserves_immutable_variants(tmp_path):
    store, project, engine, source, uploaded, prepared = _prepared_engine(tmp_path)

    assert store.read_artifact_bytes(project.id, uploaded.id) == source
    assert prepared.selected_chroma != "#00FF00"
    assert prepared.outputs == ("raw/upload.png", "sprite-request.json")
    request = json.loads((store.run_dir(project.id) / "sprite-request.json").read_text())
    assert request["states"] == {
        "upload": {"frames": 2, "fps": 1, "loop": False, "action": "uploaded source"}
    }
    assert request["cell"] == {
        "shape": "square",
        "width": 256,
        "height": 256,
        "safe_margin_x": 0,
        "safe_margin_y": 0,
        "size": 256,
        "safe_margin": 0,
    }
    assert request["fit"] == {
        "resample": "kcentroid",
        "align_x": "alpha-centroid",
        "align_y": "bottom",
        "pixel_perfect": True,
        "logical_height": 128,
        "palette_size": 12,
        "outline": False,
        "conform": False,
    }

    extracted = engine.extract(project.id)
    assert extracted.raw_artifact_id == uploaded.id
    assert len(extracted.plain_artifact_ids) == len(extracted.pixel_artifact_ids) == 2
    assert extracted.preview_artifact_ids
    assert extracted.diagnostics["ok"] is True
    assert str(store.run_dir(project.id)) not in json.dumps(extracted.diagnostics)
    for artifact_id in extracted.pixel_artifact_ids:
        _assert_canonical(_rgba(store.read_artifact_bytes(project.id, artifact_id)))
    assert store.read_artifact_bytes(project.id, uploaded.id) == source
    assert (store.run_dir(project.id) / "raw/upload.png").read_bytes() == source
    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}
    assert all(
        artifacts[artifact_id].dependencies == [uploaded.id]
        for artifact_id in (*extracted.plain_artifact_ids, *extracted.pixel_artifact_ids)
    )
    assert all(
        artifacts[artifact_id].dependencies == list(extracted.pixel_artifact_ids)
        for artifact_id in extracted.preview_artifact_ids
    )

    snapshot = engine.load_curation(project.id)
    with pytest.raises(SpriteEngineError, match="stale curation revision"):
        engine.stamp_curation(
            project.id,
            {"version": 1, "kind": "sprite-gen-curation", "runRevision": "stale", "states": {}},
        )
    curated = engine.stamp_curation(
        project.id,
        {
            "version": 1,
            "kind": "sprite-gen-curation",
            "runRevision": snapshot.run_revision,
            "pixel_perfect": True,
            "states": {
                "upload": {
                    "selected": [1, 0],
                    "order": [1, 0],
                    "transforms": {"1": {"dx": 2, "dy": 0}},
                }
            },
        },
    )
    assert curated.run_revision == engine.load_curation(project.id).run_revision
    assert curated.selected["upload"] == (1, 0)
    pixel_before = store.read_artifact_bytes(project.id, extracted.pixel_artifact_ids[1])

    composed = engine.compose(project.id)
    inspected = engine.inspect(project.id)
    assert composed.atlas_artifact_id
    assert composed.manifest_artifact_id
    assert inspected.summary["ok"] is True
    assert store.read_artifact_bytes(project.id, extracted.pixel_artifact_ids[1]) == pixel_before
    manifest = json.loads(store.read_artifact_bytes(project.id, composed.manifest_artifact_id))
    assert manifest["animation"]["rows"]["upload"]["frames"] == 2
    assert manifest["animation"]["rows"]["upload"]["frame_variant"] == "pixel"


def test_components_fail_for_fused_poses_without_slot_fallback_but_projection_is_explicit(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2, fused=True)

    with pytest.raises(SpriteEngineError, match="sprite extraction failed") as failure:
        engine.extract(project.id)

    assert "could not extract 2 sprite components" in json.dumps(failure.value.diagnostics)
    assert str(store.run_dir(project.id)) not in str(failure.value)
    projected = engine.extract(project.id, segmentation="projection")
    assert len(projected.pixel_artifact_ids) == 2
    assert projected.diagnostics["segmentation"] == "projection"


def test_detached_accessory_is_not_used_as_a_slot_fallback(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1, detached=True)

    extracted = engine.extract(project.id)

    frame = _rgba(store.read_artifact_bytes(project.id, extracted.pixel_artifact_ids[0]))
    assert (1, 2, 3) not in {pixel[:3] for pixel in frame.get_flattened_data() if pixel[3]}


def test_ingestion_rejects_untrusted_or_non_single_frame_uploads(tmp_path):
    _, project, engine = _project_engine(tmp_path)
    valid = _strip_png()
    animated = io.BytesIO()
    first = Image.new("RGBA", (8, 8), MAGENTA)
    second = Image.new("RGBA", (8, 8), (0, 0, 0, 255))
    first.save(animated, format="PNG", save_all=True, append_images=[second])
    oversized = io.BytesIO()
    Image.new("RGBA", (4001, 4000), MAGENTA).save(oversized, format="PNG")

    for data, media_type, filename in (
        (b"", "image/png", "empty.png"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", "sprite.svg"),
        (valid, "image/jpeg", "sprite.jpg"),
        (valid, "image/png", "../sprite.png"),
        (animated.getvalue(), "image/png", "animated.png"),
        (oversized.getvalue(), "image/png", "huge.png"),
    ):
        with pytest.raises(SpriteEngineError, match="upload"):
            engine.ingest_upload(project.id, data, media_type=media_type, filename=filename)


@pytest.mark.parametrize(
    ("image_format", "media_type", "filename"),
    (("PNG", "image/png", "sprite.png"), ("JPEG", "image/jpeg", "sprite.jpg"), ("WEBP", "image/webp", "sprite.webp")),
)
def test_ingestion_accepts_each_declared_single_frame_raster(tmp_path, image_format, media_type, filename):
    store, project, engine = _project_engine(tmp_path)
    encoded = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 255)).save(encoded, format=image_format)

    artifact = engine.ingest_upload(
        project.id, encoded.getvalue(), media_type=media_type, filename=filename
    )

    assert artifact.media_type == media_type
    assert store.read_artifact_bytes(project.id, artifact.id) == encoded.getvalue()


def test_prepare_rechecks_an_input_artifact_before_it_reaches_the_run_directory(tmp_path):
    store, project, engine = _project_engine(tmp_path)
    unsafe = store.put_artifact(
        project.id,
        b"not an image",
        kind="input",
        media_type="image/png",
        variant="raw",
    )

    with pytest.raises(SpriteEngineError, match="invalid upload"):
        engine.prepare(project.id, unsafe.id)

    assert not list(store.run_dir(project.id).iterdir())


def test_engine_stays_local_and_never_reprepares_a_live_run(tmp_path):
    _, project, engine, _, uploaded, _ = _prepared_engine(tmp_path, frames=1)

    with pytest.raises(SpriteEngineError, match="run is already prepared"):
        engine.prepare(project.id, uploaded.id)

    import ai_sprite_studio.sprite_engine as sprite_engine

    source = inspect.getsource(sprite_engine).lower()
    assert "openai" not in source
    assert "higgsfield" not in source
    assert "allow_slot_fallback=true" not in source
