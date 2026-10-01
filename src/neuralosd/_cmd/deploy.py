"""`neuralosd deploy` — deploy an instance into a sandbox backend."""
import asyncio
import os
import sys


def run(a):
    from ..backends import get_backend, available_backends

    inst_dir = os.path.abspath(a.instance_dir)
    if not os.path.isfile(os.path.join(inst_dir, "probes.py")):
        raise SystemExit(f"error: {inst_dir} has no probes.py")

    avail = available_backends()
    if not avail.get(a.backend):
        raise SystemExit(
            f"error: backend '{a.backend}' is not available on this host.\n"
            f"  boxlite: pip install boxlite (and run `boxlite serve`)\n"
            f"  msb:     install the Microsandbox `msb` CLI\n"
            f"  available now: {[k for k, v in avail.items() if v]}")

    backend = get_backend(a.backend)
    name = a.name
    print(f"deploying '{name}' via {a.backend} ...", file=sys.stderr)

    async def _go():
        if hasattr(backend, "create") and asyncio.iscoroutinefunction(backend.create):
            await backend.create(name)
        else:
            backend.create(name)
        # install neuralosd + start the service inside the sandbox
        start = (f"pip install -q neuralosd && cd /app && "
                 f"python -m neuralosd.cli serve --instance-dir /app "
                 f"--port {a.port}")
        if asyncio.iscoroutinefunction(backend.exec):
            return await backend.exec(name, start)
        return backend.exec(name, start)

    try:
        asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"deploy failed: {e}")

    print(f"deployed '{name}' ({a.backend}); service on port {a.port}")