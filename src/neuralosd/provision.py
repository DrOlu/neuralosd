"""Provision a sidecar environment with uv.

The sidecar (see :mod:`neuralosd.sidecar`) needs a host Python with
``neuralosd`` and whatever libraries your probes use.  Doing that by hand is
fine, but a machine may have no usable Python at all — that was the sidecar's
one real limitation.

uv solves it: it ships self-contained CPython builds, so it can create a
dedicated environment out of nothing:

    uv python install 3.12                     (implicitly, if needed)
    uv venv --python 3.12 ~/.neuralosd/sidecar
    uv pip install --python …/python neuralosd pypdf

After that the frozen binary finds ``~/.neuralosd/sidecar/bin/neuralosd-sidecar``
automatically — no environment variables required.

Provisioning is **explicit only** (``neuralosd sidecar --setup``): it touches
the network and can download an interpreter, so it must never happen silently
in the middle of answering a question.
"""
import os
import shutil
import subprocess
import sys
from typing import Iterable, Optional

DEFAULT_PYTHON = "3.12"


class ProvisionError(RuntimeError):
    """Provisioning could not be completed."""


# ── locations ──────────────────────────────────────────────────────────────

def sidecar_home() -> str:
    """Base directory for provisioned state (``$NEURALOSD_HOME`` overrides)."""
    env = os.environ.get("NEURALOSD_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".neuralosd")


def sidecar_dir() -> str:
    """Root of the provisioned virtual environment."""
    return os.path.join(sidecar_home(), "sidecar")


def _scripts_dir() -> str:
    return "Scripts" if os.name == "nt" else "bin"


def sidecar_executable() -> str:
    """Where the provisioned ``neuralosd-sidecar`` command lives."""
    name = "neuralosd-sidecar.exe" if os.name == "nt" else "neuralosd-sidecar"
    return os.path.join(sidecar_dir(), _scripts_dir(), name)


def venv_python() -> str:
    """The interpreter inside the provisioned environment."""
    name = "python.exe" if os.name == "nt" else "python"
    return os.path.join(sidecar_dir(), _scripts_dir(), name)


def is_provisioned() -> bool:
    return os.path.isfile(sidecar_executable())


# ── uv discovery ───────────────────────────────────────────────────────────

def find_uv() -> Optional[str]:
    """Locate ``uv``: ``$NEURALOSD_UV``, then PATH, then common install dirs."""
    override = os.environ.get("NEURALOSD_UV")
    if override:
        return override
    found = shutil.which("uv")
    if found:
        return found
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, ".local", "bin", "uv"),
                 os.path.join(home, ".cargo", "bin", "uv"),
                 os.path.join(home, ".local", "share", "uv", "uv")):
        if os.path.isfile(cand):
            return cand
    return None


UV_INSTALL_HINT = (
    "uv is required to provision a sidecar and was not found.\n"
    "  install it:  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
    "  (Windows):   powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"\n"
    "  or point at it directly:  export NEURALOSD_UV=/path/to/uv")


# ── status / provisioning ──────────────────────────────────────────────────

def status() -> dict:
    """Everything the CLI needs to report, without touching the network."""
    uv = find_uv()
    return {"uv": uv,
            "uv_found": bool(uv),
            "home": sidecar_home(),
            "dir": sidecar_dir(),
            "executable": sidecar_executable(),
            "python": DEFAULT_PYTHON,
            "provisioned": is_provisioned()}


def _run(cmd, log=None) -> subprocess.CompletedProcess:
    if log:
        log("  $ " + " ".join(str(c) for c in cmd))
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    except OSError as e:
        raise ProvisionError(f"could not run {cmd[0]!r}: {e}")
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[-900:]
        raise ProvisionError(
            f"{os.path.basename(str(cmd[0]))} {cmd[1] if len(cmd) > 1 else ''} "
            f"failed (exit {r.returncode}):\n{detail}")
    return r


def requirements(packages: Iterable[str] = (), version: Optional[str] = None):
    """The pip requirement list for the sidecar env."""
    spec = "neuralosd"
    if version:
        spec = f"neuralosd=={version}"
    out = [spec]
    for p in packages or ():
        p = str(p).strip()
        if p:
            out.append(p)
    return out


def provision(python: str = DEFAULT_PYTHON, packages: Iterable[str] = (),
              force: bool = False, uv: Optional[str] = None,
              version: Optional[str] = None, log=None) -> dict:
    """Create the sidecar environment. Idempotent unless ``force``.

    Returns the :func:`status` dict with an extra ``"action"`` key
    (``"created"``, ``"recreated"`` or ``"already"``).
    """
    uv = uv or find_uv()
    if not uv:
        raise ProvisionError(UV_INSTALL_HINT)

    target = sidecar_dir()

    if is_provisioned() and not force:
        st = status()
        st["action"] = "already"
        st["action_detail"] = f"already provisioned at {target}"
        return st

    action = "recreated" if os.path.isdir(target) else "created"
    if os.path.isdir(target):
        try:
            shutil.rmtree(target)
        except OSError as e:
            raise ProvisionError(f"could not remove existing {target}: {e}")

    os.makedirs(os.path.dirname(target), exist_ok=True)

    # 1. Make sure the interpreter exists. Non-fatal on its own: `uv venv
    #    --python X` will also fetch a managed Python if needed.
    try:
        _run([uv, "python", "install", python], log=log)
    except ProvisionError as e:
        if log:
            log(f"  (uv python install did not succeed, continuing: {e})")

    # 2. The environment itself.
    _run([uv, "venv", "--python", python, target], log=log)

    # 3. neuralosd (pinned to this build's version by default) + user extras.
    reqs = requirements(packages, version)
    _run([uv, "pip", "install", "--python", venv_python(), *reqs], log=log)

    if not is_provisioned():
        raise ProvisionError(
            f"provisioning finished but {sidecar_executable()} is missing — "
            "the environment is unusable")

    st = status()
    st["action"] = action
    st["action_detail"] = f"{action} {target}"
    st["packages"] = reqs
    return st


def main(argv=None) -> int:
    """``neuralosd-sidecar-setup`` style entry point (used by the CLI)."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="neuralosd sidecar",
        description="Provision a sidecar environment with uv.")
    ap.add_argument("--status", action="store_true", help="report and exit")
    ap.add_argument("--setup", action="store_true", help="create the environment")
    ap.add_argument("--with", dest="packages", default="",
                    help="extra packages, comma separated (e.g. pypdf,pywinrm)")
    ap.add_argument("--python", default=DEFAULT_PYTHON)
    ap.add_argument("--force", action="store_true", help="rebuild from scratch")
    a = ap.parse_args(argv)

    st = status()
    print("sidecar status:")
    print(f"  uv          : {st['uv'] or 'not found'}")
    print(f"  home        : {st['home']}")
    print(f"  environment : {st['dir']}")
    print(f"  executable  : {st['executable']}")
    print(f"  provisioned : {'yes' if st['provisioned'] else 'no'}")

    if not a.setup:
        if not st["provisioned"]:
            print("\ncreate it with:  neuralosd sidecar --setup --with pypdf")
        return 0

    pkgs = [p for p in (a.packages or "").split(",") if p.strip()]
    try:
        from . import __version__ as _v
    except Exception:  # noqa: BLE001
        _v = None
    try:
        out = provision(python=a.python, packages=pkgs, force=a.force,
                        version=_v, log=lambda m: print(m))
    except ProvisionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"\n{out['action']}: {out['action_detail']}")
    print(f"  installed: {', '.join(out.get('packages', []))}")
    print("  the binary will now find it automatically "
          "(no environment variables needed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
