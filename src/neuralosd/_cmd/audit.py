"""`neuralosd audit` — classify every instance import against the runtime.

Closes the "frozen binary can't see instance imports" gap at BIRTH: run it
after any instance is created (or its bridge.py changes) and it names every
module the binary + sidecar cannot satisfy — before the instance ships.

  neuralosd audit --instance-dir ./my-instance
  neuralosd audit --root ./fleet --host-python C:\\venv\\python.exe --json

Exit code is non-zero when any module is missing-everywhere, so the
instance-creation flow can gate on it.
"""
import json
import os
import sys


def run(a):
    from ..instance_audit import audit_instance, audit_root, print_report, to_json

    instance_dir = getattr(a, "instance_dir", None)
    root = getattr(a, "root", None)
    host_exe = getattr(a, "host_python", None) or os.environ.get(
        "NEURALOSD_SIDECAR") or None

    if not instance_dir and not root:
        print("error: pass --instance-dir DIR or --root DIR", file=sys.stderr)
        raise SystemExit(2)

    report = (audit_root(root, host_exe=host_exe) if root
              else {"root": instance_dir, "runtime":
                    "frozen" if getattr(sys, "frozen", False) else "pip",
                    "host_python": host_exe,
                    "instances": [],
                    "ok": True})
    if instance_dir:
        from ..instance_audit import current_runtime_importable, collect_instance_imports
        all_mods = collect_instance_imports(instance_dir)
        bundled = current_runtime_importable(all_mods)
        host = set(bundled)
        if host_exe:
            from ..instance_audit import importable_set
            host |= importable_set(host_exe, all_mods - bundled)
        rep = audit_instance(instance_dir, bundled, host)
        report["instances"] = [rep]
        report["ok"] = rep["ok"]

    if getattr(a, "json", False):
        print(to_json(report))
    else:
        print(print_report(report))
        n_bad = sum(1 for r in report["instances"] if not r["ok"])
        print(f"\naudit: {len(report['instances'])} instance(s), "
              f"{n_bad} will fail")
    if not report["ok"]:
        raise SystemExit(1)
