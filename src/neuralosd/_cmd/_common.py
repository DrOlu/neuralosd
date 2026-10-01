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

    if instance_dir not in sys.path:
        sys.path.insert(0, instance_dir)

    name = os.path.basename(instance_dir.rstrip("/")) or "instance"
    modname = f"neuralosd_inst_{name}"
    spec = importlib.util.spec_from_file_location(modname, probes_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as e:
        # A probe (or its bridge) imports a driver/model the environment
        # doesn't have. Give an actionable message instead of a traceback.
        raise SystemExit(_missing_module_help(getattr(e, "name", ""), probes_py))
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"error: failed to load {probes_py}: {e}")

    probes = list(getattr(mod, "PROBES", []))
    if not probes:
        probes = [v for v in vars(mod).values()
                  if callable(v) and hasattr(v, "_probe")]
    if not probes:
        raise SystemExit(
            f"error: {probes_py} defines no probes.\n"
            "hint: add `PROBES = [my_probe, ...]` or decorate functions "
            "with @probe(...)")

    model_fallback = _build_model_fallback(probes) if with_model else None
    return Instance(name=name, probes=probes,
                    model_fallback=model_fallback, state_dir=instance_dir)


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