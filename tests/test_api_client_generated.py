from pathlib import Path
import shutil
import subprocess
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_snapshot() -> Path:
    snapshot = REPO_ROOT / "openapi-v1.snapshot.json"
    if snapshot.exists():
        return snapshot
    real = REPO_ROOT / "packages/contracts/openapi-v1.json"
    if real.exists():
        return real
    pytest.skip("Neither openapi-v1.snapshot.json nor packages/contracts/openapi-v1.json found")


def test_api_client_regenerates_byte_for_byte(tmp_path: Path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed or not in PATH")

    snapshot = _find_snapshot()
    committed_api = REPO_ROOT / "packages/contracts/ts/api.ts"
    assert committed_api.exists(), "packages/contracts/ts/api.ts must be committed"

    gen_script = REPO_ROOT / "tools/gen_api_client.mjs"
    tmp_out = tmp_path / "api.ts"

    res = subprocess.run(
        [node, str(gen_script), str(snapshot), str(tmp_out)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Generator failed with code {res.returncode}: {res.stderr}"
    assert tmp_out.exists(), "Generator did not write to target output"

    expected = committed_api.read_text(encoding="utf-8")
    actual = tmp_out.read_text(encoding="utf-8")
    assert actual == expected, (
        "Regenerating from snapshot does not match committed api.ts byte-for-byte"
    )


def test_api_client_typecheck_passes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed or not in PATH")

    npm = shutil.which("npm")
    if not npm:
        pytest.skip("npm is not installed or not in PATH")

    res = subprocess.run(
        [npm, "run", "typecheck"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (
        f"typecheck failed with code {res.returncode}:\nstdout: {res.stdout}\nstderr: {res.stderr}"
    )
