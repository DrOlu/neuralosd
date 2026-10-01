"""neuralosd sidecar — run an instance's probes in the HOST's Python.

Why this exists
---------------
A frozen (Nuitka/PyInstaller) binary is sealed: it carries a fixed set of
libraries and CANNOT import anything from the host, even if you `pip install`
it.  That is fine for the stable core (CSV/JSON/Excel, the model, sandboxes)
but it makes "I want pypdf / pywinrm / some new SDK" impossible without
rebuilding the binary.

The sidecar removes that treadmill.  The binary keeps the *intelligence*
(routing, verification, serving); this helper keeps the *data access*.  The
binary launches it with the host interpreter; the helper loads ``probes.py``
there — where you may ``pip install`` anything — and executes probes on
request over newline-delimited JSON-RPC on stdin/stdout.

Protocol (one JSON object per line)
-----------------------------------
request : {"id": 1, "method": "ping"|"menu"|"call"|"shutdown", "params": {...}}
response: {"id": 1, "ok": true,  "result": ...}
        | {"id": 1, "ok": false, "error": "..."}

`call` params: {"probe": "<name>", "args": {...}}
`menu` result: [{"name","description","triggers","args","pii","conf_gate",
                 "tier","confirm"}, ...]

A first line with ``"event": "ready"`` (or ``"event": "error"``) is emitted
before any request is answered.
"""
import importlib.util
import json
import os
import sys


def load_probe_functions(instance_dir: str):
    """Import ``<instance_dir>/probes.py`` and return its probe functions.

    Mirrors the loader in ``neuralosd._cmd._common`` (PROBES list first, then
    any ``@probe``-decorated function) so both sides see the same capabilities.
    """
    instance_dir = os.path.abspath(instance_dir)
    probes_py = os.path.join(instance_dir, "probes.py")
    if not os.path.isfile(probes_py):
        raise FileNotFoundError(f"no probes.py in {instance_dir}")

    if instance_dir not in sys.path:
        sys.path.insert(0, instance_dir)

    name = os.path.basename(instance_dir.rstrip("/")) or "instance"
    modname = f"neuralosd_sidecar_inst_{name}"
    spec = importlib.util.spec_from_file_location(modname, probes_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)

    probes = list(getattr(mod, "PROBES", []))
    if not probes:
        probes = [v for v in vars(mod).values()
                  if callable(v) and hasattr(v, "_probe")]
    if not probes:
        raise ValueError(f"{probes_py} defines no probes")
    return probes


def probe_meta(fn) -> dict:
    """Serialisable view of a probe's declaration."""
    m = fn._probe
    return {"name": m.name,
            "description": m.description,
            "triggers": list(m.triggers),
            "args": dict(m.args),
            "pii": list(m.pii),
            "conf_gate": m.conf_gate,
            "tier": m.tier,
            "confirm": m.confirm}


class Sidecar:
    """Serves one instance's probes over the stdin/stdout protocol."""

    def __init__(self, instance_dir: str):
        self.instance_dir = os.path.abspath(instance_dir)
        self.probes = load_probe_functions(self.instance_dir)
        self.by_name = {p._probe.name: p for p in self.probes}

    # -- request handling --------------------------------------------------
    def handle(self, req: dict) -> dict:
        rid = req.get("id")
        method = req.get("method")
        try:
            if method == "ping":
                return {"id": rid, "ok": True,
                        "result": {"pong": True,
                                   "python": sys.version.split()[0],
                                   "instance": self.instance_dir,
                                   "probes": len(self.probes)}}
            if method == "menu":
                return {"id": rid, "ok": True,
                        "result": [probe_meta(p) for p in self.probes]}
            if method == "call":
                return self._call(rid, req.get("params") or {})
            if method == "shutdown":
                return {"id": rid, "ok": True, "result": {"bye": True},
                        "_stop": True}
            return {"id": rid, "ok": False,
                    "error": f"unknown method: {method!r}"}
        except ModuleNotFoundError as e:
            return {"id": rid, "ok": False,
                    "error": f"ModuleNotFoundError: {e.name!r} is not "
                             f"installed for {sys.executable}. Install it "
                             f"there (pip install {e.name or '<name>'})."}
        except Exception as e:  # noqa: BLE001
            return {"id": rid, "ok": False,
                    "error": f"{type(e).__name__}: {e}"}

    def _call(self, rid, params: dict) -> dict:
        name = params.get("probe")
        args = params.get("args") or {}
        fn = self.by_name.get(name)
        if fn is None:
            return {"id": rid, "ok": False, "error": f"unknown probe: {name!r}"}
        out = fn(**args)
        if isinstance(out, dict):
            out = {**out, "_tool": name}
        return {"id": rid, "ok": True, "result": out}


# -- entry point -----------------------------------------------------------

def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="neuralosd-sidecar",
        description="Run a neuralOS instance's probes in the host Python "
                    "(lets a frozen neuralosd binary use arbitrary libraries).")
    ap.add_argument("--instance-dir", required=True)
    a = ap.parse_args(argv)

    try:
        sc = Sidecar(a.instance_dir)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"id": 0, "ok": False, "event": "error",
                          "error": f"{type(e).__name__}: {e}"}), flush=True)
        return 2

    print(json.dumps({"id": 0, "ok": True, "event": "ready",
                      "probes": len(sc.probes), "instance": sc.instance_dir}),
          flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(json.dumps({"id": None, "ok": False,
                              "error": f"bad request json: {e}"}), flush=True)
            continue
        resp = sc.handle(req)
        stop = resp.pop("_stop", False)
        print(json.dumps(resp, default=str), flush=True)
        if stop:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
