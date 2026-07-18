from starlette.testclient import TestClient

from ai_sprite_studio.app import create_app
from test_sprite_engine import _strip_png


def _client(tmp_path):
    return TestClient(create_app(tmp_path), base_url="http://127.0.0.1")


def _upload(client, *, frames=2, segmentation="components"):
    return client.post(
        f"/curator/upload?frames={frames}&segmentation={segmentation}",
        files={"files": ("ranger.png", _strip_png(frames=frames), "image/png")},
        headers={"Origin": "http://127.0.0.1"},
    )


def test_landing_serves_the_upload_form(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert 'id="upload"' in page.text
    assert "/curator/upload.js" in page.text


def test_landing_links_the_studio_stylesheet(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/")
        css = client.get("/studio.css")
    assert 'href="/studio.css"' in page.text
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")


def test_upload_extracts_and_run_snapshot_lists_frames(tmp_path):
    with _client(tmp_path) as client:
        uploaded = _upload(client, frames=2)
        run = client.get("/api/run").json()

    assert uploaded.status_code == 201
    assert run["cell"] == {"width": 256, "height": 256}
    assert run["runRevision"]
    (state,) = run["states"]
    assert state["name"] == "upload"
    assert len(state["frames"]) == 2
    assert all(frame["present"] and frame["url"].startswith("/curator/frame/") for frame in state["frames"])


def test_uploader_shows_a_live_working_indicator(tmp_path):
    with _client(tmp_path) as client:
        js = client.get("/curator/upload.js").text
    # ticking elapsed-time feedback + button lock, so a slow extract doesn't look frozen.
    assert "setInterval" in js and "still working" in js
    assert "button.disabled = true" in js and "clearInterval" in js


def test_upload_multiple_files_become_one_row_of_frames(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post(
            "/curator/upload?segmentation=components",
            files=[("files", (f"f{i}.png", _strip_png(frames=1), "image/png")) for i in range(3)],
            headers={"Origin": "http://127.0.0.1"},
        )
        run = client.get("/api/run").json()
    assert resp.status_code == 201
    (state,) = run["states"]
    assert len(state["frames"]) == 3  # 3 separate images -> 3 frames


def test_upload_autosplit_detects_frames_on_a_sheet(tmp_path):
    # A 3-frame sheet posted with autosplit=1 (frames field left at default 4):
    # auto-detection by background gaps must recover 3, not obey the frames field.
    with _client(tmp_path) as client:
        resp = client.post(
            "/curator/upload?autosplit=1&frames=4&segmentation=components",
            files={"files": ("sheet.png", _strip_png(frames=3), "image/png")},
            headers={"Origin": "http://127.0.0.1"},
        )
        run = client.get("/api/run").json()
    assert resp.status_code == 201
    assert len(run["states"][0]["frames"]) == 3


def test_upload_single_grid_sheet_reshapes_via_cols_rows(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post(
            "/curator/upload?cols=4&rows=1&frames=4&segmentation=components",
            files={"files": ("sheet.png", _strip_png(frames=4), "image/png")},
            headers={"Origin": "http://127.0.0.1"},
        )
        run = client.get("/api/run").json()
    assert resp.status_code == 201
    assert len(run["states"][0]["frames"]) == 4


def test_uploaded_frame_bytes_are_served_as_png(tmp_path):
    with _client(tmp_path) as client:
        _upload(client)
        run = client.get("/api/run").json()
        frame_url = run["states"][0]["frames"][0]["url"]
        image = client.get(frame_url)

    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_curation_saves_and_stale_revision_conflicts(tmp_path):
    with _client(tmp_path) as client:
        _upload(client, frames=2)
        revision = client.get("/api/run").json()["runRevision"]
        headers = {"Origin": "http://127.0.0.1"}
        saved = client.post(
            "/api/curation",
            json={
                "version": 1,
                "kind": "sprite-gen-curation",
                "runRevision": revision,
                "states": {"upload": {"selected": [1, 0]}},
            },
            headers=headers,
        )
        stale = client.post(
            "/api/curation",
            json={
                "version": 1,
                "kind": "sprite-gen-curation",
                "runRevision": "not-the-current-revision",
                "states": {"upload": {"selected": [0, 1]}},
            },
            headers=headers,
        )

    assert saved.status_code == 200
    assert stale.status_code == 409
    assert "error" in stale.json()


def test_curation_save_requires_same_origin(tmp_path):
    with _client(tmp_path) as client:
        _upload(client)
        revision = client.get("/api/run").json()["runRevision"]
        blocked = client.post(
            "/api/curation",
            json={
                "version": 1,
                "kind": "sprite-gen-curation",
                "runRevision": revision,
                "states": {"upload": {"selected": [0]}},
            },
        )
    assert blocked.status_code == 403


def test_curator_assets_and_page_are_served(tmp_path):
    with _client(tmp_path) as client:
        redirect = client.get("/curator", follow_redirects=False)
        _upload(client)
        page = client.get("/curator")
        script = client.get("/curator.js")

    assert redirect.status_code == 303  # no active run yet -> back to landing
    assert page.status_code == 200 and "<body>" in page.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "/api/curation" in script.text


def test_download_atlas_composes(tmp_path):
    with _client(tmp_path) as client:
        _upload(client, frames=2)
        atlas = client.get("/download/atlas")
        pngs = client.get("/download/pngs")

    assert atlas.status_code == 200
    assert atlas.headers["content-type"] == "image/png"
    assert atlas.headers["x-filename"] == "sprite-sheet-alpha.png"
    assert pngs.status_code == 200


def test_curator_page_injects_the_editing_suite(tmp_path):
    with _client(tmp_path) as client:
        _upload(client, frames=2)
        page = client.get("/curator")
        js = client.get("/curator/suite.js")
    assert "/curator/suite.js" in page.text
    assert js.status_code == 200 and "auto scale + recenter" in js.text


def test_normalize_endpoint_rewrites_frames_and_bumps_revision(tmp_path):
    with _client(tmp_path) as client:
        _upload(client, frames=2)
        before = client.get("/api/run").json()
        auto = client.post("/curator/normalize?op=auto", headers={"Origin": "http://127.0.0.1"})
        nudged = client.post(
            "/curator/normalize?op=nudge&index=0&dx=-4", headers={"Origin": "http://127.0.0.1"}
        )
        bad = client.post("/curator/normalize?op=nudge&index=zzz&dx=2", headers={"Origin": "http://127.0.0.1"})
        after = client.get("/api/run").json()
    assert auto.status_code == 200 and nudged.status_code == 200
    assert bad.status_code == 400
    assert after["runRevision"] != before["runRevision"]
    assert after["states"][0]["frames"][0]["url"] != before["states"][0]["frames"][0]["url"]


def test_download_pngs_and_gif_exports(tmp_path):
    import zipfile as _zipfile
    from io import BytesIO as _BytesIO
    from PIL import Image as _Image

    with _client(tmp_path) as client:
        _upload(client, frames=2)
        pngs = client.get("/download/pngs")
        gif = client.get("/download/gif?state=upload")
        bad = client.get("/download/nope")
    assert pngs.status_code == 200 and pngs.headers["X-Filename"].endswith(".zip")
    with _zipfile.ZipFile(_BytesIO(pngs.content)) as bundle:
        names = bundle.namelist()
        assert names == ["frame-00.png", "frame-01.png"]
        with _Image.open(_BytesIO(bundle.read(names[0]))) as frame:
            assert frame.size == (1024, 1024)  # 256 x default scale 4, nearest neighbour
    assert gif.status_code == 200 and gif.headers["Content-Type"] == "image/gif"
    with _Image.open(_BytesIO(gif.content)) as animation:
        # identical consecutive frames may be merged by the GIF encoder
        assert animation.n_frames >= 1 and animation.size == (512, 512)
    assert bad.status_code == 404


def test_gif_export_honours_fps_and_loop_overrides(tmp_path):
    with _client(tmp_path) as client:
        _upload(client, frames=2)
        gif = client.get("/download/gif?state=upload&fps=8&loop=1")
        bad = client.get("/download/gif?fps=999")
    assert gif.status_code == 200
    assert bad.status_code == 400


def test_upload_locks_chroma_key_to_the_row_background(tmp_path):
    import json as _json

    with _client(tmp_path) as client:
        _upload(client, frames=2)
        app = client.app
        project_id = app.state.curator["project_id"]
        request = _json.loads(
            (app.state.store.run_dir(project_id) / "sprite-request.json").read_text()
        )
    assert request["chroma_key"]["selection"] == "manual"


_ORIGIN = {"Origin": "http://127.0.0.1"}


def _tiny_video(tmp_path, frames=4):
    """A short near-lossless clip whose frames are the proven _strip_png sprite."""
    from io import BytesIO
    import subprocess

    import imageio_ffmpeg
    from PIL import Image

    staging = tmp_path / "clip-frames"
    staging.mkdir(exist_ok=True)
    with Image.open(BytesIO(_strip_png(frames=1))) as sprite:
        frame = sprite.convert("RGB")
    for index in range(frames):
        frame.save(staging / f"f{index:02d}.png")
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-framerate", "8",
            "-i", str(staging / "f%02d.png"), "-qp", "0", "-pix_fmt", "yuv444p", str(video),
        ],
        check=True,
    )
    return video.read_bytes()


def test_video_upload_extracts_sampled_frames(tmp_path):
    clip = _tiny_video(tmp_path)
    with _client(tmp_path) as client:
        resp = client.post(
            "/curator/video-upload?frames=3",
            files={"file": ("clip.mp4", clip, "video/mp4")},
            headers=_ORIGIN,
        )
        run = client.get("/api/run").json()
    assert resp.status_code == 201
    body = resp.json()
    assert body["project_id"] and body["video_id"]
    assert len(run["states"][0]["frames"]) >= 1


def test_video_to_row_snaps_background_to_chroma(tmp_path):
    from io import BytesIO

    from PIL import Image

    from ai_sprite_studio.curator import video_to_row
    from ai_sprite_studio.imaging import CHROMA

    row, frames = video_to_row(_tiny_video(tmp_path), 2)
    assert frames == 2
    with Image.open(BytesIO(row)) as image:
        assert image.size == (128, 64)  # two 64px cells side by side
        assert image.convert("RGB").getpixel((0, 0)) == CHROMA  # yuv-shifted green snapped exactly


def test_video_upload_validation_errors(tmp_path):
    with _client(tmp_path) as client:
        no_input = client.post("/curator/video-upload", data={}, headers=_ORIGIN)
        bad_scheme = client.post(
            "/curator/video-upload", data={"url": "ftp://example.com/a.mp4"}, headers=_ORIGIN
        )
        bad_frames = client.post(
            "/curator/video-upload?frames=0", data={"url": "https://example.com/a.mp4"}, headers=_ORIGIN
        )
        cross_site = client.post("/curator/video-upload", data={})
    assert no_input.status_code == 400 and "error" in no_input.json()
    assert bad_scheme.status_code == 400
    assert bad_frames.status_code == 400
    assert cross_site.status_code == 403


def test_video_library_save_list_serve_extract_delete(tmp_path):
    clip = _tiny_video(tmp_path)
    with _client(tmp_path) as client:
        saved = client.post(
            "/curator/video-upload?save_only=1",
            files={"file": ("clip.mp4", clip, "video/mp4")},
            data={"name": "gits-idle.mp4"},
            headers=_ORIGIN,
        )
        video_id = saved.json()["video_id"]
        listing = client.get("/curator/videos").json()
        served = client.get(f"/curator/video/{video_id}")
        extracted = client.post(
            "/curator/video-upload?frames=2", data={"video_id": video_id}, headers=_ORIGIN
        )
        missing = client.post("/curator/video-upload", data={"video_id": "nope"}, headers=_ORIGIN)
        blocked_delete = client.delete(f"/curator/video/{video_id}")
        deleted = client.delete(f"/curator/video/{video_id}", headers=_ORIGIN)
        gone = client.get(f"/curator/video/{video_id}")
    assert saved.status_code == 201
    (entry,) = listing["videos"]
    assert entry["id"] == video_id and entry["name"] == "gits-idle.mp4"
    assert entry["url"] == f"/curator/video/{video_id}" and entry["size"] == len(clip)
    assert served.status_code == 200
    assert served.headers["content-type"] == "video/mp4" and served.content == clip
    assert extracted.status_code == 201 and extracted.json()["project_id"]
    assert missing.status_code == 404
    assert blocked_delete.status_code == 403
    assert deleted.status_code == 200 and gone.status_code == 404


def test_reference_requires_api_key(tmp_path, monkeypatch):
    from ai_sprite_studio import curator

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(curator, "load_dotenv", lambda *args, **kwargs: None)
    with _client(tmp_path) as client:
        resp = client.post("/curator/reference", json={"prompt": "a ghost"}, headers=_ORIGIN)
    assert resp.status_code == 400
    assert resp.json()["error"] == "OPENAI_API_KEY is not set"


def test_reference_generates_a_png(tmp_path, monkeypatch):
    import base64

    from ai_sprite_studio import curator

    png = _strip_png(frames=1)

    class _FakeImages:
        def generate(self, **kwargs):
            assert kwargs["model"] == "gpt-image-1" and kwargs["size"] == "1024x1024"
            data = type("D", (), {"b64_json": base64.b64encode(png).decode()})()
            return type("R", (), {"data": [data]})()

    class _FakeClient:
        def __init__(self):
            self.images = _FakeImages()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(curator, "OpenAI", _FakeClient)
    with _client(tmp_path) as client:
        resp = client.post(
            "/curator/reference", json={"prompt": "pixel ghost", "size": "1024x1024"}, headers=_ORIGIN
        )
        bad_size = client.post("/curator/reference", json={"prompt": "x", "size": "512x512"}, headers=_ORIGIN)
        no_prompt = client.post("/curator/reference", json={}, headers=_ORIGIN)
        long_prompt = client.post("/curator/reference", json={"prompt": "x" * 4001}, headers=_ORIGIN)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["X-Filename"] == "reference.png" and resp.content == png
    assert bad_size.status_code == 400
    assert no_prompt.status_code == 400
    assert long_prompt.status_code == 400


def test_landing_serves_wizard_and_mode_toggle(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert 'id="mode-guided"' in page.text and 'id="mode-manual"' in page.text
    assert "/theme.js" in page.text and "/landing.js" in page.text
    assert 'max="128"' in page.text  # video frames input honours the raised cap
    # CSP-safe: the CSRF token is substituted, no leftover placeholder
    assert "__CSRF_TOKEN__" not in page.text


def test_studio_page_and_assets_are_served(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/curator/studio")
        js = client.get("/curator/studio.js")
        css = client.get("/curator/studio.css")
        theme = client.get("/theme.js")
        landing_js = client.get("/landing.js")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert theme.status_code == 200 and landing_js.status_code == 200
    assert "/api/curation" in js.text and "runRevision" in js.text


def test_studio_page_references_only_same_origin_assets(tmp_path):
    import re as _re

    with _client(tmp_path) as client:
        landing = client.get("/").text
        studio = client.get("/curator/studio").text
    for page in (landing, studio):
        for url in _re.findall(r'(?:src|href)="([^"]+)"', page):
            assert url.startswith("/") or url == "#", url


def test_studio_page_survives_without_active_run(tmp_path):
    # The page is a static shell; its JS shows the empty state when /api/run
    # reports no active run, so serving it must never require one.
    with _client(tmp_path) as client:
        page = client.get("/curator/studio")
        run = client.get("/api/run").json()
    assert page.status_code == 200 and 'id="empty-state"' in page.text
    assert run.get("error")


def test_studio_curation_payload_roundtrips_fps_loop_and_deleted(tmp_path):
    # Exactly what curator-studio.js sends: order + selected + deleted + fps/loop.
    with _client(tmp_path) as client:
        _upload(client, frames=3)
        revision = client.get("/api/run").json()["runRevision"]
        saved = client.post(
            "/api/curation",
            json={
                "version": 1,
                "kind": "sprite-gen-curation",
                "runRevision": revision,
                "states": {
                    "upload": {
                        "selected": [2, 0],
                        "order": [2, 0, 1],
                        "deleted": [1],
                        "fps": 24,
                        "loop": True,
                    }
                },
            },
            headers=_ORIGIN,
        )
        run = client.get("/api/run").json()
        gif = client.get("/download/gif?fps=24&loop=1")
    assert saved.status_code == 200
    entry = run["curation"]["states"]["upload"]
    assert entry["selected"] == [2, 0] and entry["deleted"] == [1]
    assert entry["fps"] == 24 and entry["loop"] is True
    assert gif.status_code == 200  # exports the two selected frames at studio fps
