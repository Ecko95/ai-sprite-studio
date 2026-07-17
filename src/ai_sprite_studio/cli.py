"""Command-line entry point for the local application."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import sys
import webbrowser

import uvicorn

from .app import create_app


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-sprite-studio")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--workspace", type=Path, default=default_workspace())
    serve_parser.add_argument("--port", type=_port, default=0)
    arguments = parser.parse_args(argv)
    serve(arguments.workspace, arguments.port)
    return 0
