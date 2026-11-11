"""F8 — every reason the gate can return is in the declared wire vocabulary.

`REFUSAL_REASONS` sits next to `GATE_VERSION` and is the vocabulary a mesh
peer is promised. `_gate_decide` has always been able to return
"below_floor", "ambiguous" and "comparative", but the set omitted all three:
a consumer that validated a refusal token against the set read a real gate
refusal as an unknown reason. These tests pin the declaration AND the audit
rows for each missing reason.
"""
import ast
import json
import textwrap

import pytest

from neuralosd import probe as probe_dec
from neuralosd.router import (GATE_VERSION, REFUSAL_REASONS, NoResults,
                              Router)


def _p(name, triggers, fn=None, args=None):
    fn = fn or (lambda **_kw: [{"ok": True}])
    fn.__name__ = name
    return probe_dec(description=name.replace("_", " "), triggers=triggers,
                     args=args or {}, name=name)(fn)


def _router(tmp_path, probes, **kw):
    return Router(probes=probes,
                  cache_file=str(tmp_path / ".ask_cache.json"),
                  audit_file=str(tmp_path / "ask_audit.jsonl"),
                  config_dir=str(tmp_path), **kw)


def _last_row(tmp_path):
    with open(tmp_path / "ask_audit.jsonl", encoding="utf-8") as fh:
        lines = [l for l in fh.read().splitlines() if l.strip()]
    assert lines, "no audit rows were written"
    return json.loads(lines[-1])


def test_every_reason_gate_decide_returns_is_declared():
    """AST-level guard against the F8 drift class: a new return literal in
    _gate_decide must land in REFUSAL_REASONS in the same commit."""
    src = textwrap.dedent(
        __import__("inspect").getsource(Router._gate_decide))
    tree = ast.parse(src)
    returned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            returned.add(node.value.value)
    assert returned, "AST scan found no string returns"
    assert returned <= REFUSAL_REASONS, (
        f"_gate_decide can return {sorted(returned - REFUSAL_REASONS)} but "
        f"REFUSAL_REASONS omits it — bump the declaration (and GATE_VERSION "
        f"if the SEMANTICS changed)")


@pytest.mark.parametrize("reason,probes,question,kw", [
    ("below_floor",
     [("total_sales", ["total sales"])],
     "total sales", {"floor": 99.0}),
    ("ambiguous",
     [("sales_a", ["sales report"]), ("sales_b", ["sales report"])],
     "sales report", {}),
    # the probe NAME must not contain a compare word: _vocab_tokens()
    # includes the name, and a probe that calls itself "compare" claims
    # comparisons by identity.
    ("comparative",
     [("revenue_trend", ["total revenue higher or lower"])],
     "total revenue higher or lower", {}),
])
def test_each_gate_reason_refuses_with_a_declared_token(
        tmp_path, reason, probes, question, kw):
    r = _router(tmp_path, [_p(n, t) for n, t in probes], **kw)
    assert r.model_fallback is None          # gate stands down, no model
    with pytest.raises(NoResults) as exc:
        r.ask(question)
    env = exc.value.envelope
    assert env["refusal_reason"] == reason, env
    assert env["mode"] == "refused"
    assert env["results"] is None
    row = _last_row(tmp_path)
    assert row["refusal_reason"] == reason
    assert row["refusal_reason"] in REFUSAL_REASONS
    assert row["mode"] == "refused"


def test_gate_vocabulary_constant_still_has_one_version():
    assert GATE_VERSION == 1
