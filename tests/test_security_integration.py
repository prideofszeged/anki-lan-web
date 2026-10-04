"""Real-browser proof of the V8 origin check.

``SameSite=Strict`` ignores ports, so a page on 127.0.0.1:B *is* same-site with the app on
127.0.0.1:A and the browser would attach the session cookie. Only the Origin check stops it.
"""
import http.server
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from ankiweb.app import create_app
from ankiweb.config import Settings

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def app_url(tmp_path: Path):
    port = _free_port()
    settings = Settings(collection_path=tmp_path / "c.anki2", port=port, password="secret")
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port,
                                           log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def evil_url():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>evil</body></html>")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


_POST = "(u) => fetch(u, {method:'POST', mode:'no-cors', credentials:'include', " \
        "headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'x=1'})"


def test_sibling_port_page_cannot_post_with_victims_session(app_url, evil_url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # log in through the real form (a same-origin browser POST must pass the check)
        page.goto(f"{app_url}/login")
        page.fill("input[name=password]", "secret")
        page.click("button[type=submit]")
        page.wait_for_url(f"{app_url}/**")
        assert page.request.get(f"{app_url}/deckbrowser").status == 200

        # same-origin POST from the app's own page succeeds
        with page.expect_response(f"{app_url}/notify") as ok:
            page.evaluate(_POST, f"{app_url}/notify")
        assert ok.value.status != 403

        # the sibling-port page is same-site, so the cookie rides along; the server must refuse
        page.goto(evil_url)
        with page.expect_response(f"{app_url}/notify") as bad:
            page.evaluate(_POST, f"{app_url}/notify")
        assert bad.value.status == 403
        browser.close()
