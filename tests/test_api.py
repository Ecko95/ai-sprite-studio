import re

from starlette.testclient import TestClient

from ai_sprite_studio.app import create_app


def test_root_bootstraps_a_same_site_session_with_local_security_headers(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "same_site" not in response.headers.get("set-cookie", "").lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in response.headers
    assert re.search(r'<meta name="csrf-token" content="[^\"]+">', response.text)


def test_projects_list_is_available_under_the_versioned_api(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/projects")

    assert response.status_code == 200
    assert response.json() == {"projects": []}


def test_project_creation_requires_same_loopback_origin_and_session_csrf(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        root = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^\"]+)">', root.text).group(1)

        missing_origin = client.post("/api/v1/projects", json={"name": "Guarded ranger"})
        missing_csrf = client.post(
            "/api/v1/projects",
            json={"name": "Guarded ranger"},
            headers={"Origin": "http://127.0.0.1"},
        )
        created = client.post(
            "/api/v1/projects",
            json={"name": "Guarded ranger"},
            headers={"Origin": "http://127.0.0.1", "X-CSRF-Token": csrf},
        )

    assert missing_origin.status_code == 403
    assert set(missing_origin.json()) == {"code", "message", "retryable", "details"}
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["code"] == "csrf_failed"
    assert created.status_code == 201
    assert created.json()["name"] == "Guarded ranger"


def test_project_detail_reads_and_patches_a_project_without_exposing_bad_ids(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        root = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^\"]+)">', root.text).group(1)
        headers = {"Origin": "http://127.0.0.1", "X-CSRF-Token": csrf}
        created = client.post("/api/v1/projects", json={"name": "Patch ranger"}, headers=headers)
        project_id = created.json()["id"]

        loaded = client.get(f"/api/v1/projects/{project_id}")
        patched = client.patch(
            f"/api/v1/projects/{project_id}", json={"name": "Renamed ranger"}, headers=headers
        )
        invalid = client.get("/api/v1/projects/not-a-uuid")

    assert loaded.status_code == 200
    assert loaded.json()["name"] == "Patch ranger"
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed ranger"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_project_id"
    assert "/" not in invalid.json()["message"]


def test_job_routes_create_read_replay_and_cancel(tmp_path):
    async def handler(_job, cancel_requested):
        await cancel_requested.wait()

    with TestClient(create_app(tmp_path, handler=handler), base_url="http://127.0.0.1") as client:
        root = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^\"]+)">', root.text).group(1)
        headers = {"Origin": "http://127.0.0.1", "X-CSRF-Token": csrf}
        project = client.post("/api/v1/projects", json={"name": "Job ranger"}, headers=headers)
        project_id = project.json()["id"]

        created = client.post(
            f"/api/v1/projects/{project_id}/jobs",
            json={"command": "run_qa", "payload": {"state": "idle"}},
            headers=headers,
        )
        job_id = created.json()["id"]
        loaded = client.get(f"/api/v1/jobs/{job_id}")
        canceled = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)
        events = client.get(f"/api/v1/jobs/{job_id}/events", headers={"Last-Event-ID": "0"})
        replay = client.get(f"/api/v1/jobs/{job_id}/events", headers={"Last-Event-ID": "1"})

    assert created.status_code == 201
    assert loaded.status_code == 200
    assert loaded.json()["id"] == job_id
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "id: 1\nevent: job\ndata: " in events.text
    assert f'"id":"{job_id}"' in events.text
    assert "id: 1\nevent: job\ndata: " not in replay.text
    assert "id: 2\nevent: job\ndata: " in replay.text
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "cancel_requested"


def test_job_routes_reject_malformed_job_ids(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        root = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^\"]+)">', root.text).group(1)
        responses = (
            client.get("/api/v1/jobs/not-a-uuid"),
            client.get("/api/v1/jobs/not-a-uuid/events"),
            client.post(
                "/api/v1/jobs/not-a-uuid/cancel",
                headers={"Origin": "http://127.0.0.1", "X-CSRF-Token": csrf},
            ),
        )

    for response in responses:
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_job_id"


def test_api_rejects_external_hosts_and_bad_requests_with_the_stable_error_shape(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        root = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^\"]+)">', root.text).group(1)
        external_host = client.get("/api/v1/projects", headers={"Host": "example.test"})
        missing = client.get("/api/v1/does-not-exist")
        malformed = client.post(
            "/api/v1/projects",
            json={"name": "Bad ranger", "unexpected": True},
            headers={"Origin": "http://127.0.0.1", "X-CSRF-Token": csrf},
        )

    for response in (external_host, missing, malformed):
        assert set(response.json()) == {"code", "message", "retryable", "details"}
        assert "/tmp/" not in response.text
    assert external_host.status_code == 400
    assert "access-control-allow-origin" not in external_host.headers
    assert missing.status_code == 404
    assert malformed.status_code == 422


def test_http_and_unexpected_errors_keep_local_security_headers(tmp_path):
    async def explode(_request):
        raise RuntimeError("unexpected")

    app = create_app(tmp_path)
    app.add_route("/api/v1/explode", explode)
    with TestClient(
        app, base_url="http://127.0.0.1", raise_server_exceptions=False
    ) as client:
        missing = client.get("/api/v1/missing")
        unexpected = client.get("/api/v1/explode")

    for response in (missing, unexpected):
        assert response.headers["content-security-policy"].startswith("default-src 'self'")
        assert response.headers["x-content-type-options"] == "nosniff"

    assert missing.status_code == 404
    assert unexpected.status_code == 500


def test_unsafe_api_requests_reject_a_different_loopback_origin(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        root = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^\"]+)">', root.text).group(1)
        response = client.post(
            "/api/v1/projects",
            json={"name": "Origin ranger"},
            headers={"Origin": "http://localhost", "X-CSRF-Token": csrf},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "invalid_origin"
