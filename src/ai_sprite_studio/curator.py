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

import base64
from importlib.resources import files
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request
from uuid import UUID, uuid4
import zipfile

from dotenv import load_dotenv
import imageio_ffmpeg
from openai import OpenAI, OpenAIError
from PIL import Image, ImageChops
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, Response

from .contracts import ProjectConfig
from .imaging import CHROMA, frames_from_sheet, frames_to_gif, frames_to_row, grid_to_row, scale_nearest
from .project_store import ProjectStoreError
from .sprite_engine import SpriteEngine, SpriteEngineError

_CURATOR_DIR = files("ai_sprite_studio").joinpath(
    "assets/upstream/sprite-gen/scripts/curator"
)
_ASSETS = {
    "/curator.js": ("curator.js", "text/javascript; charset=utf-8"),
    "/curator.css": ("curator.css", "text/css; charset=utf-8"),
}
# A tiny same-origin uploader (CSP `default-src 'self'` forbids inline script/style).
# Sends multipart so one OR many frame images can be posted in a single request.
# A ticking elapsed-time indicator (text only, CSP-safe) shows it's alive, since the
# blocking upload+extract request returns no incremental progress.
_UPLOAD_JS = b"""
const form = document.getElementById('upload');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const files = form.file.files;
  const status = document.getElementById('status');
  const button = form.querySelector('button');
  if (!files.length) { status.textContent = 'Choose one or more images first.'; return; }
  const body = new FormData();
  for (const file of files) { body.append('files', file, file.name); }
  const query = new URLSearchParams({
    frames: form.frames.value,
    segmentation: form.segmentation.value,
    cols: form.cols.value,
    rows: form.rows.value,
    autosplit: form.autosplit.checked ? '1' : '0',
  });
  button.disabled = true;
  const started = Date.now();
  let tick = 0;
  const timer = setInterval(() => {
    const seconds = Math.round((Date.now() - started) / 1000);
    const dots = '.'.repeat(1 + (tick++ % 3));
    status.textContent = 'Uploading & extracting' + dots + ' (' + seconds + 's, still working)';
  }, 300);
  try {
    const res = await fetch('/curator/upload?' + query.toString(), { method: 'POST', body });
    if (res.ok) { status.textContent = 'Done \\u2014 opening curator\\u2026'; window.location = '/curator'; return; }
    let message = res.statusText;
    try { message = (await res.json()).error || message; } catch (_e) {}
    status.textContent = 'Failed: ' + message;
  } catch (err) {
    status.textContent = 'Failed: ' + err;
  } finally {
    clearInterval(timer);
    button.disabled = false;
  }
});
"""

_MAX_UPLOAD = 20 * 1024 * 1024
_MAX_VIDEO = 100 * 1024 * 1024
_REFERENCE_SIZES = {"1024x1024", "1536x1024", "1024x1536"}


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
    # The vendored index stays pristine on disk; the editing-suite script is
    # injected at serve time so upstream diffs stay clean.
    page = (_CURATOR_DIR / "index.html").read_bytes()
    page = page.replace(
        b"</body>", b'<script src="/curator/suite.js" defer></script></body>'
    )
    return Response(page, media_type="text/html; charset=utf-8")


async def suite_js(request) -> Response:
    data = files("ai_sprite_studio").joinpath("assets/curator-suite.js").read_bytes()
    return Response(data, media_type="text/javascript; charset=utf-8")


def studio_asset(name: str, media_type: str):
    """Handler factory serving one packaged asset file (same pattern as suite_js)."""

    async def handler(request) -> Response:
        data = files("ai_sprite_studio").joinpath(f"assets/{name}").read_bytes()
        return Response(data, media_type=media_type)

    return handler


async def studio_css(request) -> Response:
    data = files("ai_sprite_studio").joinpath("assets/studio.css").read_bytes()
    return Response(data, media_type="text/css; charset=utf-8")


async def normalize(request) -> Response:
    """Auto scale + recenter all frames, or nudge one frame horizontally."""
    project_id = _active(request)
    if project_id is None:
        return _error("no active run", status_code=409)
    op = request.query_params.get("op", "auto")
    nudge: tuple[int, int] | None = None
    if op == "nudge":
        try:
            nudge = (int(request.query_params["index"]), int(request.query_params["dx"]))
        except (KeyError, TypeError, ValueError):
            return _error("nudge needs integer index and dx", status_code=400)
    elif op != "auto":
        return _error("unsupported normalize op", status_code=400)

    def run() -> None:
        _engine(request).normalize(project_id, nudge=nudge)

    try:
        await run_in_threadpool(run)
    except (SpriteEngineError, ProjectStoreError) as exc:
        return _error(str(exc), status_code=400)
    return JSONResponse({})


async def curator_asset(request) -> Response:
    entry = _ASSETS.get(request.url.path)
    if entry is None:
        return _error("not found", status_code=404)
    name, media_type = entry
    return Response((_CURATOR_DIR / name).read_bytes(), media_type=media_type)


async def upload(request) -> Response:
    """Ingest one image, a sprite sheet, or many per-frame images into one snap row.

    - Multiple files      -> stitched into a 1xN row (frames = file count).
    - One file + autosplit -> frames auto-detected by background gaps (any layout).
    - One file + cols&rows -> grid reshaped into a row (frames = filled cells).
    - One file, none set   -> used as-is (a single frame or an existing row).
    """
    try:
        frames = int(request.query_params.get("frames", "1"))
        segmentation = request.query_params.get("segmentation", "components")
        cols = int(request.query_params.get("cols", "0") or 0)
        rows = int(request.query_params.get("rows", "0") or 0)
        autosplit = request.query_params.get("autosplit") in {"1", "true", "on"}
        form = await request.form()
        uploads = form.getlist("files")
        payloads = [(part.filename or "frame.png", await part.read()) for part in uploads]
    except (TypeError, ValueError):
        return _error("invalid upload request", status_code=400)
    payloads = [(name, blob) for name, blob in payloads if blob]
    if not payloads:
        return _error("choose one or more images", status_code=400)
    if sum(len(blob) for _, blob in payloads) > _MAX_UPLOAD:
        return _error("upload too large", status_code=400)

    project_name = payloads[0][0] or "Upload"
    if len(payloads) > 1:
        data, media_type, upload_name, frames = frames_to_row([blob for _, blob in payloads]), "image/png", "upload.png", len(payloads)
    elif autosplit:
        detected = frames_from_sheet(payloads[0][1])
        if not detected:
            return _error("no frames detected on the sheet (is the background a flat colour?)", status_code=400)
        data, media_type, upload_name, frames = frames_to_row(detected), "image/png", "upload.png", len(detected)
    elif cols > 0 and rows > 0:
        if not 1 <= frames <= cols * rows:
            frames = cols * rows
        data, media_type, upload_name = grid_to_row(payloads[0][1], cols=cols, rows=rows, frames=frames), "image/png", "upload.png"
    else:
        data = payloads[0][1]
        media_type = (uploads[0].content_type or "").split(";")[0].strip()
        upload_name = payloads[0][0]

    return await _finalize_row(
        request, data, media_type=media_type, filename=upload_name,
        project_name=project_name, frames=frames, segmentation=segmentation,
    )


async def _finalize_row(
    request,
    data: bytes,
    *,
    media_type: str,
    filename: str,
    project_name: str,
    frames: int,
    segmentation: str,
    extra: dict | None = None,
) -> Response:
    """Shared tail of every ingest path: row bytes -> new active curated project."""
    engine = _engine(request)
    store = request.app.state.store

    # Lock the chroma key to the actual background colour: the vendored snap's
    # auto mode may pick a different key (e.g. cyan over a green sheet) and its
    # background repaint doesn't apply on upload rows, leaving the background
    # opaque in every extracted frame.
    with Image.open(BytesIO(data)) as opened:
        corner = opened.convert("RGB").getpixel((0, 0))
    chroma_key = "#{:02X}{:02X}{:02X}".format(*corner)

    def run() -> UUID:
        project = store.create(ProjectConfig(name=project_name))
        uploaded = engine.ingest_upload(project.id, data, media_type=media_type, filename=filename)
        engine.prepare(project.id, uploaded.id, frames=frames, chroma_key=chroma_key)
        engine.extract(project.id, segmentation=segmentation)
        return project.id

    try:
        project_id = await run_in_threadpool(run)
    except (SpriteEngineError, ProjectStoreError) as exc:
        return _error(str(exc), status_code=400)
    request.app.state.curator["project_id"] = project_id
    return JSONResponse({"project_id": str(project_id), **(extra or {})}, status_code=201)


def _frame_to_chroma(data: bytes) -> bytes:
    """Snap near-background pixels to exact chroma green (same as scripts/video_to_frames.py)."""
    with Image.open(BytesIO(data)) as opened:
        image = opened.convert("RGB")
    background = image.getpixel((0, 0))
    distance = ImageChops.difference(image, Image.new("RGB", image.size, background)).convert("L")
    mask = distance.point(lambda value: 255 if value < 60 else 0)
    image.paste(CHROMA, mask=mask)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def video_to_row(data: bytes, count: int) -> tuple[bytes, int]:
    """Decode a video, evenly sample `count` frames, chroma-snap each, stitch one row."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="curator-video-") as staging:
        video = Path(staging) / "input"
        video.write_bytes(data)
        # ponytail: decode every frame then sample — avoids needing ffprobe (which
        # imageio-ffmpeg doesn't ship); switch to a select-filter pass if clips get long.
        result = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(video), "-vsync", "0", f"{staging}/f%05d.png"],
            capture_output=True, text=True,
        )
        paths = sorted(Path(staging).glob("f*.png"))
        if result.returncode != 0 or not paths:
            detail = result.stderr.strip()
            raise SpriteEngineError("could not decode video" + (f": {detail}" if detail else ""))
        step = max(1, len(paths) // count)
        sampled = paths[::step][:count]
        frames = [_frame_to_chroma(path.read_bytes()) for path in sampled]
    return frames_to_row(frames), len(frames)


def _videos_dir(request) -> Path:
    directory = Path(request.app.state.store.workspace) / "videos"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _video_index(directory: Path) -> dict[str, dict]:
    index = directory / "index.json"
    if index.is_file():
        return json.loads(index.read_text())
    return {}


def _write_video_index(directory: Path, entries: dict[str, dict]) -> None:
    (directory / "index.json").write_text(json.dumps(entries, indent=2) + "\n")


def _store_video(directory: Path, data: bytes, *, name: str, content_type: str) -> str:
    video_id = uuid4().hex
    path = directory / video_id
    path.write_bytes(data)
    entries = _video_index(directory)
    entries[video_id] = {
        "name": name,
        "size": len(data),
        "content_type": content_type,
        "created": path.stat().st_mtime,
    }
    _write_video_index(directory, entries)
    return video_id


async def video_upload(request) -> Response:
    """Ingest a video (upload, URL, or saved library id) into evenly sampled frames.

    ?frames=N (1-128, default 12) sampled evenly; ?save_only=1 stores the video in
    the library without extracting. Every new video is persisted to the library.
    """
    try:
        count = int(request.query_params.get("frames", "12"))
        if not 1 <= count <= 128:
            raise ValueError
    except (TypeError, ValueError):
        return _error("frames must be an integer between 1 and 128", status_code=400)
    save_only = request.query_params.get("save_only") in {"1", "true", "on"}
    segmentation = request.query_params.get("segmentation", "components")

    data: bytes | None = None
    name = ""
    content_type = ""
    request_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    if request_type == "application/json":
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _error("invalid JSON body", status_code=400)
        url, video_id = payload.get("url"), payload.get("video_id")
        name = str(payload.get("name") or "")
    else:
        form = await request.form()
        part = form.get("file")
        if part is not None and not isinstance(part, str):
            data = await part.read()
            name = part.filename or ""
            content_type = (part.content_type or "").split(";")[0].strip()
        url = form.get("url") if isinstance(form.get("url"), str) else None
        video_id = form.get("video_id") if isinstance(form.get("video_id"), str) else None
        name = str(form.get("name") or name)

    directory = _videos_dir(request)
    if data is None and video_id:
        entry = _video_index(directory).get(str(video_id))
        if entry is None:
            return _error("unknown video_id", status_code=404)
        video_id = str(video_id)
        data = (directory / video_id).read_bytes()
        name, content_type = name or entry["name"], entry["content_type"]
    elif data is None and url:
        url = str(url)
        if not url.startswith(("http://", "https://")):
            return _error("url must be http(s)", status_code=400)

        def fetch() -> tuple[bytes, str]:
            with urllib.request.urlopen(url) as response:
                return response.read(_MAX_VIDEO + 1), response.headers.get_content_type() or ""

        try:
            data, content_type = await run_in_threadpool(fetch)
        except (urllib.error.URLError, OSError) as exc:
            return _error(f"could not download video: {exc}", status_code=400)
        name = name or url.rsplit("/", 1)[-1].split("?")[0] or "video"
        video_id = None
    else:
        video_id = None
    if not data:
        return _error("provide a video file, url, or video_id", status_code=400)
    if len(data) > _MAX_VIDEO:
        return _error("video too large (max 100MB)", status_code=400)
    if not content_type.startswith("video/"):
        content_type = "video/webm" if name.endswith(".webm") else "video/mp4"
    if video_id is None:
        video_id = _store_video(directory, data, name=name or "video.mp4", content_type=content_type)
    if save_only:
        return JSONResponse({"video_id": video_id}, status_code=201)

    try:
        row, frames = await run_in_threadpool(video_to_row, data, count)
    except SpriteEngineError as exc:
        return _error(str(exc), status_code=400)
    return await _finalize_row(
        request, row, media_type="image/png", filename="video-row.png",
        project_name=name or "Video", frames=frames, segmentation=segmentation,
        extra={"video_id": video_id},
    )


async def videos_list(request) -> Response:
    entries = _video_index(_videos_dir(request))
    videos = [
        {
            "id": video_id,
            "name": entry["name"],
            "size": entry["size"],
            "content_type": entry["content_type"],
            "url": f"/curator/video/{video_id}",
        }
        for video_id, entry in sorted(entries.items(), key=lambda item: item[1].get("created", 0))
    ]
    return JSONResponse({"videos": videos})


async def video_file(request) -> Response:
    video_id = request.path_params["video_id"]
    directory = _videos_dir(request)
    entries = _video_index(directory)
    # Only ids present in the index are touched, so a crafted path can't escape.
    if video_id not in entries or not (directory / video_id).is_file():
        return _error("not found", status_code=404)
    if request.method == "DELETE":
        (directory / video_id).unlink(missing_ok=True)
        entries.pop(video_id, None)
        _write_video_index(directory, entries)
        return JSONResponse({})
    data = await run_in_threadpool((directory / video_id).read_bytes)
    entry = entries[video_id]
    return Response(data, media_type=entry["content_type"], headers={"X-Filename": entry["name"]})


async def reference(request) -> Response:
    """Generate a character reference PNG via the OpenAI Images API."""
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return _error("invalid JSON body", status_code=400)
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("prompt is required", status_code=400)
    if len(prompt) > 4000:
        return _error("prompt too long (max 4000 characters)", status_code=400)
    size = payload.get("size", "1024x1024")
    if size not in _REFERENCE_SIZES:
        return _error("size must be one of " + ", ".join(sorted(_REFERENCE_SIZES)), status_code=400)
    load_dotenv()  # `serve` doesn't load .env (the CLI generation commands do it per-command)
    if not os.environ.get("OPENAI_API_KEY"):
        return _error("OPENAI_API_KEY is not set", status_code=400)

    def run() -> bytes:
        result = OpenAI().images.generate(model="gpt-image-1", prompt=prompt, size=size, n=1)
        return base64.b64decode(result.data[0].b64_json)

    try:
        data = await run_in_threadpool(run)
    except OpenAIError as exc:
        return _error(str(exc), status_code=502)
    return Response(data, media_type="image/png", headers={"X-Filename": "reference.png"})


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
    engine = _engine(request)
    store = request.app.state.store

    if kind == "atlas":

        def run_atlas() -> bytes:
            result = engine.compose(project_id)
            return store.read_artifact_bytes(project_id, result.atlas_artifact_id)

        try:
            data = await run_in_threadpool(run_atlas)
        except (SpriteEngineError, ProjectStoreError) as exc:
            return _error(str(exc), status_code=400)
        return Response(
            data,
            media_type="image/png",
            headers={"X-Filename": "sprite-sheet-alpha.png"},
        )

    if kind not in {"pngs", "gifs", "gif"}:
        return _error("unknown export kind", status_code=404)
    try:
        scale = int(request.query_params.get("scale", "4" if kind == "pngs" else "2"))
        if not 1 <= scale <= 8:
            raise ValueError
    except (TypeError, ValueError):
        return _error("scale must be an integer between 1 and 8", status_code=400)
    try:
        fps = int(request.query_params.get("fps", "0"))
        if not 0 <= fps <= 60:
            raise ValueError
    except (TypeError, ValueError):
        return _error("fps must be an integer between 1 and 60", status_code=400)
    loop_param = request.query_params.get("loop")
    loop = None if loop_param is None else loop_param in {"1", "true", "on"}

    def run_export() -> tuple[bytes, str, str]:
        view = engine.run_snapshot(project_id)
        frames = [
            store.read_artifact_bytes(project_id, artifact_id)
            for artifact_id in _curated_frame_ids(view)
        ]
        if not frames:
            raise SpriteEngineError("no selected frames to export")
        if kind == "pngs":
            archive = BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for index, data in enumerate(frames):
                    bundle.writestr(f"frame-{index:02d}.png", scale_nearest(data, scale))
            return archive.getvalue(), "application/zip", "frames-1024.zip" if scale == 4 else "frames.zip"
        gif = frames_to_gif(frames, fps=fps if fps else view.fps, loop=view.loop if loop is None else loop, factor=scale)
        return gif, "image/gif", f"{view.state}.gif"

    try:
        data, media_type, filename = await run_in_threadpool(run_export)
    except (SpriteEngineError, ProjectStoreError, ValueError) as exc:
        return _error(str(exc), status_code=400)
    return Response(data, media_type=media_type, headers={"X-Filename": filename})


def _curated_frame_ids(view) -> list[UUID]:
    """Selected frame artifact ids in curated order (all frames if uncurated)."""
    pixel_ids = list(view.pixel_artifact_ids)
    entry = ((view.curation or {}).get("states") or {}).get("upload") or {}
    indices = [index for index in entry.get("order", range(len(pixel_ids))) if isinstance(index, int)]
    selected = entry.get("selected")
    deleted = set(entry.get("deleted") or ())
    if selected is not None:
        indices = [index for index in indices if index in set(selected)]
    return [pixel_ids[index] for index in indices if 0 <= index < len(pixel_ids) and index not in deleted]
