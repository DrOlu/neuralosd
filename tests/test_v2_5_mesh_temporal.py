"""Tests-first: v2.5.0 — synapse-gateway (agent2agent, for real this time)
and Temporal (durable execution), both out of the box.

Synapse bugs fixed here (the previous client invented its endpoints):
  - the real gateway speaks /api/mesh/* with a Bearer token, default port
    3000 — not /discover + /request on 8090
  - the token comes from REACTORPRO_GATEWAY_TOKEN or the gateway.env file
    (LIVEAGENT_GATEWAY_TOKEN=) — never baked into instance files
  - dispatch replies are read payload.text first, then payload.output;
    error envelopes carry the mesh error code, not a generic message
  - durable agent2agent work = /api/mesh/tasks (idempotent caller-minted
    taskId, refresh, cancel, retry) + the durable mailbox

Temporal: the loop's recurring work (golden bank, gap mining, shadow
evaluation, drift audit) becomes DURABLE ACTIVITIES — plain functions,
unit-testable with no Temporal server — plus an honest `--setup` and a
schedule creator that shells to the Temporal CLI.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from neuralosd._cmd._common import load_instance

CSV = "region,units\nNorth,100\nSouth,200\nEast,50\n"


def _build(tmp_path, name="inst"):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    src = tmp_path / "s.csv"
    src.write_text(CSV, encoding="utf-8")
    out = str(tmp_path / name)
    os.makedirs(out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=str(src), out=out, name=name,
                                 packages=None, extra_sources=None,
                                 join_on=None, since=None, header=None,
                                 secret_ref=None, secret_header=None))
    return out


# ── synapse-gateway: the real wire ────────────────────────────────────────

def _transport(tmp_path, monkeypatch, captured, responses=None,
               env_file=None):
    from neuralosd import synapse

    def fake_http(url, headers=None, data=None, timeout=30.0, method=None):
        captured.append({"url": url, "headers": dict(headers or {}),
                         "data": json.loads(data) if data else None,
                         "method": method})
        key = (url.split("/api")[-1] if "/api" in url else url)
        for pattern, resp in (responses or {}).items():
            if pattern in url:
                if isinstance(resp, Exception):
                    raise resp
                return json.dumps(resp).encode()
        return json.dumps({}).encode()

    monkeypatch.setattr(synapse, "_http", fake_http)
    env_path = None
    if env_file is not None:
        env_path = str(tmp_path / "gateway.env")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write(env_file)
    t = synapse.SynapseHTTP(gateway="http://127.0.0.1:3000",
                            token_path=env_path)
    return t


def test_token_from_env_wins_over_gateway_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("REACTORPRO_GATEWAY_TOKEN", "env-token")
    t = _transport(tmp_path, monkeypatch, [],
                   env_file="LIVEAGENT_GATEWAY_TOKEN=file-token")
    assert t.token == "env-token"


def test_token_falls_back_to_the_gateway_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("REACTORPRO_GATEWAY_TOKEN", raising=False)
    t = _transport(tmp_path, monkeypatch, [],
                   env_file='LIVEAGENT_GATEWAY_TOKEN="file-token"\n')
    assert t.token == "file-token"


def test_agents_hits_the_real_phonebook_endpoint(tmp_path, monkeypatch):
    captured = []
    t = _transport(tmp_path, monkeypatch, captured, token_env := None,
                   env_file="LIVEAGENT_GATEWAY_TOKEN=tok") \
        if False else _transport(tmp_path, monkeypatch, captured,
                                 env_file="LIVEAGENT_GATEWAY_TOKEN=tok")
    peers = t.agents()
    req = captured[-1]
    assert req["url"].endswith("/api/mesh/agents")
    assert req["headers"].get("Authorization") == "Bearer tok"
    assert isinstance(peers, (list, dict))


def test_dispatch_speaks_the_invoke_skill_and_reads_the_reply(tmp_path,
                                                              monkeypatch):
    captured = []
    t = _transport(tmp_path, monkeypatch, captured, responses={
        "/api/mesh/dispatch":
            {"v": "1.0", "type": "respond", "from": "grip-001",
             "payload": {"text": "Build 2214 is green"}}})
    reply = t.dispatch("grip-001", "Summarise the build", timeout_ms=120000)
    body = captured[-1]["data"]
    assert body["target"] == "grip-001"
    assert body["skill"] == "invoke"
    assert body["input"]["text"] == "Summarise the build"
    assert body["timeoutMs"] == 120000
    assert reply["text"] == "Build 2214 is green"


def test_dispatch_prefers_output_when_text_is_absent(tmp_path, monkeypatch):
    t = _transport(tmp_path, monkeypatch, [], responses={
        "/api/mesh/dispatch":
            {"payload": {"output": {"result": "42"}}}})
    reply = t.dispatch("edge-1", "compute")
    assert reply["text"] == "42"            # ReactorPro edges wrap as output


def test_dispatch_error_envelopes_carry_the_mesh_code(tmp_path, monkeypatch):
    from neuralosd.synapse import SynapseError
    t = _transport(tmp_path, monkeypatch, [], responses={
        "/api/mesh/dispatch":
            {"error": {"code": 3001, "message": 'Skill "x" not found',
                       "retryable": False}}})
    with pytest.raises(SynapseError) as e:
        t.dispatch("peer", "do x")
    assert "3001" in str(e.value)


def test_durable_tasks_are_idempotent_and_refreshable(tmp_path, monkeypatch):
    captured = []
    t = _transport(tmp_path, monkeypatch, captured, responses={
        "/api/mesh/tasks/t-1":
            {"taskId": "t-1", "state": "completed",
             "result": {"payload": {"text": "done"}}}})
    t.create_task("grip-001", "long job", task_id="t-1")
    body = captured[-1]["data"]
    assert body["taskId"] == "t-1"          # caller-minted = idempotency key
    assert body["target"] == "grip-001"

    t.get_task("t-1", refresh=True)
    assert "/api/mesh/tasks/t-1" in captured[-1]["url"]
    assert "refresh=true" in captured[-1]["url"]

    t.cancel_task("t-1")
    assert captured[-1]["url"].endswith("/api/mesh/tasks/t-1/cancel")


def test_mailbox_is_fire_and_forget(tmp_path, monkeypatch):
    captured = []
    t = _transport(tmp_path, monkeypatch, captured, responses={
        "/api/mesh/mailbox": {"accepted": True, "sequence": 41}})
    out = t.mailbox("agentspan-001", skill="status")
    assert out["accepted"] is True
    body = captured[-1]["data"]
    assert body["target"] == "agentspan-001"
    assert body["skill"] == "status"


def test_mesh_setup_reports_every_failure_honestly(tmp_path, monkeypatch,
                                                   capsys):
    from neuralosd._cmd import mesh as mesh_cmd
    monkeypatch.delenv("REACTORPRO_GATEWAY_TOKEN", raising=False)

    class A:  # unreachable gateway
        mesh_cmd_name = None
        gateway = "http://127.0.0.1:3000"
        timeout = 2.0
        token = None
        token_path = str(tmp_path / "missing.env")
        setup = True

    rc = mesh_cmd.run(A)
    assert rc == 1
    out = capsys.readouterr().out + capsys.readouterr().err
    blob = out
    assert "REACTORPRO_GATEWAY_TOKEN" in blob or "gateway.env" in blob or \
        "unreachable" in blob.lower()


# ── Temporal: durable execution ───────────────────────────────────────────

def test_activities_are_plain_jsonable_functions(tmp_path):
    from neuralosd.durable import ACTIVITIES, run_activity
    out = _build(tmp_path)
    assert "golden_run" in ACTIVITIES and "gaps_mine" in ACTIVITIES
    result = run_activity("golden_run", out)
    assert isinstance(result, dict) and "wrong" in result
    result = run_activity("gaps_mine", out)
    assert isinstance(result, dict)


def test_run_activity_rejects_unknown_names(tmp_path):
    from neuralosd.durable import run_activity
    with pytest.raises(SystemExit) as e:
        run_activity("does_not_exist", str(tmp_path))
    assert "golden_run" in str(e.value)     # the error lists what exists


def test_temporal_setup_without_the_cli_names_the_install(tmp_path,
                                                          monkeypatch):
    from neuralosd import durable
    monkeypatch.setattr(durable, "_which", lambda name: None)
    with pytest.raises(SystemExit) as e:
        durable.setup(which_fn=lambda n: None, runner=lambda cmd, timeout=30:
                      "", has_temporalio=False)
    blob = str(e.value)
    assert "temporal" in blob.lower()
    assert "install" in blob.lower()


def test_temporal_setup_with_cli_but_no_server_suggests_start_dev():
    from neuralosd import durable

    def fake_runner(cmd, timeout=30):
        raise SystemExit("namespace describe failed")

    with pytest.raises(SystemExit) as e:
        durable.setup(which_fn=lambda n: "/usr/local/bin/" + n,
                      runner=fake_runner, has_temporalio=True)
    assert "start-dev" in str(e.value)


def test_temporal_setup_green_when_everything_is_present():
    from neuralosd import durable
    report = durable.setup(which_fn=lambda n: "/usr/local/bin/" + n,
                           runner=lambda cmd, timeout=30: "default active",
                           has_temporalio=True)
    assert report["cli"] is True
    assert report["server"] is True
    assert report["temporalio"] is True


def test_schedule_create_builds_the_exact_cli_call(tmp_path):
    from neuralosd.durable import schedule_create_args
    args = schedule_create_args(
        schedule_id="neuralos-nightly", cron="0 2 * * *",
        task_queue="neuralos", workflow_type="neuralos_loop")
    assert args[:2] == ["schedule", "create"]
    assert "--schedule-id" in args and "neuralos-nightly" in args
    assert "--cron" in args and "0 2 * * *" in args
    assert "--task-queue" in args and "neuralos" in args
    assert "--workflow-type" in args and "neuralos_loop" in args


def test_worker_without_temporalio_is_an_honest_error(tmp_path):
    from neuralosd import durable
    if durable.temporalio_available():
        pytest.skip("temporalio installed — the honest-error path is "
                    "unreachable here")
    with pytest.raises(SystemExit) as e:
        durable.build_worker(str(tmp_path), task_queue="neuralos")
    assert "pip install temporalio" in str(e.value)


def test_shadow_and_drift_are_durable_activities_too(tmp_path):
    from neuralosd.durable import ACTIVITIES
    assert "shadow_evaluate" in ACTIVITIES
    assert "drift_audit" in ACTIVITIES
    assert "snapshot_pin" in ACTIVITIES
