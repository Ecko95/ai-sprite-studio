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


def test_genactions_builds_the_action_prompt_and_feeds_anchor_plus_guide(tmp_path):
    from ai_sprite_studio.contracts import ProjectConfig
    from ai_sprite_studio.project_store import ProjectStore
    from ai_sprite_studio.sprite_engine import SpriteEngine

    store = ProjectStore(tmp_path / "ws")
    project = store.create(ProjectConfig(name="Hero"))

    buffer = io.BytesIO()
    Image.new("RGB", (1024, 1024), "#00FF00").save(buffer, format="PNG")
    anchor = SpriteEngine(store).ingest_upload(
        project.id, buffer.getvalue(), media_type="image/png", filename="base.png"
    )

    captured = {}

    def fake_generate(prompt, images, *, model, size, quality):
        captured.update(prompt=prompt, images=images, size=size)
        out = io.BytesIO()
        Image.new("RGB", (1536, 1024), "#00FF00").save(out, format="PNG")
        return out.getvalue()

    out = tmp_path / "attack.png"
    code = cli.genactions(
        workspace=tmp_path / "ws",
        project_id=str(project.id),
        state_id="attack",
        direction="down",
        anchor=None,  # falls back to the ingested input
        frames_desc=None,
        constraints=cli._DEFAULT_ACTION_CONSTRAINTS,
        out=out,
        model="gpt-image-1",
        quality="high",
        _generate=fake_generate,
    )

    assert code == 0
    # Short intent expanded into the pinned full-spec prompt.
    assert "attack" in captured["prompt"] and "4-frame" in captured["prompt"]
    assert "front view, facing down" in captured["prompt"]
    assert captured["size"] == "1536x1024"
    # gpt-image edit receives BOTH the anchor and the pinned pose-board guide.
    assert len(captured["images"]) == 2 and captured["images"][0] == store.read_artifact_bytes(project.id, anchor.id)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert out.with_suffix(".png.prompt.md").exists()


def test_genbase_dry_run_emits_prompt_without_provider_or_store(tmp_path, capsys):
    def boom(*args, **kwargs):  # must never be called in dry-run
        raise AssertionError("provider was called during --dry-run")

    out = tmp_path / "base.png"
    code = cli.genbase(
        workspace=tmp_path / "ws",  # must NOT be created
        concept="young pirate boy in a black tricorn hat",
        costume=cli._DEFAULT_COSTUME,
        silhouette=cli._DEFAULT_SILHOUETTE,
        name="Pirate",
        out=out,
        model="gpt-image-1",
        quality="high",
        project_id=None,
        dry_run=True,
        _generate=boom,
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert "young pirate boy in a black tricorn hat" in printed and "#00FF00" in printed
    assert not out.exists()  # no image written
    assert not (tmp_path / "ws").exists()  # no project/store side effects
    assert out.with_suffix(".png.prompt.md").read_text()  # provenance sidecar written


def test_prep_floods_border_background_to_chroma_and_keeps_interior(tmp_path):
    # White canvas, black frame, white interior hole — the hole must survive.
    img = Image.new("RGB", (40, 40), "white")
    for x in range(10, 30):
        for y in range(10, 30):
            img.putpixel((x, y), (0, 0, 0) if x in (10, 29) or y in (10, 29) else (255, 255, 255))
    src = tmp_path / "in.png"
    img.save(src)

    out = tmp_path / "out.png"
    assert cli.prep(source=src, out=out, tolerance=40, pad=0.25) == 0

    result = Image.open(out).convert("RGB")
    assert result.width == result.height  # padded to square
    assert result.getpixel((0, 0)) == (0, 255, 0)  # border background is now chroma
    # Interior white hole (border-disconnected) is untouched.
    cx = (result.width - 40) // 2 + 20
    cy = (result.height - 40) // 2 + 20
    assert result.getpixel((cx, cy)) == (255, 255, 255)


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
