"""Diagnostic probe: report how the frozen binary sees the msb bundle.

Copied to ``diaginst/probes.py`` by CI and run with
``neuralosd-msb ask --instance-dir diaginst diag``. Never imported by the
package itself — a test fixture only.
"""
import os
import sys

from neuralosd import probe


@probe(description="diagnose msb bundle layout", triggers=["diag"])
def diag():
    out = {
        "sys.executable": sys.executable,
        "frozen": getattr(sys, "frozen", None),
        "compiled": "__compiled__" in globals(),
    }
    try:
        import microsandbox
        d = os.path.dirname(os.path.abspath(microsandbox.__file__))
        out["microsandbox.__file__"] = microsandbox.__file__
        bb = os.path.join(d, "_bundled", "bin")
        out["_bundled/bin exists"] = os.path.isdir(bb)
        out["_bundled/bin"] = os.listdir(bb) if os.path.isdir(bb) else None
        lb = os.path.join(d, "_bundled", "lib")
        out["_bundled/lib"] = os.listdir(lb) if os.path.isdir(lb) else None
        out["pkg dir listing"] = sorted(os.listdir(d))[:25]
    except Exception as e:  # noqa: BLE001
        out["microsandbox import error"] = repr(e)
    try:
        from neuralosd.backends import msb_backend
        out["candidates"] = msb_backend._candidate_paths()
        out["resolved"] = msb_backend.msb_binary()
    except Exception as e:  # noqa: BLE001
        out["backend error"] = repr(e)
    try:
        out["exe dir"] = sorted(os.listdir(os.path.dirname(sys.executable)))[:25]
    except Exception as e:  # noqa: BLE001
        out["exe dir error"] = repr(e)
    return out


PROBES = [diag]
