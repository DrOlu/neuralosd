"""Sandbox adapters — route probe execution to BoxLite / MSB sandboxes.

An adapter implements the same interface as an in-process probe: takes
kwargs, returns a result dict. The instance's tier declaration routes
execution to the right substrate.
"""
import json
import urllib.request
from typing import Any, Dict


class BoxLiteAdapter:
    """Executes probes inside a BoxLite box via `boxlite serve` REST."""

    def __init__(self, serve_url: str = "http://localhost:8100",
                 api_key: str = None, box_name: str = "chinook-1gb"):
        self.serve_url = serve_url.rstrip("/")
        self.api_key = api_key
        self.box_name = box_name

    def exec_probe(self, code: str) -> Dict:
        """Run a Python snippet inside the sandbox."""
        req = urllib.request.Request(
            f"{self.serve_url}/v1/boxes/{self.box_name}/exec",
            data=json.dumps({"command": code}).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"}
                        if self.api_key else {})})
        r = urllib.request.urlopen(req, timeout=120)
        return json.loads(r.read())


class MSBAdapter:
    """Executes probes inside a Microsandbox via `msb exec`."""

    def __init__(self, sandbox_name: str = "chinook-ms"):
        self.sandbox_name = sandbox_name

    def exec_probe(self, code: str) -> Dict:
        import subprocess
        r = subprocess.run(
            ["msb", "exec", self.sandbox_name, "--", "python3", "-c", code],
            capture_output=True, text=True, timeout=120)
        return {"stdout": r.stdout, "stderr": r.stderr,
                "exit_code": r.returncode}


def get_adapter(tier: str, **kwargs) -> Any:
    """Factory: tier string → adapter instance, or None for in-process."""
    if tier.startswith("sandbox:boxlite:"):
        return BoxLiteAdapter(box_name=tier.split(":")[-1], **kwargs)
    if tier.startswith("sandbox:msb:"):
        return MSBAdapter(sandbox_name=tier.split(":")[-1], **kwargs)
    return None   # in-process
