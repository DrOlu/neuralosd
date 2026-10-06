"""BoxLite backend — lifecycle management for BoxLite microVM boxes.

The `boxlite` import is MODULE-LEVEL (guarded): `create()` touches
`boxlite.BoxOptions` directly, and with the import trapped inside `_rt()` it
raised NameError on EVERY deploy regardless of whether boxlite was installed —
the import was bound only in `_rt()`'s locals.

The guarded import (rather than a bare one) keeps graceful degradation: an
environment without boxlite must still import this module so `available_backends()`
can say "not installed" instead of crashing.
"""
import asyncio
import os
import shlex
from typing import Any, Dict, List, Optional

try:
    import boxlite
except ImportError:                                   # pragma: no cover
    boxlite = None


class BoxLiteBackend:
    """Manages BoxLite box lifecycle for neuralOS instances."""

    def __init__(self, serve_url: str = None, api_key: str = None):
        self.serve_url = serve_url
        self.api_key = api_key

    def _rt(self):
        if boxlite is None:
            raise RuntimeError(
                "boxlite is not installed — pip install 'neuralosd[boxlite]'")
        if self.serve_url:
            cred = boxlite.ApiKeyCredential(self.api_key) if self.api_key else None
            return boxlite.Boxlite.rest(
                boxlite.BoxliteRestOptions(url=self.serve_url, credential=cred))
        return boxlite.Boxlite.default()

    def _require(self):
        if boxlite is None:
            raise RuntimeError(
                "boxlite is not installed — pip install 'neuralosd[boxlite]'")

    async def create(self, name: str, image: str = "python:3.12-slim",
                     cpus: int = 1, memory_mib: int = 1024,
                     disk_gb: int = 6, **kwargs):
        self._require()
        rt = self._rt()
        got = await rt.get_or_create(
            boxlite.BoxOptions(image=image, cpus=cpus, memory_mib=memory_mib,
                               disk_size_gb=disk_gb, **kwargs),
            name=name)
        return got[0] if isinstance(got, tuple) else got

    async def start(self, name: str):
        self._require()
        box = await self._rt().get(name)
        await box.start()

    async def stop(self, name: str):
        self._require()
        box = await self._rt().get(name)
        await box.stop()

    async def remove(self, name: str):
        self._require()
        rt = self._rt()
        try:
            box = await rt.get(name)
        except Exception:                         # already gone
            return
        try:
            await box.stop()      # a running box cannot be removed; note that
                                  # auto_delete boxes vanish on stop
        except Exception:                         # noqa: BLE001
            pass
        try:
            await rt.remove(name)
        except Exception:                         # already gone: stop removed it
            pass

    async def fork(self, template: str, name: str):
        self._require()
        tpl = await self._rt().get(template)
        return await tpl.clone_box(name=name)

    async def copy_in(self, name: str, host_path: str, box_path: str):
        """Stage a host file into the box (the parent dir must exist)."""
        self._require()
        box = await self._rt().get(name)
        await box.copy_in(host_path, box_path)

    async def list_names(self) -> List[str]:
        """Names of every box the runtime knows. Best-effort: the SDK's
        list shape has changed across versions, so failures degrade to an
        empty list rather than breaking idempotency checks."""
        self._require()
        try:
            infos = await self._rt().list_info()
            names = []
            for info in infos or []:
                name = getattr(info, "name", None) or (
                    info.get("name") if isinstance(info, dict) else None)
                if name:
                    names.append(name)
            return names
        except Exception:                             # noqa: BLE001
            return []

    async def wait_ready(self, name: str, timeout: float = 180.0) -> bool:
        """Block until the in-box agent answers a trivial exec.

        `start()` returns before the agent inside is listening — a pip install
        fired immediately hits "Connection refused" (gRPC/tonic, os error 61).
        Boot takes a handful of seconds on this machine; the deadline is generous
        because a cold image pull can be much slower.
        """
        self._require()
        deadline = asyncio.get_event_loop().time() + timeout
        last = ""
        while asyncio.get_event_loop().time() < deadline:
            try:
                res = await self.exec(name, "true", [])
                if res.get("exit") == 0:
                    return True
                last = res.get("stderr", "")[:90]
            except Exception as e:                    # noqa: BLE001
                last = str(e)[:90]
            await asyncio.sleep(2)
        return False

    async def ping(self, name: str) -> bool:
        """Is the box known to the runtime? (Boot state is the caller's job.)"""
        try:
            await self._rt().get(name)
            return True
        except Exception:                             # noqa: BLE001
            return False

    async def exec(self, name: str, cmd: str, args: list = None,
                   cwd: str = None):
        """Run a command in the box. Returns a dict contract:
        {stdout, stderr, exit} — exit 0 unless the SDK raised ExecError.

        Retries once on `execution_exists`: the box runs one exec at a time,
        and a previous exec still draining would otherwise fail the step.

        MEASURED LIVE: the boxlite agent re-joins command and args into a
        SHELL LINE — an argument containing spaces is split back into words at
        the far end. "pip install -q neuralosd" survives by luck; any argument
        with quoting or spaces breaks. Each argument is therefore shell-quoted
        here, which makes this exec behave like real argv. (It also makes
        `sh -c <script>` correct instead of accidentally-working.)
        """
        self._require()
        box = await self._rt().get(name)
        out: List[str] = []
        err: List[str] = []
        code = 0
        safe_args = [shlex.quote(a) if any(c in a for c in " \"'\t") else a
                     for a in (args or [])]
        for attempt in (0, 1):
            out, err = [], []
            code = 0
            try:
                ex = await box.exec(cmd, safe_args, timeout_secs=300, cwd=cwd)
                async for line in ex.stdout():
                    out.append(line)
                async for line in (ex.stderr() or []):
                    err.append(line)
                await ex.wait()
                break
            except Exception as e:                    # noqa: BLE001
                code = 1
                err.append(str(e))
                if "execution_exists" in str(e).lower() and attempt == 0:
                    await asyncio.sleep(2)
                    continue
        return {"stdout": "".join(out), "stderr": "".join(err), "exit": code}

    async def export(self, name: str, dest: str):
        self._require()
        box = await self._rt().get(name)
        await box.export(dest=dest)

    @staticmethod
    def available() -> bool:
        try:
            import boxlite
            return True
        except ImportError:
            return False

    @staticmethod
    def name() -> str:
        return "boxlite"
