"""Command-line entry point for the local application."""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import re
import socket
import sys
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


def _openai_image_bytes(prompt: str, *, model: str, quality: str) -> bytes:
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()  # picks up OPENAI_API_KEY from a local .env if present
    client = OpenAI()  # reads OPENAI_API_KEY from the environment
    result = client.images.generate(model=model, prompt=prompt, size=_CANVAS, quality=quality, n=1)
    return base64.b64decode(result.data[0].b64_json)


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

    arguments = parser.parse_args(argv)
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
        )
    serve(arguments.workspace, arguments.port)
    return 0
