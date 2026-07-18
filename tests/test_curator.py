from starlette.testclient import TestClient

from ai_sprite_studio.app import create_app
from test_sprite_engine import _strip_png


def _client(tmp_path):
    return TestClient(create_app(tmp_path), base_url="http://127.0.0.1")


def _upload(client, *, frames=2, segmentation="components"):
    return client.post(
        f"/curator/upload?frames={frames}&filename=ranger.png&segmentation={segmentation}",
        content=_strip_png(frames=frames),
        headers={"Origin": "http://127.0.0.1", "Content-Type": "image/png"},
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


def test_download_atlas_composes_and_export_defers(tmp_path):
    with _client(tmp_path) as client:
        _upload(client, frames=2)
        atlas = client.get("/download/atlas")
        pngs = client.get("/download/pngs")

    assert atlas.status_code == 200
    assert atlas.headers["content-type"] == "image/png"
    assert atlas.headers["x-filename"] == "sprite-sheet-alpha.png"
    assert pngs.status_code == 501
