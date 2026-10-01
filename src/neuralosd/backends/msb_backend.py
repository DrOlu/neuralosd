"""MSB backend — lifecycle management for Microsandbox sandboxes.

The Microsandbox Python SDK ships the `msb` CLI inside the package at
`microsandbox/_bundled/bin/msb` (and `msb.exe` on Windows). This backend
finds that binary whether it is on PATH (pip/venv install) or bundled inside
a frozen standalone binary (Nuitka extraction dir).
"""
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional


def _candidate_paths() -> List[str]:
    """Every place the `msb` binary might live, in priority order."""
    names = ["msb.exe", "msb"] if os.name == "nt" else ["msb"]
    cands: List[str] = []

    # 1. PATH (normal pip/venv install puts it here)
    for n in names:
        p = shutil.which(n)
        if p:
            cands.append(p)

    # 2. Next to the running interpreter (some activations)
    for n in names:
        cands.append(os.path.join(os.path.dirname(sys.executable), n))

    # 3. Bundled inside the microsandbox package (frozen binary + wheels)
    try:
        import microsandbox  # type: ignore
        pkg_dir = os.path.dirname(os.path.abspath(microsandbox.__file__))
        for n in names:
            cands.append(os.path.join(pkg_dir, "_bundled", "bin", n))
    except Exception:  # noqa: BLE001
        pass

    return cands


def msb_binary() -> Optional[str]:
    """Return an executable path to `msb`, or None if it cannot be found."""
    for cand in _candidate_paths():
        if cand and os.path.isfile(cand):
            # A frozen build may extract files without the exec bit set.
            if os.name != "nt" and not os.access(cand, os.X_OK):
                try:
                    os.chmod(cand, 0o755)
                except OSError:
                    pass
            return cand
    return None


class MSBBackend:
    """Manages Microsandbox lifecycle via the msb CLI."""

    def __init__(self, msb_path: Optional[str] = None):
        self._msb = msb_path or msb_binary()

    def _bin(self) -> str:
        if not self._msb:
            raise RuntimeError(
                "the msb CLI was not found — install it with "
                "'pip install microsandbox' or 'pip install neuralosd[msb]'")
        return self._msb

    def _run(self, args: List[str], timeout: int = 120):
        return subprocess.run([self._bin(), *args],
                              capture_output=True, text=True, timeout=timeout)

    def create(self, name: str, image: str = "python:3.12-slim",
               cpus: int = 1, memory: str = "1G", **kwargs):
        r = self._run(["create", "--name", name, "-c", str(cpus),
                       "-m", memory, image])
        return {"exit": r.returncode, "stdout": r.stdout[-500:]}

    def start(self, name: str):
        return self._run(["start", name]).returncode

    def stop(self, name: str):
        return self._run(["stop", name]).returncode

    def remove(self, name: str):
        self._run(["stop", name])
        return self._run(["remove", name]).returncode

    def exec(self, name: str, cmd: str, args: list = None):
        full = [cmd] + (args or [])
        r = self._run(["exec", name, "--"] + full, timeout=300)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit": r.returncode}

    def fork(self, template: str, name: str):
        return self._run(["snapshot", "restore", template,
                          "--name", name, "--forked"], timeout=300).returncode

    def snapshot(self, name: str, dest: str):
        return self._run(["snapshot", "create", "--sandbox", name,
                          "--full", "-o", dest], timeout=600).returncode

    @staticmethod
    def available() -> bool:
        return msb_binary() is not None

    @staticmethod
    def name() -> str:
        return "msb"
