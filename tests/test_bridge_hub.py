import asyncio
import pytest
from ankiweb.bridge.hub import BridgeHub


class FakeWS:
    def __init__(self):
        self.sent = []
    async def send_json(self, obj):
        self.sent.append(obj)


async def test_register_and_broadcast_opchanges():
    hub = BridgeHub()
    ws = FakeWS()
    hub.register("deckbrowser", ws)
    await hub.broadcast_opchanges({"study_queues": True}, initiator="x")
    assert ws.sent == [{"type": "opchanges", "flags": {"study_queues": True}, "initiator": "x"}]
    hub.unregister("deckbrowser", ws)
    await hub.broadcast_opchanges({"note": True}, initiator=None)
    assert len(ws.sent) == 1  # no longer receives


async def test_push_call_to_context():
    hub = BridgeHub()
    ws = FakeWS()
    hub.register("reviewer", ws)
    await hub.push_call("reviewer", "_showQuestion", ["q", "a", "card card1"])
    assert ws.sent[0]["type"] == "call"
    assert ws.sent[0]["fn"] == "_showQuestion"
    assert ws.sent[0]["args"] == ["q", "a", "card card1"]


class _FakeWs:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.sent = fail, []

    async def send_json(self, msg) -> None:
        if self.fail:
            raise RuntimeError('Unexpected ASGI message "websocket.send", after sending "websocket.close"')
        self.sent.append(msg)


async def test_dead_socket_does_not_block_delivery_to_the_others():
    hub = BridgeHub()
    dead, live = _FakeWs(fail=True), _FakeWs()
    hub.register("deckbrowser", dead)
    hub.register("deckbrowser", live)
    await hub.push_call("deckbrowser", "ankiwebReload", [])      # must not raise
    assert [m["fn"] for m in live.sent] == ["ankiwebReload"]


async def test_dead_socket_is_dropped_so_it_is_not_retried_forever():
    hub = BridgeHub()
    dead, live = _FakeWs(fail=True), _FakeWs()
    hub.register("a", dead)
    hub.register("b", live)
    await hub.broadcast_opchanges({"deck": True}, initiator=None)  # must not raise
    assert dead not in hub._conns["a"]
    assert len(live.sent) == 1


async def test_eval_callback_is_owned_by_exact_connection():
    hub = BridgeHub()
    first, second = FakeWS(), FakeWS()
    first_id = hub.register("reviewer", first)
    second_id = hub.register("reviewer", second)
    waiting = asyncio.create_task(
        hub.eval_with_callback("reviewer", "answer()", connection_id=first_id)
    )
    await asyncio.sleep(0)
    message_id = first.sent[-1]["id"]
    assert not second.sent

    hub.resolve(message_id, "spoofed", second_id)
    await asyncio.sleep(0)
    assert not waiting.done()
    hub.resolve(message_id, "accepted", first_id)
    assert await waiting == "accepted"


async def test_disconnect_cancels_only_its_pending_callback():
    hub = BridgeHub()
    ws = FakeWS()
    connection_id = hub.register("reviewer", ws)
    waiting = asyncio.create_task(
        hub.eval_with_callback("reviewer", "answer()", connection_id=connection_id)
    )
    await asyncio.sleep(0)
    hub.unregister("reviewer", ws, connection_id)
    with pytest.raises(asyncio.CancelledError):
        await waiting
