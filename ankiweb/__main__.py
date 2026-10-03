from __future__ import annotations
import asyncio
import faulthandler
import os
import signal
from pathlib import Path
import uvicorn
from ankiweb.config import Settings
from ankiweb.collection_service import CollectionService
from ankiweb.bridge.hub import BridgeHub
from ankiweb.ankiconnect.config import AnkiConnectConfig
from ankiweb.app import create_app
from ankiweb.ankiconnect.app import create_ankiconnect_app
from ankiweb.notifier import NotifierState, DeckNotifier, snapshot


def enable_diagnostics() -> None:
    """Make a wedged server diagnosable without py-spy/ptrace (the image has neither):
    `docker kill -s USR1 anki-lan-web` then `docker logs anki-lan-web` shows every thread's
    stack. faulthandler runs at C level, so it works even when the event loop is spinning."""
    faulthandler.enable()
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)


async def _serve() -> None:
    settings = Settings.from_env()
    if (problem := settings.auth_error()):
        raise SystemExit(f"refusing to start: {problem}")
    settings.import_tmp_dir.mkdir(parents=True, exist_ok=True)
    if tmpdir := os.environ.get("TMPDIR"):
        tmp_path = Path(tmpdir)
        tmp_path.mkdir(parents=True, exist_ok=True)
        # Exports use tempfile's `tmp*` names. Remove only our stale regular files;
        # TMPDIR is configurable and may contain unrelated content.
        for stale in tmp_path.glob("tmp*"):
            if stale.is_file() or stale.is_symlink():
                stale.unlink(missing_ok=True)
    ac_config = AnkiConnectConfig.load(settings.collection_path.parent / "ankiconnect.json")
    service = CollectionService(settings)
    await service.open()
    hub = BridgeHub()
    notifier_state = NotifierState(settings.collection_path.parent / "notify.json")
    web = create_app(settings, service=service, hub=hub, notifier=notifier_state)
    # Same NotifierState instance, so /extra_actions/setNotifyConfig on :8765 edits the live
    # config that the web form (:8000) and the running notifier task share.
    api = create_ankiconnect_app(settings, service=service, config=ac_config, hub=hub,
                                 notifier=notifier_state)
    web_server = uvicorn.Server(uvicorn.Config(web, host=settings.host, port=settings.port,
                                               log_level="info"))
    api_server = uvicorn.Server(uvicorn.Config(api, host=ac_config.bind_address,
                                               port=ac_config.bind_port, log_level="info"))
    # Background deck-learnability push notifier (idle unless configured via the Extras menu).
    notifier = DeckNotifier(notifier_state, fetch=lambda: service.run(snapshot))
    notifier_task = asyncio.create_task(notifier.run())
    try:
        await asyncio.gather(web_server.serve(), api_server.serve())
    finally:
        notifier_task.cancel()
        try:
            await notifier_task
        except asyncio.CancelledError:
            pass
        await service.close()


def main() -> None:
    enable_diagnostics()
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
