"""BoxLite backend — lifecycle management for BoxLite microVM boxes."""
import asyncio
from typing import Any, Dict, Optional


class BoxLiteBackend:
    """Manages BoxLite box lifecycle for neuralOS instances."""

    def __init__(self, serve_url: str = None, api_key: str = None):
        self.serve_url = serve_url
        self.api_key = api_key

    def _rt(self):
        import boxlite
        if self.serve_url:
            cred = boxlite.ApiKeyCredential(self.api_key) if self.api_key else None
            return boxlite.Boxlite.rest(
                boxlite.BoxliteRestOptions(url=self.serve_url, credential=cred))
        return boxlite.Boxlite.default()

    async def create(self, name: str, image: str = "python:3.12-slim",
                     cpus: int = 1, memory_mib: int = 1024,
                     disk_gb: int = 6, **kwargs):
        rt = self._rt()
        got = await rt.get_or_create(
            boxlite.BoxOptions(image=image, auto_delete=0,
                               cpus=cpus, memory_mib=memory_mib,
                               disk_size_gb=disk_gb, **kwargs),
            name=name)
        return got[0] if isinstance(got, tuple) else got

    async def start(self, name: str):
        box = await self._rt().get(name)
        await box.start()

    async def stop(self, name: str):
        box = await self._rt().get(name)
        await box.stop()

    async def remove(self, name: str):
        await self._rt().remove(name)

    async def fork(self, template: str, name: str):
        tpl = await self._rt().get(template)
        return await tpl.clone_box(name=name)

    async def exec(self, name: str, cmd: str, args: list = None):
        box = await self._rt().get(name)
        ex = await box.exec(cmd, args or [], timeout_secs=300)
        out = []
        async for line in ex.stdout(): out.append(line)
        await ex.wait()
        return "".join(out)

    async def export(self, name: str, dest: str):
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
