"""`neuralosd serve` — HTTP service for one instance."""
import sys


def run(a):
    from ._common import load_instance
    from ..service import serve

    inst = load_instance(a.instance_dir, with_model=getattr(a, "model", False))
    print(f"serving instance '{inst.name}' with {len(inst.probes)} probes "
          f"on http://0.0.0.0:{a.port}", file=sys.stderr)
    serve(inst, port=a.port)