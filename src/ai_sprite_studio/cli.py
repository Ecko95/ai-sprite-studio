"""Command-line entry point for the local application."""

from __future__ import annotations

import argparse
import base64
from io import BytesIO
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import webbrowser

import uvicorn

from .app import create_app

# The canonical anchor is locked to a 1024x1024 chroma-green candidate (prompt 01).
_CANVAS = "1024x1024"
# ponytail: generic reinforcements so a bare --concept works; the vendored template
# already carries the real style/silhouette discipline. Override per character.
_DEFAULT_COSTUME = "colours implied by the concept, limited 8-12 colour palette, no green clothing or accessories"
_DEFAULT_SILHOUETTE = "strong readable silhouette, compact proportions, large simple head, chunky pixel clusters"
_PROMPT_FENCE = re.compile(r"```text\n(.*?)\n```", re.S)


def default_workspace() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ai-sprite-studio"


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def serve(workspace: str | Path, port: int) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    actual_port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(url, flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        config = uvicorn.Config(create_app(workspace), host="127.0.0.1", port=actual_port, log_level="warning")
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        listener.close()


_CHROMA = (0, 255, 0)  # #00FF00 — the snap keys alpha off this exact background


def _prep_to_chroma(data: bytes, *, tolerance: int, pad: float) -> bytes:
    """Flood the border background to chroma green so the snap can key + center it.

    Only the background connected to the canvas edges is replaced, so interior
    same-colour pixels (a white shirt, the inside of a shape) survive.
    """
    from PIL import Image, ImageDraw

    with Image.open(BytesIO(data)) as opened:
        img = opened.convert("RGB")
    width, height = img.size
    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        ImageDraw.floodfill(img, corner, _CHROMA, thresh=tolerance)
    if pad > 0:
        margin = round(max(width, height) * pad)
        side = max(width, height) + 2 * margin
        canvas = Image.new("RGB", (side, side), _CHROMA)
        canvas.paste(img, ((side - width) // 2, (side - height) // 2))
        img = canvas
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def prep(*, source: Path, out: Path, tolerance: int, pad: float) -> int:
    out = Path(out)
    out.write_bytes(_prep_to_chroma(Path(source).read_bytes(), tolerance=tolerance, pad=pad))
    print(f"wrote {out}  (upload this; the snap keys + centers off {'#%02X%02X%02X' % _CHROMA})")
    return 0


def _openai_image_bytes(prompt: str, *, model: str, quality: str) -> bytes:
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()  # picks up OPENAI_API_KEY from a local .env if present
    client = OpenAI()  # reads OPENAI_API_KEY from the environment
    result = client.images.generate(model=model, prompt=prompt, size=_CANVAS, quality=quality, n=1)
    return base64.b64decode(result.data[0].b64_json)


# gpt-image can't emit the 2048x1536 hires board; 1536x1024 is its nearest 4x3-ish
# landscape. The grid stays 4x3 — the guide is a composition hint, not pixel-exact.
_POSEBOARD_SIZE = "1536x1024"
_POSEBOARD_GRID = (4, 3)  # cols, rows — from the hires preset (prompt 04)

# Short intent -> full per-frame script. This is the "curate to full spec" step:
# the pinned template supplies the pixel discipline, this supplies the motion beats.
_FRAME_SCRIPTS = {
    "idle": "A gentle {N}-frame idle loop: subtle breathing and weight shift in place. First and last frames match for a seamless loop. No stepping, no turning.",
    "attack": "A {N}-frame attack: ready stance, wind-up, strike at full extension, then recovery to the ready stance. Frame 1 and the final frame bookend the motion.",
    "hurt": "A {N}-frame hurt reaction: a sharp recoil from an impact, then settle back toward neutral. Brief and readable.",
    "jump": "A {N}-frame jump: crouch, launch upward, apex, then land and recover. Keep the character centred horizontally.",
    "death": "A {N}-frame death: stagger, lose balance, fall, and come to rest. The final frame is the resting pose.",
}
_GENERIC_SCRIPT = "A {N}-frame {ACTION} sequence that reads as one coherent short animation, with the first and last frames bookending the motion."
_DEFAULT_ACTION_CONSTRAINTS = "Keep body proportions, palette, and outfit identical across every frame. No motion blur, no smear frames, no weapon trails. Each pose sits fully inside its cell area."


def _codex_image_bytes(prompt: str, *, images: tuple[bytes, ...] = (), timeout: int = 900) -> bytes:
    """Generate via `codex exec` built-in image_gen — ChatGPT login, no API key.

    ponytail: codex reasons before the image tool fires, so expect ~4-6 min/image.
    The built-in tool saves under $CODEX_HOME/generated_images; we coax a copy to an
    exact path via the prompt and read it, falling back to the newest generated file.
    """
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        target = work / "out.png"
        image_flags: list[str] = []
        for index, data in enumerate(images):
            path = work / f"input_{index}.png"
            path.write_bytes(data)
            image_flags += ["-i", str(path)]
        instruction = (
            f"$imagegen {prompt}\n\n"
            f"Save the single final PNG to exactly {target} (copy it there). "
            "Do not ask for confirmation. Report the saved path."
        )
        before = _codex_generated_pngs()
        subprocess.run(
            ["codex", "exec", "-s", "workspace-write", "-C", str(work), "--skip-git-repo-check", *image_flags, instruction],
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        if target.exists():
            return target.read_bytes()
        fresh = [path for path in _codex_generated_pngs() if path not in before]
        if fresh:
            return max(fresh, key=lambda path: path.stat().st_mtime).read_bytes()
        raise RuntimeError("codex exec produced no image (is `codex login` done and image_gen available?)")


def _codex_generated_pngs() -> list[Path]:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return list((home / "generated_images").glob("*.png"))


def _codex_generate(prompt: str, *, model: str, quality: str) -> bytes:
    return _codex_image_bytes(prompt)


def _codex_edit(prompt: str, images: list[bytes], *, model: str, size: str, quality: str) -> bytes:
    return _codex_image_bytes(prompt, images=tuple(images))


def _openai_edit_bytes(prompt: str, images: list[bytes], *, model: str, size: str, quality: str) -> bytes:
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()
    client = OpenAI()
    files = [(f"image_{index}.png", data, "image/png") for index, data in enumerate(images)]
    result = client.images.edit(model=model, image=files, prompt=prompt, size=size, quality=quality, n=1)
    return base64.b64decode(result.data[0].b64_json)


def genactions(
    *,
    workspace: str | Path,
    project_id: str,
    state_id: str,
    direction: str,
    anchor: Path | None,
    frames_desc: str | None,
    constraints: str,
    out: Path,
    model: str,
    quality: str,
    _generate=_openai_edit_bytes,
) -> int:
    from .project_store import ProjectStore
    from .prompts import guide_for_stage, render_prompt

    store = ProjectStore(workspace)
    project = store.load(project_id)

    states = {state.id: state for state in project.states}
    if state_id not in states:
        print(f"unknown state {state_id!r}; project has: {', '.join(states)}", file=sys.stderr)
        return 1
    state = states[state_id]

    # Anchor bytes: an explicit snapped PNG, else the project's latest ingested input.
    inputs = [a for a in project.artifacts if a.kind == "input" and not a.stale]
    anchor_ref = None
    if anchor is not None:
        anchor_bytes = Path(anchor).read_bytes()
    elif inputs:
        anchor_ref = inputs[-1]
        anchor_bytes = store.read_artifact_bytes(project.id, anchor_ref.id)
    else:
        print("no anchor: pass --anchor or ingest a base image first", file=sys.stderr)
        return 1

    from .prompts import _ACTION_DIRECTION_DESCRIPTIONS  # locked direction phrasing

    cols, rows = _POSEBOARD_GRID
    width, height = (int(part) for part in _POSEBOARD_SIZE.split("x"))
    script = (frames_desc or _FRAME_SCRIPTS.get(state_id, _GENERIC_SCRIPT)).format(N=state.frames, ACTION=state_id)
    rendered = render_prompt(
        "action_poseboards",
        project,
        {
            "ACTION": state_id,
            "N": state.frames,
            "COLS": cols,
            "ROWS": rows,
            "CANVAS_W": width,
            "CANVAS_H": height,
            "DIRECTION_DESCRIPTION": _ACTION_DIRECTION_DESCRIPTIONS[direction],
            "FRAME_BY_FRAME_DESCRIPTION": script,
            "ACTION_SPECIFIC_CONSTRAINTS": constraints,
        },
        direction=direction,
        state=state,
        artifact_inputs=[anchor_ref] if anchor_ref is not None else (),
    )
    match = _PROMPT_FENCE.search(rendered.text)
    if match is None:  # pragma: no cover - pinned template always carries the fence
        print("action_poseboards template is missing its prompt block", file=sys.stderr)
        return 1
    image_prompt = match.group(1).strip()

    guide = guide_for_stage("action_poseboards")
    data = _generate(image_prompt, [anchor_bytes, guide.data], model=model, size=_POSEBOARD_SIZE, quality=quality)

    out = Path(out)
    out.write_bytes(data)
    out.with_suffix(out.suffix + ".prompt.md").write_text(rendered.text, encoding="utf-8")
    print(f"wrote {out}  ({state.frames}-frame {state_id} pose board, {direction})")
    print("next: recover + snap the frames (frame_recovery stage) — do NOT grid-crop; poses cross cell borders")
    return 0


def genbase(
    *,
    workspace: str | Path,
    concept: str,
    costume: str,
    silhouette: str,
    name: str,
    out: Path,
    model: str,
    quality: str,
    project_id: str | None,
    _generate=_openai_image_bytes,
) -> int:
    from .contracts import ProjectConfig
    from .project_store import ProjectStore
    from .prompts import render_prompt
    from .sprite_engine import SpriteEngine

    store = ProjectStore(workspace)
    project = store.load(project_id) if project_id else store.create(ProjectConfig(name=name))

    rendered = render_prompt(
        "base_generation",
        project,
        {"CORE_IDENTITY": concept, "COSTUME_AND_PALETTE": costume, "SILHOUETTE_NOTES": silhouette},
    )
    match = _PROMPT_FENCE.search(rendered.text)
    if match is None:  # pragma: no cover - pinned template always carries the fence
        print("base_generation template is missing its prompt block", file=sys.stderr)
        return 1
    image_prompt = match.group(1).strip()

    data = _generate(image_prompt, model=model, quality=quality)

    out = Path(out)
    out.write_bytes(data)
    out.with_suffix(out.suffix + ".prompt.md").write_text(rendered.text, encoding="utf-8")

    engine = SpriteEngine(store)
    artifact = engine.ingest_upload(project.id, data, media_type="image/png", filename=out.name)

    print(f"project {project.id}")
    print(f"wrote {out} (input artifact {artifact.id})")
    print(f"next: engine.prepare({project.id!r}, {str(artifact.id)!r}, frames=1) then engine.extract(...)  # snaps to grid")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-sprite-studio")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--workspace", type=Path, default=default_workspace())
    serve_parser.add_argument("--port", type=_port, default=0)

    gen_parser = commands.add_parser("genbase", help="generate a chroma-green base character with gpt-image and ingest it for snapping")
    gen_parser.add_argument("--workspace", type=Path, default=default_workspace())
    gen_parser.add_argument("--concept", required=True, help="character identity, e.g. 'young pirate boy in a black tricorn hat'")
    gen_parser.add_argument("--costume", default=_DEFAULT_COSTUME, help="costume and palette notes")
    gen_parser.add_argument("--silhouette", default=_DEFAULT_SILHOUETTE, help="silhouette notes")
    gen_parser.add_argument("--name", default="Untitled sprite", help="project name (ignored with --project)")
    gen_parser.add_argument("--project", dest="project_id", default=None, help="existing project id to reuse")
    gen_parser.add_argument("--out", type=Path, default=Path("base.png"))
    gen_parser.add_argument("--model", default="gpt-image-1")
    gen_parser.add_argument("--quality", default="high", choices=["low", "medium", "high", "auto"])
    gen_parser.add_argument("--provider", default="openai", choices=["openai", "codex"], help="codex uses your ChatGPT login via `codex exec` (no API key)")

    act_parser = commands.add_parser("genactions", help="build the full-spec action-sheet prompt and generate a pose board with gpt-image")
    act_parser.add_argument("--workspace", type=Path, default=default_workspace())
    act_parser.add_argument("--project", dest="project_id", required=True, help="project id holding the base/anchor")
    act_parser.add_argument("--state", dest="state_id", required=True, help="action state, e.g. idle/attack/hurt/jump/death")
    act_parser.add_argument("--direction", default="down", choices=["down"], help="only 'down' until directional anchors exist")
    act_parser.add_argument("--anchor", type=Path, default=None, help="snapped anchor PNG; defaults to the project's latest ingested input")
    act_parser.add_argument("--frames-desc", dest="frames_desc", default=None, help="override the per-frame motion script ({N}/{ACTION} allowed)")
    act_parser.add_argument("--constraints", default=_DEFAULT_ACTION_CONSTRAINTS)
    act_parser.add_argument("--out", type=Path, default=Path("poseboard.png"))
    act_parser.add_argument("--model", default="gpt-image-1")
    act_parser.add_argument("--quality", default="high", choices=["low", "medium", "high", "auto"])
    act_parser.add_argument("--provider", default="openai", choices=["openai", "codex"], help="codex uses your ChatGPT login via `codex exec` (no API key)")

    prep_parser = commands.add_parser("prep", help="strip a flat background to chroma green so an existing image snaps cleanly")
    prep_parser.add_argument("--in", dest="source", type=Path, required=True)
    prep_parser.add_argument("--out", type=Path, default=Path("prepped.png"))
    prep_parser.add_argument("--tolerance", type=int, default=40, help="colour distance treated as background (0-255)")
    prep_parser.add_argument("--pad", type=float, default=0.1, help="green margin as a fraction of the longest side; 0 keeps size")

    arguments = parser.parse_args(argv)
    if arguments.command == "prep":
        return prep(source=arguments.source, out=arguments.out, tolerance=arguments.tolerance, pad=arguments.pad)
    if arguments.command == "genactions":
        return genactions(
            workspace=arguments.workspace,
            project_id=arguments.project_id,
            state_id=arguments.state_id,
            direction=arguments.direction,
            anchor=arguments.anchor,
            frames_desc=arguments.frames_desc,
            constraints=arguments.constraints,
            out=arguments.out,
            model=arguments.model,
            quality=arguments.quality,
            _generate=_codex_edit if arguments.provider == "codex" else _openai_edit_bytes,
        )
    if arguments.command == "genbase":
        return genbase(
            workspace=arguments.workspace,
            concept=arguments.concept,
            costume=arguments.costume,
            silhouette=arguments.silhouette,
            name=arguments.name,
            out=arguments.out,
            model=arguments.model,
            quality=arguments.quality,
            project_id=arguments.project_id,
            _generate=_codex_generate if arguments.provider == "codex" else _openai_image_bytes,
        )
    serve(arguments.workspace, arguments.port)
    return 0
