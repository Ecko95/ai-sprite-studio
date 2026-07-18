"""Browser smoke test: the redesigned landing and Curator Studio render under CSP."""

import socket
import threading
import time

import pytest
import uvicorn

from ai_sprite_studio.app import create_app

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture()
def server_url(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(create_app(tmp_path), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            pytest.fail("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_landing_and_studio_render(server_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright.Error as exc:  # browser build not installed for this playwright version
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0]}")
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(server_url + "/")
        page.wait_for_selector("#mode-guided")
        assert page.get_attribute("html", "data-theme") in {"light", "dark"}
        # wizard composes the reference prompt on load (JS ran despite CSP)
        assert "chroma green" in page.input_value("#ref-prompt")
        page.click("#mode-manual")
        assert page.is_visible("#upload")
        page.goto(server_url + "/curator/studio")
        page.wait_for_selector("#empty-state:not([hidden])")  # no active run -> empty state
        assert not errors, errors
        browser.close()
