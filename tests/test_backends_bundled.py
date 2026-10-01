"""Unit tests for backend binary discovery (msb bundling in frozen builds)."""
import os
import sys
import types

import pytest

from neuralosd.backends import msb_backend as mb
from neuralosd.backends import available_backends, get_backend


def _fake_msb(path, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho msb\n", encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return path


def _isolate(monkeypatch, tmp_path):
    """Make PATH, the interpreter dir, and the real microsandbox invisible,
    so only an explicitly-provided bundled path can decide.

    Setting sys.modules['microsandbox'] = None makes `import microsandbox`
    raise ImportError even when the package is genuinely installed (which it
    is in CI, where we build the msb variant), keeping these tests
    environment-independent.
    """
    monkeypatch.setattr(mb.shutil, "which", lambda n: None)
    monkeypatch.setattr(mb.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setitem(sys.modules, "microsandbox", None)


def test_msb_binary_prefers_path(monkeypatch, tmp_path):
    fake = _fake_msb(tmp_path / "bin" / "msb", executable=True)
    monkeypatch.setattr(mb.shutil, "which",
                        lambda n: str(fake) if n in ("msb", "msb.exe") else None)
    assert mb.msb_binary() == str(fake)


def test_msb_binary_falls_back_to_bundled_package(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    pkg = tmp_path / "microsandbox"
    b = _fake_msb(pkg / "_bundled" / "bin" / "msb", executable=False)
    mod = types.ModuleType("microsandbox")
    mod.__file__ = str(pkg / "__init__.py")
    monkeypatch.setitem(sys.modules, "microsandbox", mod)

    got = mb.msb_binary()
    assert got == str(b)
    # the exec bit must be restored for a frozen extraction
    assert os.access(got, os.X_OK)


def test_msb_binary_none_when_absent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert mb.msb_binary() is None


def test_msb_backend_available_reflects_binary(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert mb.MSBBackend.available() is False

    pkg = tmp_path / "microsandbox"
    _fake_msb(pkg / "_bundled" / "bin" / "msb")
    mod = types.ModuleType("microsandbox")
    mod.__file__ = str(pkg / "__init__.py")
    monkeypatch.setitem(sys.modules, "microsandbox", mod)
    assert mb.MSBBackend.available() is True


def test_msb_backend_bin_raises_clear_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    backend = mb.MSBBackend(msb_path=None)
    with pytest.raises(RuntimeError) as exc:
        backend._bin()
    msg = str(exc.value)
    assert "msb CLI was not found" in msg
    assert "neuralosd[msb]" in msg


def test_msb_backend_uses_explicit_path(tmp_path):
    fake = _fake_msb(tmp_path / "msb", executable=True)
    backend = mb.MSBBackend(msb_path=str(fake))
    assert backend._bin() == str(fake)


def test_available_backends_shape():
    av = available_backends()
    assert set(av) == {"boxlite", "msb"}
    assert all(isinstance(v, bool) for v in av.values())


def test_get_backend_msb_is_lazy(tmp_path):
    """get_backend('msb') must construct even when msb is absent."""
    backend = get_backend("msb")
    assert backend.name() == "msb"


def test_get_backend_unknown():
    with pytest.raises(ValueError):
        get_backend("nope")
