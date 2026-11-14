"""Verification bands: execution vs golden vs refused.

"Verified" used to be one undifferentiated word. Every envelope now says
which proof it carries: the probe ran (execution), the exact question is in
the human-written bank and its typed oracle agreed (golden), or nothing was
produced (refused). A bank whose expected value disagrees downgrades the
answer to execution and reports the failed oracle — it is never called
"golden".
"""
import json

from neuralosd._cmd._common import load_instance
from neuralosd.mcp import handle
from neuralosd.router import NoResults

PROBES = '''\
from neuralosd import probe


@probe(description="How many rows", triggers=["how many rows", "row count"])
def count_rows():
    return {"count": 3}
'''


def _instance(tmp_path, golden, truth=None):
    d = tmp_path / "inst"
    d.mkdir()
    (d / "probes.py").write_text(PROBES, encoding="utf-8")
    (d / "golden.json").write_text(json.dumps({"items": golden}),
                                   encoding="utf-8")
    if truth is not None:
        (d / "truth.json").write_text(json.dumps({"items": truth}),
                                      encoding="utf-8")
    return load_instance(str(d))


def test_golden_question_with_a_passing_oracle_is_golden(tmp_path):
    inst = _instance(tmp_path, [{
        "q": "how many rows", "expect_probe": "count_rows",
        "oracle": {"value": 3, "shape": "count"}}])
    env = inst.ask("how many rows")
    v = env["verification"]
    assert v["level"] == "golden"
    assert v["executed"] is True
    assert v["results_gate"] == "passed"
    assert v["golden"]["matched"] is True
    assert v["golden"]["oracle"] == "passed"


def test_non_golden_question_is_execution_only(tmp_path):
    inst = _instance(tmp_path, [{
        "q": "how many rows", "expect_probe": "count_rows"}])
    v = inst.ask("row count")["verification"]
    assert v["level"] == "execution"
    assert "golden" not in v


def test_a_refusal_is_its_own_band(tmp_path):
    inst = _instance(tmp_path, [])
    try:
        inst.ask("quantum entanglement statistics")
    except NoResults as exc:
        v = exc.envelope["verification"]
    else:                                             # pragma: no cover
        raise AssertionError("expected a refusal")
    assert v["level"] == "refused"
    assert v["executed"] is False
    assert v["results_gate"] == "refused"


def test_wrong_oracle_value_downgrades_and_reports_failure(tmp_path):
    inst = _instance(tmp_path, [{
        "q": "how many rows", "expect_probe": "count_rows",
        "oracle": {"value": 99, "shape": "count"}}])
    v = inst.ask("how many rows")["verification"]
    assert v["level"] == "execution"
    assert v["golden"]["matched"] is True
    assert v["golden"]["oracle"] == "failed"


def test_a_truth_bank_marks_the_oracle_as_available(tmp_path):
    inst = _instance(tmp_path, [{
        "q": "how many rows", "expect_probe": "count_rows"}],
        truth=[{"probe": "count_rows", "sql": "SELECT 3"}])
    v = inst.ask("how many rows")["verification"]
    assert v["oracle_available"] is True


def test_mcp_carries_the_verification_band(tmp_path):
    inst = _instance(tmp_path, [{
        "q": "how many rows", "expect_probe": "count_rows",
        "oracle": {"value": 3, "shape": "count"}}])
    result = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "ask",
                                "arguments": {"question": "how many rows"}}},
                    str(tmp_path / "inst"))["result"]
    assert result["structuredContent"]["verification"]["level"] == "golden"
