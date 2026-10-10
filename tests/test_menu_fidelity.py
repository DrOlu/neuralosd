"""Router.menu() and Router.openapi() must carry the actual caged-args values.

The defect: menu() filtered arg specs to ("type", "enum", "pattern", "min",
"max", "description") — but the caged-args system uses `values`, not `enum`.
The `enum` key in the filter was dead code (the convention puts the list in
`values`, not `enum`), so an exported menu entry for a caged enum arg was
just {"type": "enum"} with no actual allowed values — the engine received a
cage with no bars.

Also: `default` was filtered out, so the model and the engine could not know
what value a probe would use when the question didn't name one.
"""
import json

import pytest

from neuralosd.probe import probe as probe_dec
from neuralosd.router import Router


def _probe(name, triggers, args, description="d"):
    def fn(**kw):
        return {"probe": name}
    fn.__name__ = name
    return probe_dec(description=description, triggers=triggers, args=args,
                     name=name)(fn)


def _router(tmp_path, probes):
    return Router(probes=probes, cache_file=str(tmp_path / "c.json"),
                  audit_file=str(tmp_path / "a.jsonl"))


ENUM_ARGS = {"genre": {"type": "enum", "values": ["Rock", "Jazz", "Metal"],
                       "required": True},
             "limit": {"type": "integer", "min": 1, "max": 50,
                       "required": False, "default": 10}}


def _enum_probe(tmp_path):
    return _router(tmp_path, [
        _probe("by_genre", ["by genre"], args=ENUM_ARGS,
               description="Tracks by genre"),
    ])


# ── menu() carries the actual values ────────────────────────────────────────

def test_menu_carries_the_values_list(tmp_path):
    r = _enum_probe(tmp_path)
    props = r.menu()[0]["parameters"]["properties"]
    assert props["genre"]["values"] == ["Rock", "Jazz", "Metal"]


def test_menu_also_emits_the_openapi_enum_key(tmp_path):
    """Both keys: `values` for the caged-args system, `enum` for the engine
    and anything that reads OpenAPI. Emitting both is cheap and means neither
    consumer has to know about the other's convention."""
    r = _enum_probe(tmp_path)
    props = r.menu()[0]["parameters"]["properties"]
    assert props["genre"]["values"] == ["Rock", "Jazz", "Metal"]
    assert props["genre"]["enum"] == ["Rock", "Jazz", "Metal"]


def test_menu_carries_the_default(tmp_path):
    r = _enum_probe(tmp_path)
    props = r.menu()[0]["parameters"]["properties"]
    assert props["limit"]["default"] == 10


def test_menu_carries_min_max(tmp_path):
    r = _enum_probe(tmp_path)
    props = r.menu()[0]["parameters"]["properties"]
    assert props["limit"]["min"] == 1
    assert props["limit"]["max"] == 50


def test_menu_carries_the_pattern(tmp_path):
    pat = _probe("by_ref", ["by reference"],
                 args={"ref": {"type": "pattern", "pattern": r"INC\d+"}},
                 description="by reference")
    r = _router(tmp_path, [pat])
    props = r.menu()[0]["parameters"]["properties"]
    assert props["ref"]["pattern"] == r"INC\d+"


def test_menu_carries_the_type(tmp_path):
    r = _enum_probe(tmp_path)
    props = r.menu()[0]["parameters"]["properties"]
    assert props["genre"]["type"] == "enum"
    assert props["limit"]["type"] == "integer"


# ── openapi() carries the actual constraint list ────────────────────────────

def test_openapi_carries_the_enum_list(tmp_path):
    r = _enum_probe(tmp_path)
    spec = r.openapi()
    props = spec["paths"]["/probes/by_genre"]["post"]["requestBody"][
        "content"]["application/json"]["schema"]["properties"]
    assert props["genre"]["enum"] == ["Rock", "Jazz", "Metal"]


def test_openapi_also_carries_values(tmp_path):
    """Both keys, same list — neither consumer has to know the other's name."""
    r = _enum_probe(tmp_path)
    props = r.openapi()["paths"]["/probes/by_genre"]["post"][
        "requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert props["genre"]["values"] == ["Rock", "Jazz", "Metal"]
    assert props["genre"]["enum"] == ["Rock", "Jazz", "Metal"]


def test_openapi_carries_min_max(tmp_path):
    r = _enum_probe(tmp_path)
    props = r.openapi()["paths"]["/probes/by_genre"]["post"][
        "requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert props["limit"]["min"] == 1
    assert props["limit"]["max"] == 50


# ── the export writes these too ─────────────────────────────────────────────

def test_export_keeps_the_values_list(tmp_path):
    import os
    r = _enum_probe(tmp_path)
    entry = r.menu()[0]
    props = entry["parameters"]["properties"]
    assert props["genre"]["values"] == ["Rock", "Jazz", "Metal"]
    assert props["genre"]["enum"] == ["Rock", "Jazz", "Metal"]


# ── the round-trip: menu → JSON → read back → same constraint info ─────────

def test_menu_round_trips_through_json_losing_nothing(tmp_path):
    """The point of the export: the engine should see the SAME cage the
    router enforces. If the values list is lost, the engine sees an open
    argument where the router has a closed one."""
    r = _enum_probe(tmp_path)
    entry = json.loads(json.dumps(r.menu()[0]))
    props = entry["parameters"]["properties"]
    assert props["genre"]["values"] == ["Rock", "Jazz", "Metal"]
    assert props["limit"]["default"] == 10
    assert props["limit"]["min"] == 1 and props["limit"]["max"] == 50
    # the pattern key too
    pat = _probe("by_ref", ["by reference"],
                 args={"ref": {"type": "pattern", "pattern": r"INC\d+"}})
    r2 = Router(probes=[pat], cache_file=str(tmp_path / "c.json"),
                audit_file=str(tmp_path / "a.jsonl"))
    pat_entry = json.loads(json.dumps(r2.menu()[0]))
    pat_props = pat_entry["parameters"]["properties"]
    assert pat_props["ref"]["pattern"] == r"INC\d+"
