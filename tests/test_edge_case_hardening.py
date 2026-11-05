"""Edge-case hardening added in 2.9.1.

Every test here is a defect that was reproduced against 2.9.0 before the fix.
They are grouped by the class of harm, because the class is what matters:

  * a corrupt CACHE must never be able to break an answer (availability),
  * an ARGUMENT guess must never quietly answer a different question
    (false positive), and must never quietly refuse a legitimate one
    (false negative),
  * a RESULT that is valid Python must never crash the audit path,
  * a SECRET under a secret key must be masked whatever its JSON type,
  * a NAMING collision must be loud, not a silent shadow.

Nothing here needs a database or network.
"""
import json
import os
import sys
import math

import pytest

from neuralosd import oracles

from neuralosd.probe import probe as probe_dec
from neuralosd.router import MASKED, NoResults, Router, mask_pii


def _probe(name, triggers, args=None, description="d", fn=None):
    def call(**kw):
        return fn(**kw) if fn else [{"probe": name}]
    call.__name__ = name
    return probe_dec(description=description, triggers=triggers,
                     args=args or {}, name=name)(call)


def _router(tmp_path, probes, **kw):
    return Router(probes=probes, cache_file=str(tmp_path / "ask_cache.json"),
                  audit_file=str(tmp_path / "ask_audit.jsonl"), **kw)


# ── cache integrity ────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"ts": 1.0, "menu_version": None},          # no payload at all
    {"ts": 1.0, "payload": "not-a-dict"},       # payload of the wrong type
    {"ts": "yesterday", "payload": {}},         # unparsable timestamp
    {"payload": {}},                            # no ts, no menu_version
    {},                                         # empty entry
])
def test_malformed_cache_entry_is_a_miss_not_a_crash(tmp_path, bad):
    """2.9.0: KeyError['payload'] / ValueError killed ask() on a corrupt cache.

    The cache is an optimisation. A hit that cannot be trusted can only ever
    cost a recomputation.
    """
    probe = _probe("row_count", ["how many rows"], fn=lambda **k: [{"n": 0}])
    r = _router(tmp_path, [probe])
    entry = dict(bad)
    entry.setdefault("menu_version", r.menu_version)
    (tmp_path / "ask_cache.json").write_text(json.dumps({"anykey": entry}), encoding="utf-8")

    env = r.ask("how many rows", use_cache=True)
    assert env["probe"] == "row_count"
    assert env["results"] == [{"n": 0}]


def test_cache_file_of_wrong_top_level_type_is_a_miss(tmp_path):
    """A JSON *list* (or a bare scalar) at the top level used to raise
    `AttributeError: 'list' object has no attribute 'get'`."""
    r = _router(tmp_path, [_probe("row_count", ["how many rows"])])
    (tmp_path / "ask_cache.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert r.ask("how many rows")["probe"] == "row_count"


def test_cache_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    """A plain open('w') truncates before writing, so a kill mid-write left
    invalid JSON behind. Write to a temp sibling and os.replace() instead."""
    r = _router(tmp_path, [_probe("row_count", ["how many rows"])])
    r.ask("how many rows")
    r.ask("how many rows")

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    cached = json.loads((tmp_path / "ask_cache.json").read_text(encoding="utf-8"))
    assert cached and all("payload" in e for e in cached.values())


def test_cache_round_trip_still_serves_the_second_ask(tmp_path):
    """The guards must not disable caching itself."""
    calls = {"n": 0}

    def counter(**_):
        calls["n"] += 1
        return [{"n": calls["n"]}]

    r = _router(tmp_path, [_probe("row_count", ["how many rows"], fn=counter)])
    first = r.ask("how many rows")
    second = r.ask("how many rows")
    assert second["results"] == first["results"] == [{"n": 1}]
    assert calls["n"] == 1


# ── caged args: no quiet wrong answers ─────────────────────────────────────

def test_enum_value_match_prefers_the_longest_value(tmp_path):
    """'Rock' shadows 'Rock and Roll' when matching is first-wins: the menu
    answered a Rock and Roll question with genre=Rock."""
    args = {"genre": {"type": "enum", "values": ["Rock", "Rock and Roll"]}}
    probe = _probe("by_genre", ["albums by genre"], args=args,
                   fn=lambda **kw: [dict(kw)])
    r = _router(tmp_path, [probe])

    assert r.ask("how many albums by Rock and Roll are there")[
        "results"][0]["genre"] == "Rock and Roll"
    assert r.ask("how many albums by Rock are there")[
        "results"][0]["genre"] == "Rock"


def test_enum_short_value_still_matches_alone(tmp_path):
    """Longest-match must not require the long value to be present."""
    args = {"genre": {"type": "enum", "values": ["Rock and Roll", "Jazz"]}}
    probe = _probe("by_genre", ["albums by genre"], args=args,
                   fn=lambda **kw: [dict(kw)])
    assert _router(tmp_path, [probe]).ask(
        "how many albums by Jazz are there")["results"][0]["genre"] == "Jazz"


def test_thousands_separator_is_parsed_not_truncated(tmp_path):
    """'top 1,000 customers' used to match the '1' and answer limit=1 — a
    confident answer to a question nobody asked."""
    args = {"limit": {"type": "integer", "min": 1, "max": 25, "default": 10}}
    probe = _probe("top_customers", ["top customers"], args=args,
                   fn=lambda **kw: [dict(kw)])
    r = _router(tmp_path, [probe])

    with pytest.raises(NoResults) as exc:
        r.ask("show the top 1,000 customers")
    assert exc.value.envelope["refusal_reason"] == "out_of_range"


def test_thousands_separator_within_range_is_kept(tmp_path):
    args = {"limit": {"type": "integer", "min": 1, "max": 2000, "default": 10}}
    probe = _probe("top_customers", ["top customers"], args=args,
                   fn=lambda **kw: [dict(kw)])
    assert _router(tmp_path, [probe]).ask(
        "top 1,000 customers")["results"][0]["limit"] == 1000


def test_negative_number_is_not_read_as_positive(tmp_path):
    """'last -5 days' was parsed as 5 — the sign silently dropped, so the
    question that was asked and the question that was answered differed."""
    args = {"days": {"type": "integer", "min": 1, "max": 365, "default": 7}}
    probe = _probe("recent", ["recent activity"], args=args,
                   fn=lambda **kw: [dict(kw)])
    r = _router(tmp_path, [probe])

    with pytest.raises(NoResults) as exc:
        r.ask("recent activity in the last -5 days")
    assert exc.value.envelope["refusal_reason"] == "out_of_range"


def test_number_glued_to_a_date_is_not_an_argument(tmp_path):
    """The new integer pattern must not eat the day out of '2026-10-31'."""
    args = {"limit": {"type": "integer", "min": 1, "max": 25, "default": 3}}
    probe = _probe("top_customers", ["top customers"], args=args,
                   fn=lambda **kw: [dict(kw)])
    env = _router(tmp_path, [probe]).ask("top customers on 2026-10-31")
    assert env["results"][0]["limit"] == 3     # default, not 2026 and not 31


# ── API contract ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, 42, b"how many rows", ["q"]])
def test_ask_rejects_a_non_string_question_clearly(tmp_path, bad):
    """ask(None) raised AttributeError from deep inside normalisation."""
    r = _router(tmp_path, [_probe("row_count", ["how many rows"])])
    with pytest.raises(TypeError) as exc:
        r.ask(bad)
    assert "question must be a str" in str(exc.value)


def test_duplicate_probe_names_are_rejected(tmp_path):
    """Two probes sharing a name: the menu showed both, `by_name`, chains and
    the OpenAPI paths silently resolved to the last one."""
    a = _probe("row_count", ["how many rows"])
    b = _probe("row_count", ["how many records"])
    with pytest.raises(ValueError) as exc:
        _router(tmp_path, [a, b])
    assert "duplicate probe name" in str(exc.value)


def test_openapi_enum_type_is_a_json_schema_type(tmp_path):
    """`type: enum` is not a JSON-Schema type; generated clients reject it.
    The constraint list stays under both spellings (existing contract)."""
    args = {"genre": {"type": "enum", "values": ["Rock", "Jazz"]}}
    probe = _probe("by_genre", ["albums by genre"], args=args)
    props = _router(tmp_path, [probe]).openapi()["paths"]["/probes/by_genre"][
        "post"]["requestBody"]["content"]["application/json"]["schema"][
        "properties"]["genre"]
    assert props["type"] == "string"
    assert props["enum"] == props["values"] == ["Rock", "Jazz"]


# ── masking: a result must never break the audit, a secret must not leak ───

def test_self_referential_results_do_not_crash_the_ask(tmp_path):
    """A cyclic dict raised RecursionError inside the audit serialisation and
    killed an otherwise successful answer."""
    cyc = {"n": 1}
    cyc["self"] = cyc
    lst = [1, 2]
    lst.append(lst)

    probe = _probe("weird", ["weird rows"], fn=lambda **k: [cyc, lst])
    env = _router(tmp_path, [probe]).ask("weird rows")
    assert env["probe"] == "weird"
    assert "<cycle>" in json.dumps(env)              # bounded, not infinite
    assert (tmp_path / "ask_audit.jsonl").exists()


def test_non_string_value_under_a_secret_key_is_masked():
    """`{'password': 12345678}` went to disk and to the client in clear: the
    key hint was applied only to `str` values. A numeric PIN is no less
    secret than a textual password."""
    masked = mask_pii({"password": 12345678, "pin": 4321, "token": 9.99,
                       "notes": "plain", "ok": True, "none": None})
    assert masked["password"] == masked["pin"] == masked["token"] == MASKED
    assert masked["notes"] == "plain"
    assert masked["ok"] is True and masked["none"] is None


def test_secret_container_under_a_secret_key_is_masked_inside():
    """A dict under a secret key must not be a hole in the mask."""
    masked = mask_pii({"password": {"hint": "my dog", "value": "hunter2"}})
    assert masked["password"]["value"] == MASKED


def test_masking_does_not_touch_numbers_in_a_normal_key():
    masked = mask_pii({"count": 12345678, "amount": 9.99})
    assert masked == {"count": 12345678, "amount": 9.99}


# ── oracle: an unusable shape is a failed check, not a traceback ───────────

@pytest.mark.parametrize("shape", [{"type": "integer"}, 42, ["count"]])
def test_oracle_with_a_non_string_shape_returns_false(shape):
    assert oracles.check_answer(shape, 5, 5) is False


def test_oracle_still_accepts_the_documented_shapes():
    assert oracles.check_answer("count", 5, 5) is True
    assert oracles.check_answer("count", 5, 6) is False
    assert oracles.check_answer("percent", 50.0, 50.0) is True
    assert oracles.check_answer("percent", 50.0, 150.0) is False
    assert oracles.check_answer("count", math.nan, math.nan) is False


# ── politeness must not cost an answer (false negative) ────────────────────

@pytest.mark.parametrize("question", [
    "kindly show open incidents",
    "open incidents thanks",
    "pls count the rows",
    "asap total sales",
    "cheers how many rows",
])
def test_politeness_filler_does_not_cause_a_refusal(tmp_path, question):
    """2.9.0 refused 'kindly how many records': 'kindly' was an unknown domain
    noun, so the FP guard fired on a word that carries no subject at all."""
    probe = _probe("row_count", ["how many rows", "count the rows", "total sales",
                                 "open incidents"], fn=lambda **k: [{"n": 1}])
    assert _router(tmp_path, [probe]).ask(question)["probe"] == "row_count"


@pytest.mark.parametrize("question", [
    "how many work orders are blocked",
    "how many rows today",
])
def test_temporal_qualifiers_still_refuse(tmp_path, question):
    """The filler list must not swallow QUALIFIERS: a probe that cannot honour
    "today"/"blocked" must refuse rather than answer an unqualified question."""
    probe = _probe("row_count", ["how many rows"], fn=lambda **k: [{"n": 1}])
    r = _router(tmp_path, [probe])
    if "blocked" in question:
        probe2 = _probe("open_work_orders", ["how many work orders"],
                        fn=lambda **k: [{"n": 2}])
        r = _router(tmp_path, [probe, probe2])
        question = "how many work orders are blocked"
    with pytest.raises(NoResults) as exc:
        r.ask(question)
    assert exc.value.envelope["refusal_reason"] in (
        "dropped_filter", "no_probe_matches")


# ── the sweep itself: routing precision/recall on the shared fixture ──────

def test_fp_fn_sweep_passes_on_the_shared_fixture():
    """The shared conformance fixture must be 100% routable AND 100% refusing.

    Runs the same sweep CI runs, so a menu or gate change that trades a false
    positive for a false negative (or vice versa) fails here, not in review.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import fp_fn_sweep

    blob = fp_fn_sweep._load(fp_fn_sweep.DEFAULT_FIXTURE, None)
    report = fp_fn_sweep.sweep(blob, stress=1)
    assert report["trigger_recall"] == 1.0, report["findings"]
    assert report["labelled_answer_rate"] == 1.0, report["findings"]
    assert report["refusal_precision"] == 1.0, report["findings"]
    assert report["trigger_collisions"] == 0, report["findings"]
    assert report["unstable"] == 0, report["findings"]
    assert report["errors"] == 0, report["findings"]


def test_fp_fn_sweep_names_a_menu_collision_as_such():
    """Two menu entries claiming the same trigger is a MENU defect. The sweep
    must say 'trigger_collision' — calling it a router miss sends the reader to
    the wrong file."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import fp_fn_sweep

    blob = {
        "menu": [
            {"name": "sales_a", "description": "total sales",
             "triggers": ["total sales"], "parameters": {"properties": {}}},
            {"name": "sales_b", "description": "total sales",
             "triggers": ["total sales"], "parameters": {"properties": {}}},
        ],
        "cases": [],
        "contract": {},
    }
    report = fp_fn_sweep.sweep(blob)
    kinds = {f["type"] for f in report["findings"]}
    assert "trigger_collision" in kinds
    assert report["trigger_collisions"] >= 1
    assert report["passed"] is False


def test_generic_intent_words_are_stored_in_stem_form():
    """The gate compares STEMS, so a plural in the word list is dead text.

    "status", "exists" and "cheers" were listed in plural/surface form while
    the gate looked for "statu"/"exist"/"cheer" — the words they were meant to
    neutralise still triggered "unknown domain noun" refusals.
    """
    from neuralosd.router import GENERIC_INTENT, _GENERIC_INTENT_WORDS, tokens

    for word in _GENERIC_INTENT_WORDS.split():
        assert tokens(word) <= GENERIC_INTENT, f"{word!r} is inert in GENERIC_INTENT"


# ── results that JSON cannot encode must not 500 the service ───────────────

def test_non_json_result_types_are_coerced_in_the_envelope(tmp_path):
    """A money column arrives as Decimal, a DATE as date, a BLOB as bytes.

    The envelope used to leave those un-encodable: `/ask` answered 500 and the
    SDK's json parse raised, caused by an ordinary NUMERIC column.
    """
    import datetime
    import decimal

    probe = _probe("typed_rows", ["typed rows"], fn=lambda **k: [
        {"d": decimal.Decimal("19.99"), "t": datetime.date(2026, 1, 1),
         "b": b"hello", "blob": b"\xff\xfe", "s": {3, 1, 2}}])
    env = _router(tmp_path, [probe]).ask("typed rows")
    json.dumps(env)                                   # must not raise
    row = env["results"][0]
    assert row["d"] == "19.99"                        # exact, not 19.99 float
    assert row["t"] == "2026-01-01"
    assert row["b"] == "hello"
    assert row["blob"].startswith("base64:")
    assert row["s"] == [1, 2, 3]


def test_jsonable_is_cycle_safe_and_total(tmp_path):
    """Any object at all must survive: the contract is 'encodable', not 'primitive'."""
    from neuralosd.router import jsonable

    cyc = {}
    cyc["self"] = cyc
    blob = jsonable({"a": cyc, "f": object(), "n": float("nan")})
    assert blob["a"]["self"] == "<cycle>"
    assert isinstance(blob["f"], str)
    json.dumps(blob)


def test_cached_envelope_is_written_without_a_default_hook(tmp_path):
    """The cache writer must not rely on json.dump(default=…) — a silent
    exception there means the cache never persists on a Decimal-heavy instance."""
    probe = _probe("money", ["total money"], fn=lambda **k: [
        {"amount": __import__("decimal").Decimal("5.00")}])
    r = _router(tmp_path, [probe])
    r.ask("total money", use_cache=False)
    with open(r.cache_file, encoding="utf-8") as fh:
        assert json.load(fh)


# ── unrelated probes must not steal a trigger, and a probe must not be ─────
# ── charged for the word it is named after                          ─────

def _status_menu(tmp_path):
    """`breakdown` declares `status` as an ENUM; `distinct_status` is no-arg.

    This is the shape `neuralosd init` generates for any table with a status
    column, and it produced a false positive: "status list" was answered by
    `breakdown` (a count per status) instead of `distinct_status`, because the
    no-arg probe was charged UNCONSUMED_PENALTY for "ignoring" the word it is
    literally named after.
    """
    dim = {"type": "enum", "values": ["status", "region"], "required": True}
    measure = {"type": "enum", "values": ["amount"], "required": False}
    return [
        _probe("distinct_status", ["status list", "distinct status",
                                   "status values", "what status"],
               fn=lambda **k: [{"distinct": ["paid", "unpaid"]}]),
        _probe("distinct_region", ["region list", "distinct region",
                                   "region values", "what region"],
               fn=lambda **k: [{"distinct": ["north", "south"]}]),
        _probe("breakdown", ["breakdown", "break down", "status breakdown",
                             "breakdown by status", "region breakdown"],
               args={"dimension": dim, "measure": measure},
               fn=lambda **k: [{"by": k.get("dimension")}]),
    ]


def test_a_probe_is_not_penalised_for_its_own_subject_token(tmp_path):
    """A named FILTER the probe is NAMED after is consumed by identity.

    The rescue existed but compared a surface form ("status") against a STEM
    ("statu"), so it never fired for any plural-ending value and the penalty
    (12.0) was applied — the same stem/surface mismatch that killed the
    GENERIC_INTENT table.
    """
    r = _router(tmp_path, _status_menu(tmp_path))
    meta = next(m for m in r.metas if m.name == "distinct_status")
    assert r._unconsumed_for(meta, {}, "status list") == []
    assert r._unconsumed_for(meta, {}, "status values") == []


def test_exact_trigger_beats_an_arg_enum_coincidence(tmp_path):
    """`status` is an enum of breakdown's `dimension`, so breakdown earns
    ARG_MATCH_BONUS for merely containing the word. Its own trigger phrase must
    not lose to that coincidence."""
    r = _router(tmp_path, _status_menu(tmp_path))
    for q in ("status list", "list status", "status values"):
        assert r.ask(q, use_cache=False)["probe"] == "distinct_status", q
    # …while an explicit dimension + measure is still a breakdown.
    assert r.ask("status breakdown", use_cache=False)["probe"] == "breakdown"


def test_top_named_filter_is_still_reported_as_dropped(tmp_path):
    """The identity rescue must not blunt the real check: a filter the probe
    neither takes nor is named after is still a dropped filter."""
    r = _router(tmp_path, _status_menu(tmp_path))
    meta = next(m for m in r.metas if m.name == "distinct_status")
    assert r._unconsumed_for(meta, {}, "distinct status by region") == ["region"]


def test_generated_menu_has_no_preposition_only_triggers():
    """"by" and "per" are prepositions; as triggers they are a false-positive
    magnet that out-scores the probe that answers the question."""
    import inspect
    from neuralosd._cmd import init as init_mod

    src = inspect.getsource(init_mod)
    assert '"split by", "group by", "trend"' in src
    assert '"per", "by", "trend"' not in src


def test_sweep_calls_an_arg_gated_trigger_routed_not_missed(tmp_path):
    """A probe that identifies itself and then demands a required argument is
    RIGHT; scoring that as a false negative sends the reader to the router
    instead of the menu."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts"))
    import fp_fn_sweep as sweep_mod

    dim = {"type": "enum", "values": ["status", "region"], "required": True}
    probes = [_probe("breakdown", ["breakdown", "break down"],
                     args={"dimension": dim},
                     fn=lambda **k: [{"by": k.get("dimension")}])]
    blob = {"menu": [{"name": "breakdown", "triggers": ["breakdown"],
                      "description": "d",
                      "parameters": {"properties": {"dimension": {
                          "enum": ["status", "region"]}}}}],
            "cases": [], "contract": {}, "probes": probes}
    rep = sweep_mod.sweep(blob, stress=0, instance_mode=True)
    assert rep["arg_gated"] == 1
    assert rep["trigger_recall"] == 1.0
    assert rep["trigger_collisions"] == 0
    assert not [f for f in rep["findings"] if f["type"] == "trigger_collision"]
