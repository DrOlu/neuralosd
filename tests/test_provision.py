"""Unit tests for uv-based sidecar provisioning.

Hermetic: ``uv`` is replaced by a stub that records its argv and creates the
layout a real ``uv venv`` would, so nothing touches the network and the
command sequence can be asserted exactly.
"""
import json
import os
import sys
import textwrap

import pytest

from neuralosd import provision as prov

FAKE_UV_IMPL = textwrap.dedent('''
    import json, os, pathlib, sys

    argv = sys.argv[1:]
    log = os.environ.get("FAKE_UV_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\\n")

    marker = os.environ.get("FAKE_UV_FAIL_IF")
    if marker and marker in " ".join(argv):
        sys.stderr.write("fake uv: refusing\\n")
        sys.exit(3)

    if argv and argv[0] == "venv":
        target = pathlib.Path(argv[-1])
        for sub, name in (("bin", "neuralosd-sidecar"),
                          ("Scripts", "neuralosd-sidecar.exe")):
            d = target / sub
            d.mkdir(parents=True, exist_ok=True)
            p = d / name
            p.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
            try:
                p.chmod(0o755)
            except OSError:
                pass
    sys.exit(0)
''')


def _fake_uv(tmp_path, name="uv"):
    """Create an executable stub `uv` that logs argv, cross-platform."""
    impl = tmp_path / "fake_uv_impl.py"
    impl.write_text(FAKE_UV_IMPL, encoding="utf-8")
    if os.name == "nt":
        stub = tmp_path / f"{name}.cmd"
        stub.write_text(f'@echo off\r\n"{sys.executable}" "{impl}" %*\r\n',
                        encoding="utf-8")
    else:
        stub = tmp_path / name
        stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n',
                        encoding="utf-8")
        stub.chmod(0o755)
    return str(stub)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated NEURALOSD_HOME + a stub uv, with argv logging."""
    home = tmp_path / "home"
    monkeypatch.setenv("NEURALOSD_HOME", str(home))
    log = tmp_path / "uv.log"
    monkeypatch.setenv("FAKE_UV_LOG", str(log))
    uv = _fake_uv(tmp_path)
    monkeypatch.setenv("NEURALOSD_UV", uv)
    monkeypatch.delenv("NEURALOSD_SIDECAR", raising=False)

    def calls():
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]

    return {"home": home, "uv": uv, "log": log, "calls": calls}


# ── locations ──────────────────────────────────────────────────────────────

def test_home_default(monkeypatch):
    monkeypatch.delenv("NEURALOSD_HOME", raising=False)
    assert prov.sidecar_home().endswith(".neuralosd")


def test_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NEURALOSD_HOME", str(tmp_path / "custom"))
    assert prov.sidecar_home() == str(tmp_path / "custom")


def test_sidecar_dir_is_under_home(env):
    assert prov.sidecar_dir() == str(env["home"] / "sidecar")


def test_executable_path_is_platform_correct(env):
    p = prov.sidecar_executable()
    if os.name == "nt":
        assert p.endswith(os.path.join("Scripts", "neuralosd-sidecar.exe"))
    else:
        assert p.endswith(os.path.join("bin", "neuralosd-sidecar"))
    assert p.startswith(prov.sidecar_dir())


def test_venv_python_path_is_platform_correct(env):
    p = prov.venv_python()
    assert p.startswith(prov.sidecar_dir())
    assert p.endswith("python.exe" if os.name == "nt" else "python")


# ── uv discovery ───────────────────────────────────────────────────────────

def test_find_uv_env_override_wins(monkeypatch):
    monkeypatch.setenv("NEURALOSD_UV", "/custom/uv")
    monkeypatch.setattr(prov.shutil, "which", lambda n: "/path/uv")
    assert prov.find_uv() == "/custom/uv"


def test_find_uv_from_path(monkeypatch):
    monkeypatch.delenv("NEURALOSD_UV", raising=False)
    monkeypatch.setattr(prov.shutil, "which",
                        lambda n: "/usr/local/bin/uv" if n == "uv" else None)
    assert prov.find_uv() == "/usr/local/bin/uv"


def test_find_uv_common_locations(monkeypatch, tmp_path):
    monkeypatch.delenv("NEURALOSD_UV", raising=False)
    monkeypatch.setattr(prov.shutil, "which", lambda n: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(prov.os.path, "expanduser",
                        lambda p: str(tmp_path) if p == "~" else p)
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "uv").write_text("#!/bin/sh\n", encoding="utf-8")
    assert prov.find_uv() == str(local / "uv")


def test_find_uv_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("NEURALOSD_UV", raising=False)
    monkeypatch.setattr(prov.shutil, "which", lambda n: None)
    monkeypatch.setattr(prov.os.path, "expanduser",
                        lambda p: str(tmp_path) if p == "~" else p)
    assert prov.find_uv() is None


# ── status / requirements ──────────────────────────────────────────────────

def test_status_reports_not_provisioned(env):
    st = prov.status()
    assert st["provisioned"] is False
    assert st["uv_found"] is True
    assert st["uv"] == env["uv"]


def test_requirements_without_version():
    assert prov.requirements() == ["neuralosd"]


def test_requirements_pins_version():
    assert prov.requirements(version="1.2.3") == ["neuralosd==1.2.3"]


def test_requirements_includes_extras_and_skips_blanks():
    got = prov.requirements(["pypdf", "", "  ", "pywinrm"], version="1.0")
    assert got == ["neuralosd==1.0", "pypdf", "pywinrm"]


# ── provisioning ───────────────────────────────────────────────────────────

def test_provision_creates_the_environment(env):
    st = prov.provision(version="9.9.9")
    assert st["action"] == "created"
    assert prov.is_provisioned()
    assert os.path.isfile(prov.sidecar_executable())


def test_provision_issues_exactly_the_expected_commands(env):
    prov.provision(python="3.12", packages=["pypdf"], version="9.9.9")
    cmds = env["calls"]()
    assert cmds[0] == ["python", "install", "3.12"]
    assert cmds[1][0] == "venv" and cmds[1][1] == "--python" \
        and cmds[1][2] == "3.12" and cmds[1][3] == prov.sidecar_dir()
    assert cmds[2][:5] == ["pip", "install", "--python", prov.venv_python(),
                           "neuralosd==9.9.9"]
    assert cmds[2][5] == "pypdf"
    assert len(cmds) == 3


def test_provision_is_idempotent(env):
    prov.provision(version="1.0")
    before = len(env["calls"]())
    st = prov.provision(version="1.0")
    assert st["action"] == "already"
    assert len(env["calls"]()) == before      # uv was not invoked again


def test_provision_force_rebuilds(env):
    prov.provision(version="1.0")
    st = prov.provision(version="1.0", force=True)
    assert st["action"] == "recreated"
    assert st["action_detail"].startswith("recreated")
    assert prov.is_provisioned()


def test_provision_without_uv_gives_an_install_hint(env, monkeypatch):
    monkeypatch.setenv("NEURALOSD_UV", "")
    monkeypatch.delenv("NEURALOSD_UV", raising=False)
    monkeypatch.setattr(prov, "find_uv", lambda: None)
    with pytest.raises(prov.ProvisionError) as e:
        prov.provision()
    msg = str(e.value)
    assert "uv is required" in msg
    assert "astral.sh" in msg
    assert not os.path.isdir(prov.sidecar_dir())   # nothing half-built


def test_provision_surfaces_uv_failure(env, monkeypatch):
    monkeypatch.setenv("FAKE_UV_FAIL_IF", "venv")
    with pytest.raises(prov.ProvisionError) as e:
        prov.provision()
    assert "failed" in str(e.value)
    assert "refusing" in str(e.value)


def test_provision_detects_a_useless_environment(env, monkeypatch):
    """If uv succeeds but no sidecar appears, that is an error, not success."""
    def liar(cmd, log=None):
        class R:
            returncode = 0
            stdout = stderr = ""
        return R()
    monkeypatch.setattr(prov, "_run", liar)
    with pytest.raises(prov.ProvisionError, match="missing"):
        prov.provision()


def test_provision_python_install_failure_is_not_fatal(env, monkeypatch):
    """`uv venv --python X` can fetch its own interpreter, so a failed
    `uv python install` must not abort provisioning."""
    monkeypatch.setenv("FAKE_UV_FAIL_IF", "python install")
    st = prov.provision(version="1.0")
    assert st["action"] == "created"
    assert prov.is_provisioned()


# ── discovery integration ──────────────────────────────────────────────────

def test_discovery_finds_a_provisioned_environment(env, monkeypatch):
    """With nothing on PATH, discovery must fall through to what we
    provisioned. (In CI the venv's own neuralosd-sidecar IS on PATH, which is
    why this test hides it explicitly.)"""
    from neuralosd import sidecar_client as scc
    prov.provision(version="1.0")
    monkeypatch.setattr(scc.shutil, "which", lambda n: None)
    assert scc.sidecar_command() == [prov.sidecar_executable()]


def test_path_beats_the_provisioned_copy(env, monkeypatch, tmp_path):
    """Explicit user intent (PATH) must win over what we provisioned."""
    from neuralosd.sidecar_client import sidecar_command
    prov.provision(version="1.0")
    on_path = tmp_path / "bin" / "neuralosd-sidecar"
    on_path.parent.mkdir(parents=True, exist_ok=True)
    on_path.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("shutil.which",
                        lambda n: str(on_path) if n == "neuralosd-sidecar" else None)
    assert sidecar_command() == [str(on_path)]


def test_env_off_disables_even_when_provisioned(env, monkeypatch):
    from neuralosd.sidecar_client import sidecar_command
    prov.provision(version="1.0")
    monkeypatch.setenv("NEURALOSD_SIDECAR", "off")
    assert sidecar_command() is None


def test_discovery_never_raises_when_provision_is_broken(env, monkeypatch):
    """Discovery runs on every load — a broken provision module must not
    take the whole CLI down."""
    from neuralosd.sidecar_client import sidecar_command
    monkeypatch.setattr(prov, "is_provisioned",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(prov.shutil, "which",
                        lambda n: None if n == "neuralosd-sidecar" else "/usr/bin/python3")
    assert sidecar_command() is not None   # falls through, no exception


# ── CLI ────────────────────────────────────────────────────────────────────

def test_cli_status_when_not_provisioned(env, capsys):
    rc = prov.main(["--status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "provisioned : no" in out
    assert "neuralosd sidecar --setup" in out


def test_cli_setup_creates_and_reports(env, capsys):
    rc = prov.main(["--setup", "--with", "pypdf,pywinrm"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "created" in out
    assert prov.is_provisioned()
    joined = " ".join(" ".join(c) for c in env["calls"]())
    assert "pypdf" in joined and "pywinrm" in joined


def test_cli_setup_reports_uv_failure(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_UV_FAIL_IF", "venv")
    rc = prov.main(["--setup"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
