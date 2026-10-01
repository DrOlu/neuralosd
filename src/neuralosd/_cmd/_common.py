"""Shared helpers for neuralosd CLI commands."""
import importlib.util
import os
import sys
from typing import Optional


# Missing-module -> (human label, pip package). Used to turn a bare
# "No module named 'pymysql'" into an actionable message. A probe's data
# layer may import any of these lazily at call time.
_MODULE_HINTS = {
    "pymysql": ("the MySQL driver", "pymysql"),
    "MySQLdb": ("the MySQL driver (mysqlclient)", "mysqlclient"),
    "mysql": ("the MySQL connector", "mysql-connector-python"),
    "psycopg2": ("the PostgreSQL driver", "psycopg2-binary"),
    "psycopg": ("the PostgreSQL driver", "psycopg[binary]"),
    "pyodbc": ("the ODBC / SQL Server driver", "pyodbc"),
    "oracledb": ("the Oracle driver", "oracledb"),
    "cx_Oracle": ("the Oracle driver", "cx_Oracle"),
    "sqlalchemy": ("SQLAlchemy", "sqlalchemy"),
    "pandas": ("pandas", "pandas"),
    "openpyxl": ("the Excel reader", "openpyxl"),
    "requests": ("the HTTP client", "requests"),
    "boxlite": ("the BoxLite sandbox backend", "boxlite"),
    "microsandbox": ("the Microsandbox backend", "microsandbox"),
    "needle": ("the on-device neuralOS model", "neuralos"),
    "pydantic": ("Pydantic", "pydantic"),
}


def _is_frozen() -> bool:
    """True inside a Nuitka / PyInstaller standalone build."""
    if getattr(sys, "frozen", False):
        return True
    try:
        return bool(__compiled__)  # type: ignore[name-defined]  # noqa: F821
    except NameError:
        return False


def _missing_module_help(missing: str, source: str) -> str:
    root = (missing or "").split(".")[0] or "<unknown>"
    hint = _MODULE_HINTS.get(root)
    out = [f"error: {source} needs the Python module '{missing}',",
           "       which is not available in this environment.", ""]
    if _is_frozen():
        out += [
            "You are running the standalone binary. It embeds the neuralosd",
            "framework, the docs, the skills, pydantic and the on-device model —",
            "but a frozen binary is sealed: you cannot pip install into it.",
            "",
        ]
    if hint:
        label, pkg = hint
        out += [f"'{missing}' is {label}. Install it where neuralosd can see it:",
                "", f"    pip install {pkg}"]
        if root in ("boxlite", "microsandbox", "needle"):
            out.append("    pip install 'neuralosd[all]'      # everything at once")
    else:
        out += ["Install the missing module:", "", f"    pip install {root}"]
    out += ["", "Or switch to the full install:  pip install 'neuralosd[all]'",
            "See:  neuralosd docs usage"]
    return "\n".join(out)


def load_instance(instance_dir: str, with_model: bool = False):
    """Load an :class:`~neuralosd.instance.Instance` from a directory.

    Resolution order:
      1. ``<dir>/probes.py``                    -> single instance in ``dir``
      2. exactly one ``<dir>/*/probes.py``       -> that sub-instance
      3. otherwise raise SystemExit with a helpful message

    The instance dir is added to ``sys.path`` so sibling modules
    (``bridge.py``, ``models.py``) import normally. Probes come from a
    module-level ``PROBES`` list, falling back to every ``@probe``-decorated
    function in the module.
    """
    from ..instance import Instance

    instance_dir = os.path.abspath(os.path.expanduser(instance_dir))
    probes_py = os.path.join(instance_dir, "probes.py")

    if not os.path.isfile(probes_py):
        if not os.path.isdir(instance_dir):
            raise SystemExit(f"error: instance dir not found: {instance_dir}")
        subs = [d for d in sorted(os.listdir(instance_dir))
                if os.path.isfile(os.path.join(instance_dir, d, "probes.py"))]
        if len(subs) == 1:
            instance_dir = os.path.join(instance_dir, subs[0])
            probes_py = os.path.join(instance_dir, "probes.py")
        elif subs:
            raise SystemExit(
                f"error: {instance_dir} contains multiple instances "
                f"({', '.join(subs)}); pass one of them explicitly")
        else:
            raise SystemExit(
                f"error: no probes.py found in {instance_dir}\n"
                "hint: create one with `neuralosd init --source <file> --name <n>`"
                " or point --instance-dir at an instance directory")

    name = os.path.basename(instance_dir.rstrip("/")) or "instance"

    try:
        probes = _load_probes_in_process(probes_py, instance_dir, name)
    except ModuleNotFoundError as e:
        # A probe (or its bridge) imports a driver/model this environment
        # does not have. Before giving up, try the sidecar: a host-Python
        # helper where arbitrary libraries CAN be installed.
        probes = _probes_via_sidecar_or_die(
            instance_dir, name, probes_py, getattr(e, "name", ""))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"error: failed to load {probes_py}: {e}")

    # Probes explicitly declared tier="sidecar" run in the host Python even
    # when the instance loaded fine here (e.g. to use a library the frozen
    # binary lacks while everything else stays in-process).
    probes = _maybe_redirect_tier_sidecar(probes, instance_dir)

    # A probe's dependency is often imported INSIDE the function, so a missing
    # library shows up at CALL time, not load time — the module imports fine
    # and the failure would otherwise be swallowed into an error envelope
    # (a false negative: never delegated). Wrap every locally-run probe so a
    # call-time ModuleNotFoundError retries in the sidecar.
    probes = _wrap_with_sidecar_fallback(probes, instance_dir)

    model_fallback = _build_model_fallback(probes) if with_model else None
    return Instance(name=name, probes=probes,
                    model_fallback=model_fallback, state_dir=instance_dir)


def _wrap_with_sidecar_fallback(probes, instance_dir):
    """Retry a probe in the sidecar when its (lazy) import is missing here.

    The sidecar is started lazily — only on the first call that actually needs
    it — so instances whose dependencies are all present never pay for a
    subprocess.
    """
    holder = {"client": None, "tried": False}

    def client():
        if not holder["tried"]:
            holder["tried"] = True
            from ..sidecar_client import (SidecarClient, SidecarError,
                                          sidecar_command)
            if sidecar_command():
                try:
                    holder["client"] = SidecarClient(instance_dir).start()
                except SidecarError:
                    holder["client"] = None
        return holder["client"]

    out = []
    for fn in probes:
        meta = getattr(fn, "_probe", None)
        if meta is None or getattr(fn, "_sidecar_backed", False):
            out.append(fn)          # already a forwarding stub
            continue
        out.append(_make_fallback_probe(fn, meta, client))
    return out


def _make_fallback_probe(fn, meta, client):
    def wrapper(**kwargs):
        try:
            return fn(**kwargs)
        except ModuleNotFoundError as e:
            c = client()
            if c is None:
                raise SystemExit(_missing_module_help(
                    getattr(e, "name", "") or "<unknown>",
                    f"probe '{meta.name}'"))
            return c.call(meta.name, kwargs)

    wrapper.__name__ = meta.name
    wrapper._probe = meta
    meta.function = wrapper
    return wrapper


def _load_probes_in_process(probes_py, instance_dir, name):
    """Import probes.py here. Raises ModuleNotFoundError when a probe's
    dependency is missing (the caller may then fall back to the sidecar)."""
    if instance_dir not in sys.path:
        sys.path.insert(0, instance_dir)
    modname = f"neuralosd_inst_{name}"
    spec = importlib.util.spec_from_file_location(modname, probes_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)

    probes = list(getattr(mod, "PROBES", []))
    if not probes:
        probes = [v for v in vars(mod).values()
                  if callable(v) and hasattr(v, "_probe")]
    if not probes:
        raise SystemExit(
            f"error: {probes_py} defines no probes.\n"
            "hint: add `PROBES = [my_probe, ...]` or decorate functions "
            "with @probe(...)")
    return probes


def _probes_via_sidecar_or_die(instance_dir, name, probes_py, missing):
    """Build sidecar-backed probes, or exit with the actionable message."""
    from ..sidecar_client import SidecarClient, SidecarError, sidecar_command

    if not sidecar_command():
        raise SystemExit(_missing_module_help(missing, probes_py))
    try:
        client = SidecarClient(instance_dir).start()
        metas = client.menu()
    except SidecarError as e:
        msg = _missing_module_help(missing, probes_py)
        raise SystemExit(
            f"{msg}\n\nthe sidecar was found but failed: {e}\n"
            "(the sidecar runs in the HOST python — install the missing "
            "library there, e.g. `pip install " + (missing or "<lib>") + "`)")
    return _stubs_from_menu(metas, client)


def _stubs_from_menu(metas, client):
    """Turn a sidecar menu into probe functions that forward their calls."""
    from ..probe import ProbeMeta

    out = []
    for m in metas:
        def make(meta):
            def fn(**kwargs):
                return client.call(meta["name"], kwargs)
            fn.__name__ = meta["name"]
            fn._probe = ProbeMeta(
                name=meta["name"],
                description=meta.get("description", ""),
                triggers=list(meta.get("triggers") or []),
                args=dict(meta.get("args") or {}),
                pii=list(meta.get("pii") or []),
                conf_gate=meta.get("conf_gate"),
                tier=meta.get("tier") or "sidecar",
                confirm=bool(meta.get("confirm")),
                function=None,
            )
            fn._probe.function = fn
            fn._sidecar_backed = True   # already delegates; don't re-wrap
            return fn
        out.append(make(m))
    return out


def _maybe_redirect_tier_sidecar(probes, instance_dir):
    """Replace tier="sidecar" probes with forwarding stubs (needs a sidecar)."""
    wanted = [p for p in probes
              if getattr(getattr(p, "_probe", None), "tier", None) == "sidecar"]
    if not wanted:
        return probes
    from ..sidecar_client import SidecarClient, SidecarError, sidecar_command
    if not sidecar_command():
        raise SystemExit(
            "error: this instance has probes with tier=\"sidecar\", but no "
            "sidecar was found.\n"
            "hint: pip install neuralosd   (provides the neuralosd-sidecar "
            "command) or set $NEURALOSD_SIDECAR")
    try:
        client = SidecarClient(instance_dir).start()
        metas = {m["name"]: m for m in client.menu()}
    except SidecarError as e:
        raise SystemExit(f"error: sidecar unavailable for tier=\"sidecar\" "
                         f"probes: {e}")

    names = {p._probe.name for p in wanted}
    stubs = {p._probe.name: p for p in _stubs_from_menu(
        [metas[n] for n in names if n in metas], client)}
    out = []
    for p in probes:
        out.append(stubs.get(p._probe.name, p))
    return out


def _build_model_fallback(probes):
    """Model fallback using the on-device neuralOS/needle model.

    Only called when the caller explicitly asked for it (``--model``). If the
    model runtime is missing, this is a hard error — the user asked for the
    model and silently ignoring it would be worse than failing.
    """
    try:
        import needle  # noqa: F401
    except ModuleNotFoundError as e:
        raise SystemExit(_missing_module_help(
            getattr(e, "name", "needle") or "needle", "the --model flag"))

    def _fallback(question: str, menu):
        try:
            from .model_bridge import model_ask  # type: ignore
            return model_ask(question, menu)
        except Exception:  # noqa: BLE001
            return None
    return _fallback