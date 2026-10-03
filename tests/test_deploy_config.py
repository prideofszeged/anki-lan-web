"""Static guarantees about the Compose model (SPEC section 13, ADR 0002). No containers are started."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")


def _config(*profiles: str) -> dict:
    cmd = ["docker", "compose", "config", "--format", "json"]
    for p in profiles:
        cmd[2:2] = ["--profile", p]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True,
                         env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(Path.home())})
    return json.loads(out.stdout)


def test_default_profile_runs_only_the_app():
    assert set(_config()["services"]) == {"app"}


def test_lan_profile_adds_the_proxy():
    assert set(_config("lan")["services"]) == {"app", "proxy"}


def test_app_port_is_loopback_only():
    for port in _config("lan")["services"]["app"]["ports"]:
        assert port["host_ip"] == "127.0.0.1"


def test_proxy_binds_a_specific_address_never_all_interfaces():
    ports = _config("lan")["services"]["proxy"]["ports"]
    assert ports and all(p["host_ip"] not in ("", "0.0.0.0", "::") for p in ports)
    assert {p["target"] for p in ports} == {18443}


def test_proxy_is_locked_down():
    proxy = _config("lan")["services"]["proxy"]
    assert proxy["read_only"] is True
    assert "no-new-privileges:true" in proxy["security_opt"]
    assert proxy["cap_drop"] == ["ALL"]
    assert not proxy.get("cap_add")          # listens on 18443 (unprivileged), needs nothing back
    assert not any("docker.sock" in str(v) for v in proxy.get("volumes", []))
    assert proxy["image"] == "local/anki-lan-proxy:2.8.4"
    dockerfile = (ROOT / "deploy/proxy/Dockerfile").read_text()
    assert "FROM caddy:2.8.4-alpine@sha256:" in dockerfile
    assert "caddy-unprivileged" in dockerfile


def test_proxy_waits_for_a_healthy_app():
    proxy = _config("lan")["services"]["proxy"]
    assert proxy["depends_on"]["app"]["condition"] == "service_healthy"


def test_secure_cookie_follows_env_not_hardcoded_false():
    assert "ANKIWEB_SECURE_COOKIE" in (ROOT / "compose.yaml").read_text()
    assert '"false"' not in "".join(
        line for line in (ROOT / "compose.yaml").read_text().splitlines()
        if "ANKIWEB_SECURE_COOKIE" in line)


def test_caddyfile_preserves_host_and_uses_internal_ca():
    text = (ROOT / "deploy/proxy/Caddyfile").read_text()
    assert "tls internal" in text
    assert "reverse_proxy app:8000" in text
    assert "bind 0.0.0.0" in text  # container interface; host binding is restricted by Compose
    assert "default_sni {$ANKIWEB_LAN_HOST:192.168.1.7}" in text
    assert "health_headers" in text and "Host localhost" in text
    assert "header_up Host" not in text          # Origin check compares against the real Host
    assert "admin off" in text


@pytest.mark.parametrize("script", ["backup.sh", "restore-drill.sh", "verify.sh", "pilot.sh"])
def test_scripts_are_executable_and_parse(script):
    path = ROOT / "scripts" / script
    assert path.stat().st_mode & 0o111, f"{script} is not executable"
    subprocess.run(["bash", "-n", str(path)], check=True)


def test_restore_drill_runs_with_no_network_and_no_data_mount():
    text = (ROOT / "scripts/restore-drill.sh").read_text()
    assert "--network none" in text and "--read-only" in text
    assert ":/data" not in text                               # no volume maps into the live data dir
    assert "./data" not in text and "/data/anki" not in text


def test_backup_script_delegates_to_the_tested_tool():
    text = (ROOT / "scripts/backup.sh").read_text()
    assert "ankiweb.adapters.anki.backup" in text and "prune" in text


def test_restore_drill_scratch_dir_is_not_under_tmp():
    """Docker Desktop only shares $HOME-ish paths with containers; a bare `mktemp -d` lands in
    /tmp and the bind mount is then denied ("path is not shared from the host")."""
    text = (ROOT / "scripts/restore-drill.sh").read_text()
    line = next(l for l in text.splitlines() if l.startswith("scratch="))
    assert "mktemp -d" in line and ("${drill_tmp}" in line or "ANKIWEB_DRILL_TMP" in line), line
    assert "ANKIWEB_DRILL_TMP" in text


def test_restore_drill_points_tempfile_at_its_writable_scratch_mount():
    text = (ROOT / "scripts/restore-drill.sh").read_text()
    assert '-v "${scratch}:/tmp"' in text
    assert "-e HOME=/tmp -e TMPDIR=/tmp" in text
