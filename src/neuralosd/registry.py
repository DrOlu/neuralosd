"""Registry — discover/load instances from a directory of instance dirs.

Contract per instance dir: a `probes.py` exposing `PROBES` (list of @probe
functions). The dir is appended to sys.path so sibling modules (bridge.py,
models.py) import normally.
"""
import importlib.util
import os
import sys


class Registry:
    def __init__(self, instances_dir: str):
        self.instances_dir = os.path.abspath(instances_dir)

    def discover(self):
        """Returns {name: {"dir":…, "probes":[…]}} for every instance dir with
        a probes.py. Imports happen once per unique module name."""
        found = {}
        if not os.path.isdir(self.instances_dir):
            return found
        for entry in sorted(os.listdir(self.instances_dir)):
            d = os.path.join(self.instances_dir, entry)
            if os.path.isfile(os.path.join(d, "probes.py")):
                found[entry] = self.load(entry)
        return found

    def load(self, name: str):
        d = os.path.join(self.instances_dir, name)
        if d not in sys.path:
            sys.path.append(d)
        modname = f"neuralosd_inst_{name}"
        spec = importlib.util.spec_from_file_location(
            modname, os.path.join(d, "probes.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
        probes = list(getattr(mod, "PROBES", []))
        return {"dir": d, "probes": probes}

    def reload(self, name: str):
        modname = f"neuralosd_inst_{name}"
        sys.modules.pop(modname, None)
        return self.load(name)
