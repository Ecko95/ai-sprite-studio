import re

import pytest

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


def test_cli_does_not_expose_a_non_loopback_host_option():
    with pytest.raises(SystemExit):
        cli.main(["serve", "--host", "0.0.0.0"])
