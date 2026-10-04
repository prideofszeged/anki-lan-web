from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from ankiweb.config import Settings
from ankiweb.app import create_app


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(collection_path=tmp_path / "c.anki2"))) as c:
        yield c


def _result_id(ws):
    m = ws.receive_json()
    while m.get("type") != "result":
        m = ws.receive_json()
    return m["id"]


def test_bad_json_then_valid_cmd_survives(client):
    with client.websocket_connect("/ws?context=deckbrowser") as ws:
        ws.send_text("this is not json{{{")          # malformed → must be skipped, not fatal
        ws.send_json({"type": "cmd", "id": 1, "ctx": "deckbrowser", "arg": "noop:"})
        assert _result_id(ws) == 1


def test_non_object_frame_skipped(client):
    with client.websocket_connect("/ws?context=deckbrowser") as ws:
        ws.send_json([1, 2, 3])                        # JSON array, not an object
        ws.send_json({"type": "cmd", "id": 2, "ctx": "deckbrowser", "arg": "noop:"})
        assert _result_id(ws) == 2


def test_result_frame_missing_id_skipped(client):
    with client.websocket_connect("/ws?context=deckbrowser") as ws:
        ws.send_json({"type": "result", "value": "x"})  # no 'id' → must not KeyError/drop
        ws.send_json({"type": "cmd", "id": 3, "ctx": "deckbrowser", "arg": "noop:"})
        assert _result_id(ws) == 3


def _spin_probe(client, monkeypatch, exc):
    """Make every receive on the server side fail with ``exc``; return how many times the
    handler called receive_json. A correct handler stops after the first failure; the buggy one
    spins (the 500 cap only exists so the bug shows up as a failed assertion, not a hung run)."""
    import time
    from starlette.websockets import WebSocket, WebSocketDisconnect
    calls = {"n": 0}

    async def receive_json(self, mode="text"):
        calls["n"] += 1
        if calls["n"] > 500:
            raise WebSocketDisconnect(1001)
        raise exc

    monkeypatch.setattr(WebSocket, "receive_json", receive_json)
    try:
        with client.websocket_connect("/ws?context=deckbrowser"):
            time.sleep(0.3)                  # let a spinning handler show itself
    except Exception:
        pass            # an unexpected error legitimately aborts the session (and the client sees it)
    return calls["n"]


def test_dead_socket_ends_the_handler_instead_of_spinning(client, monkeypatch):
    """Regression: Starlette raises WebSocketDisconnected (a RuntimeError, NOT a
    WebSocketDisconnect) once the socket is gone, e.g. after a failed broadcast send to a closed
    tab. A blanket `except Exception: continue` turned that into a hot loop that starved the
    event loop (container pinned at 100% CPU, /healthz timing out). The handler must stop."""
    try:
        from starlette.websockets import WebSocketDisconnected as Gone
    except ImportError:                      # older Starlette raises a plain RuntimeError
        Gone = RuntimeError
    n = _spin_probe(client, monkeypatch, Gone('WebSocket is not connected. Need to call "accept" first.'))
    assert n <= 2, f"receive loop spun {n} times on a dead socket"


def test_unexpected_receive_error_does_not_spin(client, monkeypatch):
    n = _spin_probe(client, monkeypatch, OSError("connection reset"))
    assert n <= 2, f"receive loop spun {n} times after an unexpected error"
