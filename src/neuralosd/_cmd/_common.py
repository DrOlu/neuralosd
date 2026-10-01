"""Shared helpers for neuralosd CLI commands."""
import importlib.util
import os
import sys
from typing import Optional


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
    """Best-effort model fallback using the on-device neuralOS/needle model.

    Returns None when the model runtime is unavailable so the deterministic
    fast path still works.
    """
    try:
        import needle  # noqa: F401
    except Exception:  # noqa: BLE001
        return None

    def _fallback(question: str, menu):
        try:
            from .model_bridge import model_ask  # type: ignore
            return model_ask(question, menu)
        except Exception:  # noqa: BLE001
            return None
    return _fallback