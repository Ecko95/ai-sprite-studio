"""Local curator: serves the vendored sprite-gen curation UI over our engine.

The upstream curator front-end (`assets/upstream/sprite-gen/scripts/curator/`,
Apache-2.0) is served **unmodified**. It uses origin-absolute paths and was built
for a single-run server, so this adapter drives exactly one *active* project at a
time and reimplements the small HTTP contract it expects: ``GET /api/run``,
``GET /api/progress``, ``POST /api/curation`` (stale run -> 409), and
``GET /download/{kind}``, plus image byte serving.

Provider-dependent features degrade with honest errors: PNG/GIF export is Task 11
and AI frame edit is Task 6; direction groups/anchors are Task 7.
"""

from __future__ import annotations

from importlib.resources import files
import json
from uuid import UUID

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, Response

from .contracts import ProjectConfig
from .project_store import ProjectStoreError
from .sprite_engine import SpriteEngine, SpriteEngineError

_CURATOR_DIR = files("ai_sprite_studio").joinpath(
    "assets/upstream/sprite-gen/scripts/curator"
)
_ASSETS = {
    "/curator.js": ("curator.js", "text/javascript; charset=utf-8"),
    "/curator.css": ("curator.css", "text/css; charset=utf-8"),
}
# A tiny same-origin uploader (CSP `default-src 'self'` forbids inline script).
_UPLOAD_JS = b"""
const form = document.getElementById('upload');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = form.file.files[0];
  const status = document.getElementById('status');
  if (!file) { status.textContent = 'Choose an image first.'; return; }
  status.textContent = 'Uploading and extracting\\u2026';
  const query = new URLSearchParams({
    frames: form.frames.value,
    filename: file.name,
    segmentation: form.segmentation.value,
  });
  const res = await fetch('/curator/upload?' + query.toString(), {
    method: 'POST',
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
  });
  if (res.ok) { window.location = '/curator'; return; }
  let message = res.statusText;
  try { message = (await res.json()).error || message; } catch (_e) {}
  status.textContent = 'Failed: ' + message;
});
"""

_MAX_UPLOAD = 20 * 1024 * 1024


def _engine(request) -> SpriteEngine:
    return SpriteEngine(request.app.state.store)


def _active(request) -> UUID | None:
    return request.app.state.curator.get("project_id")


def _error(message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def upload_js(request) -> Response:
    return Response(_UPLOAD_JS, media_type="text/javascript; charset=utf-8")


async def curator_index(request) -> Response:
    if _active(request) is None:
        return RedirectResponse("/", status_code=303)
    return Response(
        (_CURATOR_DIR / "index.html").read_bytes(), media_type="text/html; charset=utf-8"
    )


async def curator_asset(request) -> Response:
    entry = _ASSETS.get(request.url.path)
    if entry is None:
        return _error("not found", status_code=404)
    name, media_type = entry
    return Response((_CURATOR_DIR / name).read_bytes(), media_type=media_type)


async def upload(request) -> Response:
    try:
        frames = int(request.query_params.get("frames", "1"))
        filename = request.query_params.get("filename", "upload.png")
        segmentation = request.query_params.get("segmentation", "components")
        media_type = request.headers.get("content-type", "").split(";")[0].strip()
        data = await request.body()
    except (TypeError, ValueError):
        return _error("invalid upload request", status_code=400)
    if not data or len(data) > _MAX_UPLOAD:
        return _error("invalid upload", status_code=400)
    engine = _engine(request)
    store = request.app.state.store

    def run() -> UUID:
        project = store.create(ProjectConfig(name=filename or "Upload"))
        uploaded = engine.ingest_upload(
            project.id, data, media_type=media_type, filename=filename
        )
        engine.prepare(project.id, uploaded.id, frames=frames)
        engine.extract(project.id, segmentation=segmentation)
        return project.id

    try:
        project_id = await run_in_threadpool(run)
    except (SpriteEngineError, ProjectStoreError) as exc:
        return _error(str(exc), status_code=400)
    request.app.state.curator["project_id"] = project_id
    return JSONResponse({"project_id": str(project_id)}, status_code=201)


def _run_json(view) -> dict:
    plain = list(view.plain_artifact_ids)
    frames = []
    for index, pixel_id in enumerate(view.pixel_artifact_ids):
        frame = {"index": index, "url": f"/curator/frame/{pixel_id}", "present": True}
        if index < len(plain):
            frame["plainUrl"] = f"/curator/frame/{plain[index]}"
        frames.append(frame)
    snapshot = {
        "characterId": view.character_id,
        "runDir": "run",
        "schemaVersion": 1,
        "runRevision": view.run_revision,
        "cell": view.cell,
        "states": [
            {
                "name": view.state,
                "requestFrames": view.request_frames,
                "fps": view.fps,
                "loop": view.loop,
                "extractOk": True,
                "rawPresent": True,
                "frames": frames,
            }
        ],
    }
    if view.curation is not None:
        snapshot["curation"] = view.curation
    if view.has_atlas and view.atlas_artifact_id is not None:
        snapshot["hasAtlas"] = True
        snapshot["atlas"] = {
            "url": "/run/sprite-sheet-alpha.png",
            "manifestUrl": "/run/manifest.json",
        }
    return snapshot


async def api_run(request) -> Response:
    project_id = _active(request)
    if project_id is None:
        return JSONResponse({"error": "no active run"})
    try:
        view = await run_in_threadpool(_engine(request).run_snapshot, project_id)
    except (SpriteEngineError, ProjectStoreError) as exc:
        return JSONResponse({"error": str(exc)})
    return JSONResponse(_run_json(view))


async def api_progress(request) -> Response:
    project_id = _active(request)
    if project_id is None:
        return JSONResponse({"states": []})
    try:
        view = await run_in_threadpool(_engine(request).run_snapshot, project_id)
    except (SpriteEngineError, ProjectStoreError):
        return JSONResponse({"states": []})
    state = {
        "name": view.state,
        "raw": True,
        "frames": len(view.pixel_artifact_ids),
    }
    if view.pixel_artifact_ids:
        state["frame0Url"] = f"/curator/frame/{view.pixel_artifact_ids[0]}"
    return JSONResponse({"runRevision": view.run_revision, "states": [state]})


async def api_curation(request) -> Response:
    project_id = _active(request)
    if project_id is None:
        return _error("no active run", status_code=409)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return _error("invalid curation payload", status_code=400)

    def run() -> None:
        _engine(request).stamp_curation(project_id, payload)

    try:
        await run_in_threadpool(run)
    except SpriteEngineError as exc:
        message = str(exc)
        # A changed run revision is the front-end's stale-run signal (409).
        status = 409 if "revision" in message else 400
        return _error(message, status_code=status)
    except ProjectStoreError as exc:
        return _error(str(exc), status_code=409)
    return JSONResponse({})


async def _artifact_response(request, artifact_id: UUID, media_type: str) -> Response:
    project_id = _active(request)
    if project_id is None:
        return _error("no active run", status_code=404)
    try:
        data = await run_in_threadpool(
            request.app.state.store.read_artifact_bytes, project_id, artifact_id
        )
    except ProjectStoreError:
        return _error("not found", status_code=404)
    return Response(data, media_type=media_type)


async def frame_bytes(request) -> Response:
    try:
        artifact_id = UUID(request.path_params["artifact_id"])
    except (KeyError, TypeError, ValueError):
        return _error("invalid artifact id", status_code=404)
    return await _artifact_response(request, artifact_id, "image/png")


async def run_file(request) -> Response:
    project_id = _active(request)
    if project_id is None:
        return _error("no active run", status_code=404)
    try:
        view = await run_in_threadpool(_engine(request).run_snapshot, project_id)
    except (SpriteEngineError, ProjectStoreError):
        return _error("not found", status_code=404)
    name = request.path_params["name"]
    if name == "sprite-sheet-alpha.png" and view.atlas_artifact_id is not None:
        return await _artifact_response(request, view.atlas_artifact_id, "image/png")
    if name == "manifest.json" and view.manifest_artifact_id is not None:
        return await _artifact_response(request, view.manifest_artifact_id, "application/json")
    return _error("not found", status_code=404)


async def download(request) -> Response:
    project_id = _active(request)
    if project_id is None:
        return _error("no active run", status_code=409)
    kind = request.path_params.get("kind", "")
    if kind != "atlas":
        # PNG/GIF export is Task 11; per-state gif needs the same exporter.
        return _error("export is not available yet", status_code=501)

    def run() -> bytes:
        result = _engine(request).compose(project_id)
        return request.app.state.store.read_artifact_bytes(
            project_id, result.atlas_artifact_id
        )

    try:
        data = await run_in_threadpool(run)
    except (SpriteEngineError, ProjectStoreError) as exc:
        return _error(str(exc), status_code=400)
    return Response(
        data,
        media_type="image/png",
        headers={"X-Filename": "sprite-sheet-alpha.png"},
    )
