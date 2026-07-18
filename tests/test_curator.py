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
