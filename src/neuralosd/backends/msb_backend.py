"""MSB backend — lifecycle management for Microsandbox sandboxes."""
import subprocess
from typing import Any, Dict


class MSBBackend:
    """Manages Microsandbox lifecycle via the msb CLI."""

    def create(self, name: str, image: str = "python:3.12-slim",
               cpus: int = 1, memory: str = "1G", **kwargs):
        r = subprocess.run(
            ["msb", "create", "--name", name, "-c", str(cpus),
             "-m", memory, image],
            capture_output=True, text=True, timeout=120)
        return {"exit": r.returncode, "stdout": r.stdout[-500:]}

    def start(self, name: str):
        r = subprocess.run(["msb", "start", name],
                           capture_output=True, text=True, timeout=120)
        return r.returncode

    def stop(self, name: str):
        r = subprocess.run(["msb", "stop", name],
                           capture_output=True, text=True, timeout=120)
        return r.returncode

    def remove(self, name: str):
        subprocess.run(["msb", "stop", name], capture_output=True)
        r = subprocess.run(["msb", "remove", name],
                           capture_output=True, text=True, timeout=120)
        return r.returncode

    def exec(self, name: str, cmd: str, args: list = None):
        full = [cmd] + (args or [])
        r = subprocess.run(["msb", "exec", name, "--"] + full,
                           capture_output=True, text=True, timeout=300)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit": r.returncode}

    def fork(self, template: str, name: str):
        r = subprocess.run(["msb", "snapshot", "restore", template,
                            "--name", name, "--forked"],
                           capture_output=True, text=True, timeout=300)
        return r.returncode

    def snapshot(self, name: str, dest: str):
        r = subprocess.run(["msb", "snapshot", "create", "--sandbox", name,
                            "--full", "-o", dest],
                           capture_output=True, text=True, timeout=600)
        return r.returncode

    @staticmethod
    def available() -> bool:
        import shutil
        return shutil.which("msb") is not None

    @staticmethod
    def name() -> str:
        return "msb"
