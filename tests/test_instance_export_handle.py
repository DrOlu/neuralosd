"""F12 — `Instance.export` must close the file it opens.

The old body was `json.dump(self.menu, open(path, "w", ...))`: the handle
was only released when the object was collected. It now uses a context
manager, so the file is closed deterministically even if dump raises.
"""
import builtins
import json
from types import SimpleNamespace
from unittest import mock

from neuralosd.instance import Instance

MENU = [{"name": "row_count", "triggers": ["how many rows"],
         "description": "rows", "parameters": {}}]


def test_export_closes_the_handle(tmp_path):
    path = tmp_path / "menu.json"
    handles = []
    real_open = builtins.open

    def tracking_open(*a, **kw):
        fh = real_open(*a, **kw)
        handles.append(fh)
        return fh

    stub = SimpleNamespace(menu=MENU)
    with mock.patch("builtins.open", tracking_open):
        Instance.export(stub, str(path))
    assert handles, "export did not open the target"
    assert all(fh.closed for fh in handles)
    assert json.loads(path.read_text(encoding="utf-8")) == MENU
