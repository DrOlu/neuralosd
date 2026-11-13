"""The MCP agent surface — guarded, discoverable, and honest about refusal.

The old server grew a second execution path: ``tools/call`` did
``fn(**args)``, so an agent got no grammar cage, no audit row, no PII mask,
no row cap, no refusal and no cache — and it crashed on any instance the
scaffolder actually produces (it expected ``instance.py`` +
``needle_menu.json``; ``init`` writes ``probes.py`` + ``bridge.py``).
These tests pin the replacement: every tool goes through the router, and
the discovery tools make a large menu callable without flooding the context.
"""
import json
import subprocess
import sys

import pytest

from neuralosd import __version__
from neuralosd.mcp import handle

PROBES = '''\
from neuralosd import probe


@probe(description="How many rows are in the table",
       triggers=["how many rows", "row count", "count rows"])
def count_rows():
    return {"count": 3}


@probe(description="Rows by status",
       triggers=["rows by status", "status breakdown"],
       args={"status": {"type": "enum", "values": ["open", "closed"]}})
def rows_by_status(status):
    return {"status": status, "count": 2}


@probe(description="A probe that prints while it runs",
       triggers=["noisy count", "noisy rows"])
def noisy_count():
    print("NOISE ON STDOUT")
    return {"count": 1}
'''


@pytest.fixture
def inst_dir(tmp_path):
    d = tmp_path / "inst"
    d.mkdir()
    (d / "probes.py").write_text(PROBES, encoding="utf-8")
    return d


def _call(inst_dir, name, arguments=None):
    req = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
           "params": {"name": name, "arguments": arguments or {}}}
    return handle(req, str(inst_dir))["result"]


def test_initialize_names_the_server_and_version(inst_dir):
    out = handle({"jsonrpc": "2.0", "id": 1,
                  "method": "initialize"}, str(inst_dir))
    assert out["result"]["serverInfo"]["name"] == "neuralosd"
    assert out["result"]["serverInfo"]["version"] == __version__
    assert "tools" in out["result"]["capabilities"]


def test_tools_list_is_the_agent_surface_not_a_dump(inst_dir):
    out = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 str(inst_dir))
    tools = out["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["ask", "search", "describe", "catalog", "explain",
                     "health"]
    # every probe is NOT dumped into the context; discovery goes through
    # search/describe, which is what keeps a 200-probe menu usable
    assert "count_rows" not in names
    by_name = {t["name"]: t for t in tools}
    assert by_name["ask"]["inputSchema"]["required"] == ["question"]
    assert by_name["ask"]["annotations"]["readOnlyHint"] is False
    assert by_name["search"]["annotations"]["readOnlyHint"] is True


def test_tools_list_works_on_a_scaffolded_instance(inst_dir):
    """Regression: the old server required instance.py + needle_menu.json,
    which `neuralosd init` does not write. The generated layout must work."""
    assert not (inst_dir / "instance.py").exists()
    assert not (inst_dir / "needle_menu.json").exists()
    out = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                 str(inst_dir))
    assert out["result"]["tools"]


def test_ask_goes_through_the_router_and_is_audited(inst_dir):
    result = _call(inst_dir, "ask", {"question": "how many rows",
                                     "agent_id": "planner",
                                     "session_id": "s-1"})
    assert result["isError"] is False
    env = result["structuredContent"]
    assert env["probe"] == "count_rows"
    assert env["results"][0]["count"] == 3
    assert env["mode"] == "deterministic"

    rows = [json.loads(line) for line in
            (inst_dir / "ask_audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert len(rows) == 1
    assert rows[0]["source"] == "mcp"
    assert rows[0]["probe"] == "count_rows"
    assert rows[0]["agent_id"] == "planner"
    assert rows[0]["session_id"] == "s-1"


def test_idempotent_ask_replays_without_a_second_execution(inst_dir):
    args = {"question": "how many rows", "agent_id": "a1",
            "session_id": "s1", "idempotency_key": "k1"}
    first = _call(inst_dir, "ask", args)["structuredContent"]
    second = _call(inst_dir, "ask", args)["structuredContent"]
    assert second["_meta"]["replayed"] is True
    assert second["ask_id"] == first["ask_id"]

    rows = [json.loads(line) for line in
            (inst_dir / "ask_audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert len(rows) == 1               # the replay did not execute again

    third = _call(inst_dir, "ask", {**args, "idempotency_key": "k2"})
    assert third["structuredContent"]["_meta"].get("replayed") is not True
    rows = [json.loads(line) for line in
            (inst_dir / "ask_audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert len(rows) == 2


def test_run_id_is_stamped_on_the_audit_row(inst_dir):
    _call(inst_dir, "ask", {"question": "how many rows", "run_id": "r-9"})
    row = json.loads(
        (inst_dir / "ask_audit.jsonl").read_text(encoding="utf-8").strip())
    assert row["run_id"] == "r-9"


def test_refusal_is_a_result_with_a_machine_readable_contract(inst_dir):
    result = _call(inst_dir, "ask",
                   {"question": "what is the airspeed velocity of a swallow"})
    assert result["isError"] is False          # a refusal is an answer
    env = result["structuredContent"]
    assert env["mode"] == "refused"
    assert env["results"] is None
    assert env["refusal"]["code"] == "no_probe_matches"
    assert env["refusal"]["retryable"] is True
    assert env["refusal"]["suggested_action"]
    assert isinstance(env["refusal"]["candidates"], list)


def test_action_intent_is_reported_as_a_hard_no(inst_dir):
    result = _call(inst_dir, "ask", {"question": "delete all rows"})
    env = result["structuredContent"]
    assert env["mode"] == "refused"
    assert env["refusal"]["code"] == "action_intent"
    assert env["refusal"]["retryable"] is False


def test_search_ranks_probes_without_executing(inst_dir):
    result = _call(inst_dir, "search", {"query": "how many rows", "top_k": 3})
    payload = result["structuredContent"]
    assert result["isError"] is False
    assert payload["results"]
    assert payload["results"][0]["name"] == "count_rows"
    assert payload["results"][0]["score"] > 0
    assert payload["results"][0]["triggers"]
    # search must not write an audit row (nothing was executed)
    assert not (inst_dir / "ask_audit.jsonl").exists()


def test_describe_returns_the_schema(inst_dir):
    result = _call(inst_dir, "describe", {"name": "rows_by_status"})
    payload = result["structuredContent"]
    assert payload["description"] == "Rows by status"
    assert payload["parameters"]["properties"]["status"]["enum"] == [
        "open", "closed"]
    assert payload["triggers"]
    assert payload["pii"] == []
    assert len(payload["tool_version"]) == 12


def test_ask_labels_results_untrusted_with_provenance(inst_dir):
    env = _call(inst_dir, "ask", {"question": "how many rows"})[
        "structuredContent"]
    assert env["content_trust"] == "untrusted"
    assert env["provenance"]["instance"] == "inst"
    assert env["provenance"]["probe"] == "count_rows"
    assert env["provenance"]["ask_id"]


def test_describe_unknown_probe_is_a_tool_error(inst_dir):
    result = _call(inst_dir, "describe", {"name": "nope"})
    assert result["isError"] is True
    assert "unknown probe" in result["structuredContent"]["error"]


def test_catalog_returns_markdown(inst_dir):
    result = _call(inst_dir, "catalog")
    text = result["content"][0]["text"]
    assert text.startswith("# inst")
    assert "count_rows" in text
    assert result["isError"] is False


def test_explain_dry_runs_without_auditing(inst_dir):
    result = _call(inst_dir, "explain", {"question": "how many rows"})
    payload = result["structuredContent"]
    assert payload["would_answer"] is True
    assert payload["probe"] == "count_rows"
    assert not (inst_dir / "ask_audit.jsonl").exists()


def test_health(inst_dir):
    payload = _call(inst_dir, "health")["structuredContent"]
    assert payload["ok"] is True
    assert payload["probes"] == 3
    assert payload["menu_version"]


def test_unknown_tool_is_an_error_not_a_crash(inst_dir):
    result = _call(inst_dir, "nope", {})
    assert result["isError"] is True
    assert "unknown tool" in result["structuredContent"]["error"]


def test_bad_arguments_are_an_error(inst_dir):
    result = _call(inst_dir, "ask", {"question": ""})
    assert result["isError"] is True
    assert "question" in result["structuredContent"]["error"]


def test_probe_prints_do_not_corrupt_the_protocol_stream(inst_dir, capsys):
    result = _call(inst_dir, "ask", {"question": "noisy count"})
    assert result["structuredContent"]["results"][0]["count"] == 1
    captured = capsys.readouterr()
    assert "NOISE ON STDOUT" not in captured.out
    assert "NOISE ON STDOUT" in captured.err


def test_stdio_end_to_end(inst_dir):
    """A real host writes JSON lines; stdout must be JSON lines only."""
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "ask",
                               "arguments": {"question": "how many rows"}}}),
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "neuralosd.mcp", "--dir", str(inst_dir)],
        input="\n".join(lines) + "\n", capture_output=True, text=True,
        timeout=120)
    frames = [json.loads(line) for line in proc.stdout.splitlines()
              if line.strip()]
    assert [f["id"] for f in frames] == [1, 2, 3]
    assert frames[2]["result"]["structuredContent"]["probe"] == "count_rows"
    assert "NOISE" not in proc.stdout
