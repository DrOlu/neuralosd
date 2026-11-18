"""Self-update: version math, install detection, verified binary swap.

No network: every fetch/open is injected. The swap tests run on a temporary
file standing in for the executable.
"""
import hashlib
import io
import json
import pathlib
import os
import stat

import pytest

from neuralosd import update as up


# ── versions ─────────────────────────────────────────────────────────────

def test_version_ordering_is_numeric_and_pre_aware():
    assert up.parse_version("3.10.0") > up.parse_version("3.9.9")
    assert up.parse_version("3.4.0") > up.parse_version("3.4.0rc2")
    assert up.parse_version("3.4.0rc2") > up.parse_version("3.4.0b10")
    assert up.parse_version("3.4.0b10") > up.parse_version("3.4.0a9")
    assert up.parse_version("v3.4.0+local") == up.parse_version("3.4.0")
    assert up.parse_version("not-a-version") is None
    assert up.is_newer("3.4.0", "3.3.1")
    assert not up.is_newer("3.3.1", "3.3.1")
    assert not up.is_newer("3.3.0", "3.3.1")
    assert up.is_prerelease("3.4.0rc1")
    assert not up.is_prerelease("3.4.0")


# ── install shape and assets ─────────────────────────────────────────────

def test_asset_names_cover_every_release_platform():
    assert up.asset_name("darwin", "arm64") == "neuralosd-macos-arm64"
    assert up.asset_name("darwin", "x86_64") == "neuralosd-macos-x64"
    assert up.asset_name("linux", "x86_64") == "neuralosd-linux-x64"
    assert up.asset_name("win32", "AMD64") == "neuralosd-windows-x64.exe"
    assert (up.asset_name("linux", "aarch64", "msb")
            == "neuralosd-msb-linux-arm64")
    assert (up.asset_name("linux", "arm64", "boxlite")
            == "neuralosd-boxlite-linux-arm64")
    with pytest.raises(up.UpdateError):
        up.asset_name("freebsd", "mips")


def test_variant_is_read_from_the_executable_name():
    assert up.variant_from_path("/usr/local/bin/neuralosd") == "base"
    assert up.variant_from_path("~/bin/neuralosd-msb") == "msb"
    assert up.variant_from_path("neuralosd-boxlite-macos-arm64") == "boxlite"


# ── latest versions ──────────────────────────────────────────────────────

def test_latest_pypi_reads_info_version():
    def fetch(url, timeout=15):
        assert "pypi.org" in url
        return {"info": {"version": "9.9.9"}}

    assert up.latest_pypi(fetch=fetch) == "9.9.9"


def test_latest_pypi_pre_channel_considers_all_releases():
    def fetch(url, timeout=15):
        return {"info": {"version": "3.3.1"},
                "releases": {"3.3.1": [], "3.4.0rc1": [], "bad": []}}

    assert up.latest_pypi(fetch=fetch) == "3.3.1"
    assert up.latest_pypi(fetch=fetch, channel="pre") == "3.4.0rc1"


def test_latest_pypi_rejects_garbage():
    def fetch(url, timeout=15):
        return {"info": {}}

    with pytest.raises(up.UpdateError):
        up.latest_pypi(fetch=fetch)


def test_latest_release_stable_skips_drafts_and_prereleases():
    def fetch(url, timeout=15):
        return [
            {"tag_name": "v9.9.9", "draft": True},
            {"tag_name": "v9.9.8rc1", "prerelease": True},
            {"tag_name": "v9.9.7", "prerelease": False},
        ]

    assert up.latest_release(fetch=fetch, channel="pre") == "9.9.8rc1"

    def stable_fetch(url, timeout=15):
        return {"tag_name": "v9.9.7", "prerelease": False}

    assert up.latest_release(fetch=stable_fetch) == "9.9.7"


def test_check_reports_plan_and_update_available(monkeypatch):
    monkeypatch.setattr(up, "update_command",
                        lambda latest: ["pip", "install", latest])
    info = up.check(kind="pip", fetch=lambda url, timeout=15:
                    {"info": {"version": "99.0.0"}})
    assert info["update_available"] is True
    assert info["current"] == up.__version__
    assert info["latest"] == "99.0.0"
    assert "99.0.0" in info["plan"]


def test_check_only_never_mutates(monkeypatch):
    monkeypatch.setattr(up, "update_command", lambda latest: ["pip"])
    monkeypatch.setattr(up, "update_pip",
                        lambda *a, **k: pytest.fail("must not update"))
    out = up.run(check_only=True, fetch=lambda url, timeout=15:
                 {"info": {"version": "99.0.0"}}, kind="pip")
    assert out["action"] == "none"
    assert out["update_available"] is True


# ── pip shape ────────────────────────────────────────────────────────────

def test_pip_update_runs_the_running_interpreter(monkeypatch):
    class R:
        returncode = 0
        stdout = "Successfully installed neuralosd-9.9.9"
        stderr = ""

    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return R()

    monkeypatch.setattr(up, "is_editable", lambda: False)
    out = up.update_pip("9.9.9", command=["py", "-m", "pip"], run=run)
    assert out["action"] == "updated"
    assert seen["cmd"] == ["py", "-m", "pip"]


def test_pip_update_refuses_an_editable_checkout(monkeypatch):
    monkeypatch.setattr(up, "is_editable", lambda: True)
    with pytest.raises(up.UpdateError, match="editable"):
        up.update_pip("9.9.9")


def test_pip_update_surfaces_a_failed_command(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "no matching distribution"

    monkeypatch.setattr(up, "is_editable", lambda: False)
    with pytest.raises(up.UpdateError, match="no matching distribution"):
        up.update_pip("9.9.9", command=["pip"], run=lambda *a, **k: R())


# ── standalone binary ────────────────────────────────────────────────────

class _Resp:
    status = 200

    def __init__(self, data):
        self._bio = io.BytesIO(data)

    def read(self, n=-1):
        return self._bio.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release_for(payload, name="neuralosd-test", digest=None):
    digest = digest or "sha256:" + hashlib.sha256(payload).hexdigest()
    return {"tag_name": "v99.0.0",
            "assets": [{"name": name, "digest": digest, "size": len(payload),
                        "browser_download_url":
                            "https://example.invalid/bin"}]}


def _exe(tmp_path):
    exe = tmp_path / "neuralosd"
    exe.write_bytes(b"old-binary")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def test_binary_update_verifies_then_swaps(monkeypatch, tmp_path):
    payload = b"new-binary-payload"
    exe = _exe(tmp_path)
    monkeypatch.setattr(up, "asset_name", lambda **kw: "neuralosd-test")
    monkeypatch.setattr(up, "_release_for",
                        lambda version, fetch=None, timeout=15: _release_for(payload))
    monkeypatch.setattr(up, "download_verified",
                        lambda url, dest, digest, opener, timeout: (
                            pathlib.Path(dest).write_bytes(payload)
                            and len(payload)))
    out = up.update_binary("99.0.0", exe=str(exe))
    assert out["action"] in ("updated", "deferred")
    if out["action"] == "updated":
        assert exe.read_bytes() == payload
        assert (tmp_path / "neuralosd.bak").read_bytes() == b"old-binary"
        assert not (tmp_path / "neuralosd.update.lock").exists()


def test_binary_update_refuses_a_bad_digest(monkeypatch, tmp_path):
    payload = b"new-binary-payload"
    exe = _exe(tmp_path)
    bad = "sha256:" + "0" * 64
    monkeypatch.setattr(up, "asset_name", lambda **kw: "neuralosd-test")
    monkeypatch.setattr(up, "_release_for",
                        lambda version, fetch=None, timeout=15: _release_for(
                            payload, digest=bad))
    with pytest.raises(up.UpdateError, match="sha256 mismatch"):
        up.update_binary(
            "99.0.0", exe=str(exe),
            opener=lambda req, timeout: _Resp(payload))
    assert exe.read_bytes() == b"old-binary"


def test_download_without_a_digest_is_refused(tmp_path):
    with pytest.raises(up.UpdateError, match="no sha256 digest"):
        up.download_verified("https://example.invalid/bin",
                             str(tmp_path / "x"), None)


def test_a_live_update_lock_is_respected(monkeypatch, tmp_path):
    exe = _exe(tmp_path)
    lock = tmp_path / "neuralosd.update.lock"
    lock.write_text("1", encoding="utf-8")
    monkeypatch.setattr(up, "asset_name", lambda **kw: "neuralosd-test")
    monkeypatch.setattr(up, "_release_for",
                        lambda version, fetch=None, timeout=15: _release_for(b"x"))
    with pytest.raises(up.UpdateError, match="already running"):
        up.update_binary("99.0.0", exe=str(exe))


def test_a_stale_lock_is_replaced(monkeypatch, tmp_path):
    payload = b"new"
    exe = _exe(tmp_path)
    lock = tmp_path / "neuralosd.update.lock"
    lock.write_text("1", encoding="utf-8")
    old = 1_000_000
    os.utime(lock, (old, old))
    monkeypatch.setattr(up, "asset_name", lambda **kw: "neuralosd-test")
    monkeypatch.setattr(up, "_release_for",
                        lambda version, fetch=None, timeout=15: _release_for(payload))
    monkeypatch.setattr(up, "download_verified",
                        lambda url, dest, digest, opener, timeout: (
                            pathlib.Path(dest).write_bytes(payload)
                            and len(payload)))
    out = up.update_binary("99.0.0", exe=str(exe))
    assert out["action"] == "updated"
    assert exe.read_bytes() == payload
    assert not lock.exists()


def test_run_pip_marks_restart_required(monkeypatch):
    monkeypatch.setattr(up, "update_command", lambda latest: ["pip"])
    monkeypatch.setattr(up, "is_editable", lambda: False)
    monkeypatch.setattr(up, "update_pip",
                        lambda version, **k: {"action": "updated"})
    out = up.run(fetch=lambda url, timeout=15:
                 {"info": {"version": "99.0.0"}}, kind="pip")
    assert out["action"] == "updated"
    assert out["restart_required"] is True


def test_run_declined_does_nothing(monkeypatch):
    monkeypatch.setattr(up, "update_command", lambda latest: ["pip"])
    out = up.run(fetch=lambda url, timeout=15:
                 {"info": {"version": "99.0.0"}}, kind="pip",
                 confirm=lambda info: False)
    assert out["action"] == "declined"


def test_cli_update_check_json(monkeypatch, capsys):
    from neuralosd._cmd import update as cmd

    class A:
        check = True
        yes = False
        force = False
        channel = "stable"
        json = True
        timeout = 5

    monkeypatch.setattr(up, "check",
                        lambda **kw: {"current": "3.3.1", "latest": "3.3.1",
                                      "install": "pip", "channel": "stable",
                                      "update_available": False,
                                      "plan": None})
    assert cmd.run(A()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["latest"] == "3.3.1"
