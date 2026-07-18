import http.client
import io
import os
import re
import select
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

import pytest
from PIL import Image

from ai_sprite_studio import cli


def test_serve_reserves_a_loopback_port_and_opens_its_exact_url(tmp_path, monkeypatch, capsys):
    observed = {}
    opened = []

    class FakeServer:
        def __init__(self, config):
            observed["config"] = config

        def run(self, *, sockets):
            observed["address"] = sockets[0].getsockname()

    monkeypatch.setattr("ai_sprite_studio.cli.uvicorn.Server", FakeServer)
    monkeypatch.setattr("ai_sprite_studio.cli.webbrowser.open", opened.append)

    cli.serve(tmp_path, 0)

    url = capsys.readouterr().out.strip()
    assert re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]*/", url)
    assert opened == [url]
    assert observed["config"].host == "127.0.0.1"
    assert observed["address"] == ("127.0.0.1", int(url.split(":")[2][:-1]))


def test_genbase_renders_the_prompt_and_ingests_the_generated_image(tmp_path):
    from ai_sprite_studio.contracts import ProjectConfig
    from ai_sprite_studio.project_store import ProjectStore

    store = ProjectStore(tmp_path / "ws")
    project = store.create(ProjectConfig(name="Pirate"))

    captured = {}

    def fake_generate(prompt, *, model, quality):
        captured.update(prompt=prompt, model=model, quality=quality)
        buffer = io.BytesIO()
        Image.new("RGB", (1024, 1024), "#00FF00").save(buffer, format="PNG")
        return buffer.getvalue()

    out = tmp_path / "base.png"
    code = cli.genbase(
        workspace=tmp_path / "ws",
        concept="young pirate boy in a black tricorn hat",
        costume=cli._DEFAULT_COSTUME,
        silhouette=cli._DEFAULT_SILHOUETTE,
        name="ignored",
        out=out,
        model="gpt-image-1",
        quality="high",
        project_id=str(project.id),
        _generate=fake_generate,
    )

    assert code == 0
    # The concept is threaded into the pinned prompt, not the surrounding provenance doc.
    assert "young pirate boy in a black tricorn hat" in captured["prompt"]
    assert "#00FF00" in captured["prompt"] and "```" not in captured["prompt"]
    assert captured["model"] == "gpt-image-1" and captured["quality"] == "high"
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert out.with_suffix(".png.prompt.md").exists()

    # The generated image is a real, snappable input artifact in the store.
    reloaded = store.load(project.id)
    inputs = [a for a in reloaded.artifacts if a.kind == "input"]
    assert len(inputs) == 1


def test_cli_does_not_expose_a_non_loopback_host_option():
    with pytest.raises(SystemExit):
        cli.main(["serve", "--host", "0.0.0.0"])


def test_real_cli_port_zero_prints_a_serving_local_url(tmp_path):
    environment = os.environ | {"BROWSER": "/bin/true"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from ai_sprite_studio.cli import main; raise SystemExit(main())",
            "serve",
            "--workspace",
            str(tmp_path),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, "CLI did not print its local URL"
        url = process.stdout.readline().strip()
        assert re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]*/", url)

        parsed = urlsplit(url)
        deadline = time.monotonic() + 5
        while True:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=1)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                body = response.read().decode()
                break
            except (ConnectionError, OSError, http.client.HTTPException):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
            finally:
                connection.close()

        assert response.status == 200
        assert "AI Sprite Studio" in body
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    assert process.poll() is not None
