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


def _acquire_engine_mutation_lock(run_dir, attempting, entered):
    from pathlib import Path

    from ai_sprite_studio.sprite_engine import _engine_mutation_guard

    attempting.set()
    with _engine_mutation_guard(Path(run_dir)):
        entered.set()


def _compose_then_hold(workspace, project_id, finished, release):
    SpriteEngine(ProjectStore(workspace)).compose(project_id)
    finished.set()
    if not release.wait(timeout=10):
        raise RuntimeError("test did not release the first compose process")


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


def _change_live_pixel_frame(path):
    with Image.open(path) as opened:
        frame = opened.convert("RGBA")
    for y in range(0, 256, 2):
        for x in range(0, 256, 2):
            color = frame.getpixel((x, y))
            if color[3]:
                changed = ((color[0] + 1) % 256, color[1], color[2], 255)
                for dy in range(2):
                    for dx in range(2):
                        frame.putpixel((x + dx, y + dy), changed)
                frame.save(path)
                return
    raise AssertionError("fixture did not contain a pixel frame block")


def _change_live_request(path):
    request = json.loads(path.read_text())
    request["sprite_engine_test_drift"] = True
    path.write_text(json.dumps(request, sort_keys=True) + "\n")


def _write_matching_curation_sidecar(store, project, data):
    run_dir = store.run_dir(project.id)
    sidecar = run_dir / "curation.json"
    state = json.loads((run_dir / "sprite-engine.json").read_text())
    artifact = store.put_artifact(
        project.id,
        data,
        kind="sprite_curation",
        media_type="application/json",
        variant="raw",
        stage="sprite_upload",
        dependencies=state["pixel_artifact_ids"],
    )
    sidecar.write_bytes(data)
    state["curation_artifact_id"] = str(artifact.id)
    (run_dir / "sprite-engine.json").write_text(json.dumps(state, sort_keys=True) + "\n")
    return sidecar


def test_extract_rejects_a_palette_spread_across_pixel_frames(tmp_path, monkeypatch):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)

    import ai_sprite_studio.sprite_engine as sprite_engine

    upstream_extract = sprite_engine.sprite_extract.run

    def extract_with_disjoint_palettes(*args, **kwargs):
        result = upstream_extract(*args, **kwargs)
        run_dir = kwargs["run_dir"]
        manifest = json.loads((run_dir / "frames/frames-manifest.json").read_text())
        row = next(item for item in manifest["rows"] if item["state"] == "upload")
        palettes = (
            ((1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12), (13, 14, 15), (16, 17, 18), (19, 20, 21)),
            ((31, 32, 33), (34, 35, 36), (37, 38, 39), (40, 41, 42), (43, 44, 45), (46, 47, 48), (49, 50, 51)),
        )
        for relative_path, palette in zip(row["files"], palettes, strict=True):
            path = run_dir / relative_path
            with Image.open(path) as opened:
                frame = opened.convert("RGBA")
            pixels = frame.load()
            color_index = 0
            for y in range(0, 256, 2):
                for x in range(0, 256, 2):
                    if pixels[x, y][3]:
                        color = (*palette[color_index % len(palette)], 255)
                        color_index += 1
                        for dy in range(2):
                            for dx in range(2):
                                pixels[x + dx, y + dy] = color
            frame.save(path)
        return result

    monkeypatch.setattr(sprite_engine.sprite_extract, "run", extract_with_disjoint_palettes)

    with pytest.raises(SpriteEngineError, match="shared palette"):
        engine.extract(project.id)

    assert not [
        artifact
        for artifact in store.load(project.id).artifacts
        if artifact.kind == "sprite_frame" and artifact.variant == "pixel"
    ]


def test_stamp_curation_rejects_noncanonical_upload_edits(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision

    payloads = (
        {"pixel_perfect": False, "states": {"upload": {"selected": [0, 1]}}},
        {"states": {"upload": {"pixel_perfect": False, "selected": [0, 1]}}},
        {"states": {"upload": {"selected": [0, 1, 2], "clones": {"2": 0}}}},
        {"states": {"upload": {"selected": [0, 0]}}},
        {"states": {"upload": {"selected": [0, 1], "transforms": {"0": {"dx": 1}}}}},
        {"states": {"upload": {"selected": [0, 1], "transforms": {"0": {"scale": []}}}}},
        {"states": {"upload": {"selected": [0, 1], "pixels": {"0": {"0,0": "#010203"}}}}},
    )
    for update in payloads:
        with pytest.raises(SpriteEngineError, match="invalid curation payload"):
            engine.stamp_curation(
                project.id,
                {"version": 1, "kind": "sprite-gen-curation", "runRevision": revision, **update},
            )

    assert not [artifact for artifact in store.load(project.id).artifacts if artifact.kind == "sprite_curation"]


def test_stamp_curation_keeps_upstream_empty_selection_semantics(tmp_path):
    _, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision

    curated = engine.stamp_curation(
        project.id,
        {
            "version": 1,
            "kind": "sprite-gen-curation",
            "runRevision": revision,
            "states": {"upload": {"selected": []}},
        },
    )

    assert curated.selected["upload"] == (0, 1)


def test_compose_rejects_a_noncanonical_atlas_before_publishing_it(tmp_path, monkeypatch):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)

    import ai_sprite_studio.sprite_engine as sprite_engine

    upstream_compose = sprite_engine.compose_atlas.run

    def compose_with_soft_alpha(*args, **kwargs):
        result = upstream_compose(*args, **kwargs)
        path = kwargs["run_dir"] / "sprite-sheet-alpha.png"
        with Image.open(path) as opened:
            atlas = opened.convert("RGBA")
        atlas.putpixel((0, 0), (1, 2, 3, 127))
        atlas.save(path)
        return result

    monkeypatch.setattr(sprite_engine.compose_atlas, "run", compose_with_soft_alpha)

    with pytest.raises(SpriteEngineError, match="canonical sprite atlas has non-binary alpha"):
        engine.compose(project.id)

    assert not [
        artifact
        for artifact in store.load(project.id).artifacts
        if artifact.kind == "sprite_atlas" and artifact.variant == "pixel"
    ]


def test_extracted_frame_artifacts_depend_on_source_and_request(tmp_path):
    store, project, engine, _, uploaded, prepared = _prepared_engine(tmp_path, frames=2)

    extracted = engine.extract(project.id)
    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}

    assert all(
        artifacts[artifact_id].dependencies == [uploaded.id, prepared.request_artifact_id]
        for artifact_id in (*extracted.plain_artifact_ids, *extracted.pixel_artifact_ids)
    )


def test_composed_artifacts_depend_on_the_stamped_curation(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision
    curated = engine.stamp_curation(
        project.id,
        {
            "version": 1,
            "kind": "sprite-gen-curation",
            "runRevision": revision,
            "states": {"upload": {"selected": [1, 0], "transforms": {"1": {"dx": 2}}}},
        },
    )

    composed = engine.compose(project.id)
    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}

    assert curated.artifact_id is not None
    assert all(
        curated.artifact_id in artifacts[artifact_id].dependencies
        for artifact_id in (
            composed.atlas_artifact_id,
            composed.manifest_artifact_id,
            composed.report_artifact_id,
        )
    )


def test_compose_rejects_a_curation_sidecar_without_matching_artifact_provenance(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision
    engine.stamp_curation(
        project.id,
        {
            "version": 1,
            "kind": "sprite-gen-curation",
            "runRevision": revision,
            "states": {"upload": {"selected": [1, 0]}},
        },
    )
    sidecar = store.run_dir(project.id) / "curation.json"
    changed = json.loads(sidecar.read_text())
    changed["states"]["upload"]["selected"] = [0, 1]
    sidecar.write_text(json.dumps(changed))

    with pytest.raises(SpriteEngineError, match="curation provenance"):
        engine.compose(project.id)

    assert not [artifact for artifact in store.load(project.id).artifacts if artifact.kind == "sprite_atlas"]


def test_reextraction_clears_current_curation_atlas_and_inspection_lineage(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision
    curated = engine.stamp_curation(
        project.id,
        {
            "version": 1,
            "kind": "sprite-gen-curation",
            "runRevision": revision,
            "states": {"upload": {"selected": [1, 0]}},
        },
    )
    composed = engine.compose(project.id)
    inspected = engine.inspect(project.id)
    before = json.loads((store.run_dir(project.id) / "sprite-engine.json").read_text())

    assert curated.artifact_id is not None
    assert before["curation_artifact_id"] == str(curated.artifact_id)
    assert before["atlas_artifact_id"] == str(composed.atlas_artifact_id)
    assert before["inspect_report_artifact_id"] == str(inspected.report_artifact_id)

    refreshed = engine.extract(project.id)
    current = json.loads((store.run_dir(project.id) / "sprite-engine.json").read_text())

    for key in (
        "curation_artifact_id",
        "atlas_artifact_id",
        "manifest_artifact_id",
        "compose_report_artifact_id",
        "inspect_report_artifact_id",
    ):
        assert key not in current
    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}
    assert {curated.artifact_id, composed.atlas_artifact_id, inspected.report_artifact_id} <= set(artifacts)

    fresh_inspection = engine.inspect(project.id)
    assert artifacts.get(fresh_inspection.report_artifact_id) is None
    fresh_artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}
    assert fresh_artifacts[fresh_inspection.report_artifact_id].dependencies == list(
        refreshed.pixel_artifact_ids
    )


def test_concurrent_curation_stamps_keep_sidecar_artifact_and_state_in_sync(tmp_path, monkeypatch):
    import threading

    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision
    first_put_started = threading.Event()
    second_put_started = threading.Event()
    second_finished = threading.Event()
    release_first_put = threading.Event()
    failures = []
    original_put = SpriteEngine._put_artifact
    put_count = 0
    put_count_lock = threading.Lock()

    def pause_first_curation_put(self, project_id, data, **kwargs):
        nonlocal put_count
        if kwargs["kind"] == "sprite_curation":
            with put_count_lock:
                put_count += 1
                call = put_count
            if call == 1:
                first_put_started.set()
                assert release_first_put.wait(timeout=5)
            elif call == 2:
                second_put_started.set()
        return original_put(self, project_id, data, **kwargs)

    monkeypatch.setattr(SpriteEngine, "_put_artifact", pause_first_curation_put)

    def stamp(selected, finished=None):
        try:
            engine.stamp_curation(
                project.id,
                {
                    "version": 1,
                    "kind": "sprite-gen-curation",
                    "runRevision": revision,
                    "states": {"upload": {"selected": selected}},
                },
            )
        except BaseException as exc:  # pragma: no cover - assertions make failures explicit below
            failures.append(exc)
        finally:
            if finished is not None:
                finished.set()

    first = threading.Thread(target=stamp, args=([0, 1],))
    second = threading.Thread(target=stamp, args=([1, 0], second_finished))
    first.start()
    assert first_put_started.wait(timeout=5)
    second.start()
    if second_put_started.wait(timeout=1):
        assert second_finished.wait(timeout=5)
    release_first_put.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not failures
    state = json.loads((store.run_dir(project.id) / "sprite-engine.json").read_text())
    sidecar = (store.run_dir(project.id) / "curation.json").read_bytes()
    assert store.read_artifact_bytes(project.id, state["curation_artifact_id"]) == sidecar


def test_engine_mutation_lock_serializes_separate_processes(tmp_path):
    import multiprocessing

    store, project, _ = _project_engine(tmp_path)
    run_dir = store.run_dir(project.id)
    context = multiprocessing.get_context("spawn")
    attempting = context.Event()
    entered = context.Event()
    process = context.Process(
        target=_acquire_engine_mutation_lock,
        args=(str(run_dir), attempting, entered),
    )

    import ai_sprite_studio.sprite_engine as sprite_engine

    with sprite_engine._engine_mutation_guard(run_dir):
        process.start()
        assert attempting.wait(timeout=5)
        assert not entered.wait(timeout=0.25)
    assert entered.wait(timeout=5)
    process.join(timeout=5)
    assert process.exitcode == 0


@pytest.mark.parametrize(
    ("boundary", "operation"),
    (
        ("load_curation", "load"),
        ("run_revision", "load"),
        ("state_plan", "load"),
        ("frame_variant", "load"),
        ("read_guard", "load"),
        ("stamp_curation", "stamp"),
    ),
)
def test_curation_boundaries_redact_upstream_failures(tmp_path, monkeypatch, boundary, operation):
    from contextlib import contextmanager

    _, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision

    import ai_sprite_studio.sprite_engine as sprite_engine

    if boundary == "read_guard":
        @contextmanager
        def failing_boundary(*args, **kwargs):
            raise SystemExit(f"unsafe upstream path {sprite_engine.Path.cwd()}")
            yield

        monkeypatch.setattr(sprite_engine, boundary, failing_boundary)
    else:
        def failing_boundary(*args, **kwargs):
            raise SystemExit(f"unsafe upstream path {sprite_engine.Path.cwd()}")

        monkeypatch.setattr(sprite_engine, boundary, failing_boundary)

    with pytest.raises(SpriteEngineError) as failure:
        if operation == "load":
            engine.load_curation(project.id)
        else:
            engine.stamp_curation(
                project.id,
                {
                    "version": 1,
                    "kind": "sprite-gen-curation",
                    "runRevision": revision,
                    "states": {"upload": {"selected": [0, 1]}},
                },
            )

    assert "unsafe upstream" not in str(failure.value)
    assert str(sprite_engine.Path.cwd()) not in str(failure.value)


def test_load_curation_redacts_a_malformed_upstream_sidecar(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    sidecar = store.run_dir(project.id) / "curation.json"
    sidecar.write_text("{")

    with pytest.raises(SpriteEngineError) as failure:
        engine.load_curation(project.id)

    assert str(sidecar) not in str(failure.value)


def test_compose_redacts_a_malformed_upstream_sidecar(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    sidecar = _write_matching_curation_sidecar(store, project, b"{")

    with pytest.raises(SpriteEngineError) as failure:
        engine.compose(project.id)

    assert str(sidecar) not in str(failure.value)
    assert "JSONDecodeError" not in str(failure.value)


def test_stamp_curation_redacts_state_update_failures(tmp_path, monkeypatch):
    _, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision

    def failing_state_update(*args, **kwargs):
        raise OSError("unsafe state update /tmp/sprite-engine.json")

    monkeypatch.setattr(SpriteEngine, "_write_state", failing_state_update)

    with pytest.raises(SpriteEngineError) as failure:
        engine.stamp_curation(
            project.id,
            {
                "version": 1,
                "kind": "sprite-gen-curation",
                "runRevision": revision,
                "states": {"upload": {"selected": [0]}},
            },
        )

    assert "unsafe state update" not in str(failure.value)
    assert "/tmp" not in str(failure.value)


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
        artifacts[artifact_id].dependencies == [uploaded.id, prepared.request_artifact_id]
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
    editable = _rgba(store.read_artifact_bytes(project.id, extracted.pixel_artifact_ids[1]))
    edit_x, edit_y = next(
        (x, y)
        for y in range(0, 256, 2)
        for x in range(0, 256, 2)
        if editable.getpixel((x, y))[3]
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
                    "pixels": {
                        "1": {
                            f"{edit_x + dx},{edit_y + dy}": None
                            for dy in range(2)
                            for dx in range(2)
                        }
                    },
                }
            },
        },
    )
    current_curation = engine.load_curation(project.id)
    assert curated.run_revision == current_curation.run_revision
    assert current_curation.artifact_id == curated.artifact_id
    assert curated.selected["upload"] == (1, 0)
    pixels_before = tuple(
        store.read_artifact_bytes(project.id, artifact_id)
        for artifact_id in extracted.pixel_artifact_ids
    )

    composed = engine.compose(project.id)
    inspected = engine.inspect(project.id)
    assert composed.atlas_artifact_id
    assert composed.manifest_artifact_id
    assert inspected.summary["ok"] is True
    assert tuple(
        store.read_artifact_bytes(project.id, artifact_id)
        for artifact_id in extracted.pixel_artifact_ids
    ) == pixels_before
    manifest = json.loads(store.read_artifact_bytes(project.id, composed.manifest_artifact_id))
    assert manifest["animation"]["rows"]["upload"]["frames"] == 2
    assert manifest["animation"]["rows"]["upload"]["frame_variant"] == "pixel"
    report = json.loads(store.read_artifact_bytes(project.id, composed.report_artifact_id))
    assert [cell["frame"] for cell in report["cells"]] == [1, 0]
    atlas = _rgba(store.read_artifact_bytes(project.id, composed.atlas_artifact_id))
    assert atlas.crop((256, 0, 512, 256)).tobytes() == _rgba(pixels_before[0]).tobytes()
    assert atlas.crop((0, 0, 256, 256)).tobytes() != _rgba(pixels_before[1]).tobytes()


def test_components_fail_for_fused_poses_without_slot_fallback_but_projection_is_explicit(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2, fused=True)

    with pytest.raises(SpriteEngineError, match="sprite extraction failed") as failure:
        engine.extract(project.id)

    assert "could not extract 2 sprite components" in json.dumps(failure.value.diagnostics)
    assert str(store.run_dir(project.id)) not in str(failure.value)
    projected = engine.extract(project.id, segmentation="projection")
    assert len(projected.pixel_artifact_ids) == 2
    assert projected.diagnostics["segmentation"] == "projection"


def test_components_reject_large_input_before_upstream_but_projection_can_run(tmp_path, monkeypatch):
    store, project, engine = _project_engine(tmp_path)
    encoded = io.BytesIO()
    Image.new("RGBA", (513, 512), MAGENTA).save(encoded, format="PNG")
    uploaded = engine.ingest_upload(
        project.id, encoded.getvalue(), media_type="image/png", filename="large.png"
    )
    engine.prepare(project.id, uploaded.id)

    import ai_sprite_studio.sprite_engine as sprite_engine

    calls = []

    def upstream_probe(*args, **kwargs):
        calls.append(kwargs["segmentation"])
        raise SystemExit("unsafe upstream diagnostic")

    monkeypatch.setattr(sprite_engine.sprite_extract, "run", upstream_probe)

    with pytest.raises(SpriteEngineError, match="component input is too complex"):
        engine.extract(project.id)
    assert not calls
    with pytest.raises(SpriteEngineError, match="sprite extract failed"):
        engine.extract(project.id, segmentation="projection")
    assert calls == ["projection"]


def test_components_gate_rechecks_verified_bytes_not_forged_metadata(tmp_path, monkeypatch):
    store, project, engine = _project_engine(tmp_path)
    encoded = io.BytesIO()
    Image.new("RGBA", (513, 512), MAGENTA).save(encoded, format="PNG")
    forged = store.put_artifact(
        project.id,
        encoded.getvalue(),
        kind="input",
        media_type="image/png",
        variant="raw",
        width=1,
        height=1,
    )
    engine.prepare(project.id, forged.id)

    import ai_sprite_studio.sprite_engine as sprite_engine

    calls = []

    def upstream_probe(*args, **kwargs):
        calls.append(kwargs["segmentation"])
        raise SystemExit("unsafe upstream diagnostic")

    monkeypatch.setattr(sprite_engine.sprite_extract, "run", upstream_probe)

    with pytest.raises(SpriteEngineError, match="component input is too complex"):
        engine.extract(project.id)
    assert not calls
    with pytest.raises(SpriteEngineError, match="sprite extract failed"):
        engine.extract(project.id, segmentation="projection")
    assert calls == ["projection"]


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


def test_failed_reextract_clears_all_current_derived_state(tmp_path, monkeypatch):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision
    engine.stamp_curation(
        project.id,
        {
            "version": 1,
            "kind": "sprite-gen-curation",
            "runRevision": revision,
            "states": {"upload": {"selected": [1, 0]}},
        },
    )
    engine.compose(project.id)
    engine.inspect(project.id)

    original_put = SpriteEngine._put_artifact

    def fail_frame_import(self, project_id, data, **kwargs):
        if kwargs["kind"] == "sprite_frame":
            raise SpriteEngineError("test frame import failure")
        return original_put(self, project_id, data, **kwargs)

    monkeypatch.setattr(SpriteEngine, "_put_artifact", fail_frame_import)

    with pytest.raises(SpriteEngineError, match="test frame import failure"):
        engine.extract(project.id)

    state = json.loads((store.run_dir(project.id) / "sprite-engine.json").read_text())
    assert set(state) == {"input_artifact_id", "request_artifact_id"}
    with pytest.raises(SpriteEngineError, match="has not been extracted"):
        engine.compose(project.id)
    with pytest.raises(SpriteEngineError, match="has not been extracted"):
        engine.inspect(project.id)


def test_compose_rejects_live_frame_changed_after_upstream_runs(tmp_path, monkeypatch):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)

    import ai_sprite_studio.sprite_engine as sprite_engine

    upstream_compose = sprite_engine.compose_atlas.run

    def compose_then_change_frame(*args, **kwargs):
        result = upstream_compose(*args, **kwargs)
        manifest = json.loads((kwargs["run_dir"] / "frames/frames-manifest.json").read_text())
        row = next(item for item in manifest["rows"] if item["state"] == "upload")
        _change_live_pixel_frame(kwargs["run_dir"] / row["files"][0])
        return result

    monkeypatch.setattr(sprite_engine.compose_atlas, "run", compose_then_change_frame)

    with pytest.raises(SpriteEngineError, match="frame provenance"):
        engine.compose(project.id)

    assert not [artifact for artifact in store.load(project.id).artifacts if artifact.kind == "sprite_atlas"]


def test_compose_error_after_live_frame_change_resets_current_state(tmp_path, monkeypatch):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision
    curated = engine.stamp_curation(
        project.id,
        {
            "version": 1,
            "kind": "sprite-gen-curation",
            "runRevision": revision,
            "states": {"upload": {"selected": [0]}},
        },
    )
    composed = engine.compose(project.id)
    inspected = engine.inspect(project.id)
    before = json.loads((store.run_dir(project.id) / "sprite-engine.json").read_text())
    artifact_ids = {artifact.id for artifact in store.load(project.id).artifacts}

    import ai_sprite_studio.sprite_engine as sprite_engine

    upstream_compose = sprite_engine.compose_atlas.run

    def compose_then_change_frame_and_fail(*args, **kwargs):
        upstream_compose(*args, **kwargs)
        manifest = json.loads((kwargs["run_dir"] / "frames/frames-manifest.json").read_text())
        row = next(item for item in manifest["rows"] if item["state"] == "upload")
        _change_live_pixel_frame(kwargs["run_dir"] / row["files"][0])
        raise SystemExit("unsafe upstream diagnostic")

    monkeypatch.setattr(sprite_engine.compose_atlas, "run", compose_then_change_frame_and_fail)

    with pytest.raises(SpriteEngineError, match="sprite compose failed"):
        engine.compose(project.id)

    state = json.loads((store.run_dir(project.id) / "sprite-engine.json").read_text())
    assert state == {
        "input_artifact_id": before["input_artifact_id"],
        "request_artifact_id": before["request_artifact_id"],
    }
    assert curated.artifact_id is not None
    assert {curated.artifact_id, composed.atlas_artifact_id, inspected.report_artifact_id} <= artifact_ids
    with pytest.raises(SpriteEngineError, match="has not been extracted"):
        engine.load_curation(project.id)
    with pytest.raises(SpriteEngineError, match="has not been extracted"):
        engine.stamp_curation(
            project.id,
            {
                "version": 1,
                "kind": "sprite-gen-curation",
                "runRevision": revision,
                "states": {"upload": {"selected": [0]}},
            },
        )
    with pytest.raises(SpriteEngineError, match="has not been extracted"):
        engine.compose(project.id)
    with pytest.raises(SpriteEngineError, match="has not been extracted"):
        engine.inspect(project.id)
    assert {artifact.id for artifact in store.load(project.id).artifacts} == artifact_ids


def test_inspect_rejects_live_frames_that_no_longer_match_artifacts(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    manifest = json.loads((store.run_dir(project.id) / "frames/frames-manifest.json").read_text())
    row = next(item for item in manifest["rows"] if item["state"] == "upload")
    _change_live_pixel_frame(store.run_dir(project.id) / row["files"][0])

    with pytest.raises(SpriteEngineError, match="frame provenance"):
        engine.inspect(project.id)

    state = json.loads((store.run_dir(project.id) / "sprite-engine.json").read_text())
    assert "inspect_report_artifact_id" not in state


def test_compose_releases_its_upstream_lock_after_an_error(tmp_path, monkeypatch):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    before = json.loads((store.run_dir(project.id) / "sprite-engine.json").read_text())

    import ai_sprite_studio.sprite_engine as sprite_engine
    from sprite_gen.runio import acquire_run_dir_lock

    def acquire_then_fail(*args, **kwargs):
        acquire_run_dir_lock(kwargs["run_dir"], "test")
        raise SystemExit("unsafe upstream diagnostic")

    monkeypatch.setattr(sprite_engine.compose_atlas, "run", acquire_then_fail)

    with pytest.raises(SpriteEngineError, match="sprite compose failed"):
        engine.compose(project.id)

    assert not (engine.store.run_dir(project.id) / ".sprite-gen.lock").exists()
    assert json.loads((store.run_dir(project.id) / "sprite-engine.json").read_text()) == before


def test_separate_process_compose_releases_the_upstream_lock(tmp_path):
    import multiprocessing

    _, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    context = multiprocessing.get_context("spawn")
    first_finished = context.Event()
    release_first = context.Event()
    first = context.Process(
        target=_compose_then_hold,
        args=(str(tmp_path), str(project.id), first_finished, release_first),
    )
    second_finished = context.Event()
    second_release = context.Event()
    second = context.Process(
        target=_compose_then_hold,
        args=(str(tmp_path), str(project.id), second_finished, second_release),
    )

    first.start()
    try:
        assert first_finished.wait(timeout=10)
        second.start()
        assert second_finished.wait(timeout=10)
        second_release.set()
        second.join(timeout=10)
        assert second.exitcode == 0
    finally:
        release_first.set()
        first.join(timeout=10)
    assert first.exitcode == 0


def test_diagnostics_redact_cross_platform_path_like_mapping_keys(tmp_path):
    run_dir = tmp_path / "project" / "run"
    paths = (
        "/tmp/private/sprite.png",
        r"C:\private\sprite.png",
        r"\\host\share\sprite.png",
    )
    diagnostics = SpriteEngine._redact(
        {
            "paths": list(paths),
            paths[0]: "posix",
            paths[1]: "drive",
            paths[2]: "unc",
            "nested": {str(run_dir): "safe"},
            "artifact-123": "asset-123",
        },
        run_dir,
    )

    rendered = json.dumps(diagnostics)
    assert all(path not in rendered for path in paths)
    assert str(run_dir) not in rendered
    assert diagnostics["paths"] == ["<path>", "<path>", "<path>"]
    assert diagnostics["nested"] == {"<run>": "safe"}
    assert diagnostics["artifact-123"] == "asset-123"


def test_engine_lock_rejects_a_symlink_sidecar(tmp_path):
    store, project, engine = _project_engine(tmp_path)
    uploaded = engine.ingest_upload(
        project.id, _strip_png(frames=1), media_type="image/png", filename="ranger.png"
    )
    run_dir = store.run_dir(project.id)
    sidecar = run_dir.parent / f".{run_dir.name}.sprite-engine.lock"
    target = tmp_path / "outside-lock"
    sidecar.symlink_to(target)

    with pytest.raises(SpriteEngineError, match="sprite engine operation failed"):
        engine.prepare(project.id, uploaded.id)

    assert sidecar.is_symlink()
    assert not target.exists()
    assert not list(run_dir.iterdir())


@pytest.mark.parametrize("operation", ("load", "compose"))
@pytest.mark.parametrize("shape", ("root-list", "states-list", "states-null"))
def test_valid_json_wrong_shape_curation_is_contained(tmp_path, operation, shape):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    if shape == "root-list":
        data = b"[]\n"
    elif shape == "states-list":
        data = json.dumps(
            {
                "kind": "sprite-gen-curation",
                "run_revision": engine.load_curation(project.id).run_revision,
                "states": [],
            }
        ).encode()
    else:
        data = json.dumps(
            {
                "kind": "sprite-gen-curation",
                "run_revision": engine.load_curation(project.id).run_revision,
                "states": None,
            }
        ).encode()
    sidecar = _write_matching_curation_sidecar(store, project, data)

    with pytest.raises(SpriteEngineError, match="invalid sprite curation") as failure:
        if operation == "load":
            engine.load_curation(project.id)
        else:
            engine.compose(project.id)

    assert str(sidecar) not in str(failure.value)
    assert "AttributeError" not in str(failure.value)


def test_extraction_request_artifact_matches_live_request_bytes(tmp_path):
    store, project, engine, _, uploaded, prepared = _prepared_engine(tmp_path, frames=1)

    extracted = engine.extract(project.id)
    run_dir = store.run_dir(project.id)
    state = json.loads((run_dir / "sprite-engine.json").read_text())

    assert state["request_artifact_id"] == str(prepared.request_artifact_id)
    assert store.read_artifact_bytes(project.id, state["request_artifact_id"]) == (
        run_dir / "sprite-request.json"
    ).read_bytes()
    artifacts = {artifact.id: artifact for artifact in store.load(project.id).artifacts}
    assert all(
        artifacts[artifact_id].dependencies == [uploaded.id, prepared.request_artifact_id]
        for artifact_id in (*extracted.plain_artifact_ids, *extracted.pixel_artifact_ids)
    )


@pytest.mark.parametrize("operation", ("extract", "load", "stamp", "compose", "inspect"))
def test_live_request_drift_blocks_current_sprite_operations(tmp_path, operation):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision
    run_dir = store.run_dir(project.id)
    before = json.loads((run_dir / "sprite-engine.json").read_text())
    artifact_ids = {artifact.id for artifact in store.load(project.id).artifacts}
    _change_live_request(run_dir / "sprite-request.json")

    with pytest.raises(SpriteEngineError, match="request provenance"):
        if operation == "extract":
            engine.extract(project.id)
        elif operation == "load":
            engine.load_curation(project.id)
        elif operation == "stamp":
            engine.stamp_curation(
                project.id,
                {
                    "version": 1,
                    "kind": "sprite-gen-curation",
                    "runRevision": revision,
                    "states": {"upload": {"selected": [0]}},
                },
            )
        elif operation == "compose":
            engine.compose(project.id)
        else:
            engine.inspect(project.id)

    assert json.loads((run_dir / "sprite-engine.json").read_text()) == {
        "input_artifact_id": before["input_artifact_id"],
        "request_artifact_id": before["request_artifact_id"],
    }
    assert {artifact.id for artifact in store.load(project.id).artifacts} == artifact_ids


@pytest.mark.parametrize("operation", ("load", "compose"))
@pytest.mark.parametrize(
    ("states", "version"),
    (
        ({"upload": []}, 1),
        ({"upload": {"pixel_perfect": False}}, 1),
        ({"upload": {"clones": {"1": 0}}}, 1),
        ({"upload": {"transforms": {"0": {"dx": 1}}}}, 1),
        ({"upload": {"transforms": {"0": {"dx": 10**100}}}}, 1),
        ({"upload": {"pixels": {"0": {"0,0": "#010203"}}}}, 1),
        ({"hidden": {"selected": [0]}}, 1),
        ({"upload": {"selected": [0]}}, 2),
    ),
    ids=(
        "nested-list",
        "plain",
        "clones",
        "off-grid-transform",
        "huge-transform",
        "partial-pixel-edit",
        "unknown-row",
        "wrong-version",
    ),
)
def test_artifact_backed_curation_obeys_canonical_policy(tmp_path, operation, states, version):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    data = json.dumps(
        {
            "version": version,
            "kind": "sprite-gen-curation",
            "run_revision": engine.load_curation(project.id).run_revision,
            "states": states,
        }
    ).encode()
    sidecar = _write_matching_curation_sidecar(store, project, data)

    with pytest.raises(SpriteEngineError, match="invalid sprite curation") as failure:
        if operation == "load":
            engine.load_curation(project.id)
        else:
            engine.compose(project.id)

    assert str(sidecar) not in str(failure.value)
    assert not [artifact for artifact in store.load(project.id).artifacts if artifact.kind == "sprite_atlas"]


@pytest.mark.parametrize("change_live_frame", (False, True), ids=("unchanged", "changed"))
def test_compose_overflow_is_contained_and_checks_live_frames(
    tmp_path, monkeypatch, change_live_frame
):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    run_dir = store.run_dir(project.id)
    before = json.loads((run_dir / "sprite-engine.json").read_text())

    import ai_sprite_studio.sprite_engine as sprite_engine

    upstream_compose = sprite_engine.compose_atlas.run

    def compose_then_overflow(*args, **kwargs):
        upstream_compose(*args, **kwargs)
        if change_live_frame:
            manifest = json.loads((kwargs["run_dir"] / "frames/frames-manifest.json").read_text())
            row = next(item for item in manifest["rows"] if item["state"] == "upload")
            _change_live_pixel_frame(kwargs["run_dir"] / row["files"][0])
        raise OverflowError(f"unsafe upstream diagnostic {kwargs['run_dir']}")

    monkeypatch.setattr(sprite_engine.compose_atlas, "run", compose_then_overflow)

    with pytest.raises(SpriteEngineError, match="sprite compose failed") as failure:
        engine.compose(project.id)

    assert "unsafe upstream" not in str(failure.value)
    assert str(run_dir) not in str(failure.value)
    state = json.loads((run_dir / "sprite-engine.json").read_text())
    if change_live_frame:
        assert state == {
            "input_artifact_id": before["input_artifact_id"],
            "request_artifact_id": before["request_artifact_id"],
        }
    else:
        assert state == before


def test_compose_lock_release_failure_invalidates_live_frame_drift(tmp_path, monkeypatch):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=1)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision
    engine.stamp_curation(
        project.id,
        {
            "version": 1,
            "kind": "sprite-gen-curation",
            "runRevision": revision,
            "states": {"upload": {"selected": [0]}},
        },
    )
    engine.compose(project.id)
    engine.inspect(project.id)
    run_dir = store.run_dir(project.id)
    before = json.loads((run_dir / "sprite-engine.json").read_text())

    import ai_sprite_studio.sprite_engine as sprite_engine

    upstream_release = sprite_engine.release_run_dir_lock

    def release_then_change_frame_and_fail(path):
        upstream_release(path)
        manifest = json.loads((path / "frames/frames-manifest.json").read_text())
        row = next(item for item in manifest["rows"] if item["state"] == "upload")
        _change_live_pixel_frame(path / row["files"][0])
        raise OSError(f"unsafe lock release {path}")

    monkeypatch.setattr(sprite_engine, "release_run_dir_lock", release_then_change_frame_and_fail)

    with pytest.raises(SpriteEngineError, match="sprite compose failed") as failure:
        engine.compose(project.id)

    assert "unsafe lock release" not in str(failure.value)
    assert str(run_dir) not in str(failure.value)
    assert json.loads((run_dir / "sprite-engine.json").read_text()) == {
        "input_artifact_id": before["input_artifact_id"],
        "request_artifact_id": before["request_artifact_id"],
    }


def test_extract_rebinds_the_immutable_source_over_a_mutated_raw_file(tmp_path):
    from sprite_gen.layout import raw_rel

    store, project, engine, source, _, _ = _prepared_engine(tmp_path, frames=2)
    run_dir = store.run_dir(project.id)
    request = json.loads((run_dir / "sprite-request.json").read_text())
    raw_path = run_dir / raw_rel(request, "upload")

    assert raw_path.read_bytes() == source
    raw_path.write_bytes(_strip_png(frames=1))  # tamper the on-disk raw source

    engine.extract(project.id)

    # Extraction re-binds the verified upload, so the mutated bytes are discarded.
    assert raw_path.read_bytes() == source


def test_extract_rejects_a_request_pointer_with_the_wrong_artifact_role(tmp_path):
    store, project, engine, _, uploaded, _ = _prepared_engine(tmp_path, frames=2)
    run_dir = store.run_dir(project.id)
    impostor = store.put_artifact(
        project.id,
        (run_dir / "sprite-request.json").read_bytes(),
        kind="sprite_report",  # same bytes, wrong role
        media_type="application/json",
        variant="raw",
        stage="sprite_upload",
        dependencies=[uploaded.id],
    )
    state = json.loads((run_dir / "sprite-engine.json").read_text())
    state["request_artifact_id"] = str(impostor.id)
    (run_dir / "sprite-engine.json").write_text(json.dumps(state, sort_keys=True) + "\n")

    with pytest.raises(SpriteEngineError, match="request provenance"):
        engine.extract(project.id)


def test_load_curation_rejects_a_pixel_pointer_with_broken_lineage(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    extracted = engine.extract(project.id)
    run_dir = store.run_dir(project.id)
    impostor = store.put_artifact(
        project.id,
        store.read_artifact_bytes(project.id, extracted.pixel_artifact_ids[0]),
        kind="sprite_frame",
        variant="pixel",
        media_type="image/png",
        stage="sprite_upload",
        dependencies=[],  # same bytes and role, missing source/request lineage
        width=256,
        height=256,
    )
    state = json.loads((run_dir / "sprite-engine.json").read_text())
    state["pixel_artifact_ids"][0] = str(impostor.id)
    (run_dir / "sprite-engine.json").write_text(json.dumps(state, sort_keys=True) + "\n")

    with pytest.raises(SpriteEngineError, match="frame provenance"):
        engine.load_curation(project.id)


def test_compose_rejects_a_curation_pointer_with_broken_lineage(tmp_path):
    store, project, engine, _, _, _ = _prepared_engine(tmp_path, frames=2)
    engine.extract(project.id)
    revision = engine.load_curation(project.id).run_revision
    engine.stamp_curation(
        project.id,
        {
            "version": 1,
            "kind": "sprite-gen-curation",
            "runRevision": revision,
            "states": {"upload": {"selected": [1, 0]}},
        },
    )
    run_dir = store.run_dir(project.id)
    sidecar_bytes = (run_dir / "curation.json").read_bytes()
    impostor = store.put_artifact(
        project.id,
        sidecar_bytes,
        kind="sprite_curation",
        variant="raw",
        media_type="application/json",
        stage="sprite_upload",
        dependencies=[],  # same bytes and role, missing pixel-frame lineage
    )
    state = json.loads((run_dir / "sprite-engine.json").read_text())
    state["curation_artifact_id"] = str(impostor.id)
    (run_dir / "sprite-engine.json").write_text(json.dumps(state, sort_keys=True) + "\n")

    with pytest.raises(SpriteEngineError, match="curation provenance"):
        engine.compose(project.id)
