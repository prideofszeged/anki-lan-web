import os
from pathlib import Path
from ankiweb.config import Settings


def test_lang_defaults_to_empty(tmp_path: Path):
    s = Settings(collection_path=tmp_path / "c.anki2")
    assert s.lang == ""


def test_from_env_reads_ankiweb_lang(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ANKIWEB_COLLECTION", str(tmp_path / "c.anki2"))
    monkeypatch.setenv("ANKIWEB_LANG", "zh-CN")
    s = Settings.from_env()
    assert s.lang == "zh-CN"


def test_from_env_lang_absent_is_empty(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ANKIWEB_COLLECTION", str(tmp_path / "c.anki2"))
    monkeypatch.delenv("ANKIWEB_LANG", raising=False)
    s = Settings.from_env()
    assert s.lang == ""


def test_multi_user_runtime_settings_from_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ANKIWEB_COLLECTION", str(tmp_path / "legacy.anki2"))
    monkeypatch.setenv("ANKIWEB_MULTI_USER", "true")
    monkeypatch.setenv("ANKIWEB_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("ANKIWEB_MAX_ACTIVE_RUNTIMES", "7")
    monkeypatch.setenv("ANKIWEB_RUNTIME_IDLE_SECONDS", "120")
    monkeypatch.setenv("ANKIWEB_RUNTIME_WAIT_SECONDS", "9")
    monkeypatch.setenv("ANKIWEB_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,172.16.0.0/12")
    monkeypatch.setenv("ANKIWEB_INSECURE_COOKIE_OK", "true")
    settings = Settings.from_env()
    assert settings.multi_user is True
    assert settings.effective_data_root == tmp_path / "data"
    assert settings.max_active_runtimes == 7
    assert settings.runtime_idle_seconds == 120
    assert settings.runtime_wait_seconds == 9
    assert settings.trusted_proxy_cidrs == ("127.0.0.0/8", "172.16.0.0/12")
    assert settings.insecure_cookie_ok is True
