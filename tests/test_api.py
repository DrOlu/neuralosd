"""First-class HTTP API: auth, discovery, ask, probes, plans, audit, limits.

The API is tested over a real socket against a fake instance (the documented
fallback when a live `plasma-estate` is not reachable). Every route in the
OpenAPI document is dispatched by the same `ROUTES` table, so a path cannot
exist in one and not the other.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from neuralosd.api import ApiState, serve_api
from neuralosd.actuate import actuate
from neuralosd._cmd._common import load_instance
from neuralosd.openapi_api import ROUTES, build_spec, route_paths

PROBES = '''\
import time
from neuralosd import probe


@probe(description="How many rows", triggers=["how many rows", "row count"])
def count_rows():
    return {"count": 3}


@probe(description="Total revenue", triggers=["total revenue"])
def total_revenue():
    return {"total": 100}


@probe(description="Rows by region", triggers=["rows by region"],
       args={"region": {"type": "enum", "values": ["EMEA", "APAC"]}})
def rows_by_region(region):
    return {"region": region, "count": 2}


@probe(description="Count of incidents", triggers=["count of incidents"])
def count_incidents():
    return {"count": 5}


@probe(description="Incidents by priority", triggers=["incidents by priority"])
def incidents_by_priority():
    return {"rows": [{"priority": "high", "count": 2}]}


@probe(description="List regions", triggers=["list regions"])
def list_regions():
    return {"regions": ["EMEA", "APAC"]}


@probe(description="Slow probe", triggers=["slow probe"])
def slow_probe():
    time.sleep(0.6)
    return {"ok": True}


@probe(description="Submit payroll", triggers=["submit payroll"],
       confirm=True)
def submit_payroll():
    return {"submitted": True}
'''

GOLDEN = {"items": [
    {"q": "how many rows", "expect_probe": "count_rows"},
    {"q": "row count", "expect_probe": "count_rows"},
    {"q": "total revenue", "expect_probe": "total_revenue"},
    {"q": "count of incidents", "expect_probe": "count_incidents"},
    {"q": "quantum entanglement statistics", "expect_refusal": True},
    {"q": "delete all rows", "expect_refusal": True},
    {"q": "total revenue closed", "expect_refusal": True},
]}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _instance(tmp_path):
    d = tmp_path / "fake"
    d.mkdir()
    (d / "probes.py").write_text(PROBES, encoding="utf-8")
    (d / "golden.json").write_text(json.dumps(GOLDEN), encoding="utf-8")
    inst = load_instance(str(d))
    inst.router.min_question_coverage = 0.9
    inst.router.strict_discards = True
    return d, inst


def _start(tmp_path, read_only=False, plan_ttl=86400, plan_ts=None,
           make_plan=True):
    d, inst = _instance(tmp_path)
    plan_id = None
    if make_plan:
        report = actuate("block_ip", "1.2.3.4", "reviewed", "ops", str(d),
                         side_effect=None, stub=True, ts=plan_ts)
        plan_id = report["plan_id"]
    state = ApiState("fake", str(d), inst)
    port = _free_port()
    thread = threading.Thread(
        target=serve_api,
        kwargs={"instance_dirs": [str(d)], "states": [state],
                "host": "127.0.0.1", "port": port,
                "api_keys": ["testkey"], "read_only": read_only,
                "plan_ttl": plan_ttl, "quiet": True},
        daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    else:                                             # pragma: no cover
        pytest.fail("API server did not start")
    return base, d, plan_id, state


def _req(base, method, path, body=None, key="testkey", headers=None,
         raw=False):
    hdrs = {"Content-Type": "application/json"}
    if key:
        hdrs["Authorization"] = "Bearer " + key
    hdrs.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            return resp.status, (data if raw else json.loads(data or b"{}"))
    except urllib.error.HTTPError as exc:
        data = exc.read()
        return exc.code, (data if raw else json.loads(data or b"{}"))


# ── auth ─────────────────────────────────────────────────────────────────

def test_auth_contract(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    assert _req(base, "GET", "/v1/instances", key=None)[0] == 401
    assert _req(base, "GET", "/v1/instances", key="wrong")[0] == 401
    assert _req(base, "GET", "/v1/instances")[0] == 200
    assert _req(base, "GET", "/v1/instances", key=None,
                headers={"X-API-Key": "testkey"})[0] == 200
    # a query-string key is never accepted
    assert _req(base, "GET", "/v1/instances?api_key=testkey",
                key=None)[0] == 401
    assert _req(base, "GET", "/healthz", key=None)[0] == 200


# ── read-only ────────────────────────────────────────────────────────────

def test_read_only_refuses_mutations_but_allows_dry_runs(tmp_path):
    base, _d, plan_id, _s = _start(tmp_path, read_only=True)
    assert _req(base, "POST", f"/v1/instances/fake/plans/{plan_id}/apply",
                {"reason": "x"})[0] == 403
    assert _req(base, "POST", f"/v1/instances/fake/plans/{plan_id}/undo",
                {"reason": "x"})[0] == 403
    assert _req(base, "POST", "/v1/instances/fake/reload", {})[0] == 403
    assert _req(base, "GET", "/v1/instances/fake/plans")[0] == 200
    code, body = _req(base, "POST",
                      f"/v1/instances/fake/plans/{plan_id}/apply",
                      {"reason": "x", "dry_run": True})
    assert code == 200
    assert body["would_apply"] is True


# ── discovery ────────────────────────────────────────────────────────────

def test_discovery_endpoints(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    for path in ("/v1/version", "/v1/instances", "/v1/instances/fake",
                 "/v1/instances/fake/health",
                 "/v1/instances/fake/capabilities",
                 "/v1/instances/fake/menu",
                 "/v1/instances/fake/menu/count_rows",
                 "/v1/instances/fake/gate",
                 "/v1/instances/fake/model",
                 "/v1/instances/fake/golden",
                 "/v1/instances/fake/config",
                 "/v1/instances/fake/stats"):
        code, body = _req(base, "GET", path)
        assert code == 200, (path, body)
    code, menu = _req(base, "GET", "/v1/instances/fake/menu")
    assert any(m["name"] == "count_rows" for m in menu["menu"])
    code, gate = _req(base, "GET", "/v1/instances/fake/gate")
    assert "no_probe_matches" in gate["reasons"]
    code, model = _req(base, "GET", "/v1/instances/fake/model")
    assert model["params"] == 121_000_000
    assert model["loaded"] is False


# ── ask / refusals ───────────────────────────────────────────────────────

def test_ask_answers_a_golden_question(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    code, body = _req(base, "POST", "/v1/instances/fake/ask",
                      {"question": "how many rows"})
    assert code == 200
    assert body["ok"] is True
    assert body["probe"] == "count_rows"
    assert body["results"][0]["count"] == 3
    assert body["gate_version"] == 1
    assert body["menu_version"]


def test_all_four_refusal_reasons(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    cases = [
        ("quantum entanglement statistics", "no_probe_matches"),
        ("delete all rows", "action_intent"),
        ("total revenue closed", "dropped_filter"),
        ("count of incidents in EMEA", "low_coverage"),
    ]
    for question, reason in cases:
        code, body = _req(base, "POST", "/v1/instances/fake/ask",
                          {"question": question})
        assert code == 422, (question, body)
        assert body["ok"] is False
        assert body["code"] == "refused"
        assert body["refusal_reason"] == reason, (question, body)


# ── probes ───────────────────────────────────────────────────────────────

def test_hitl_confirm_flow(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    code, body = _req(base, "POST", "/v1/instances/fake/ask",
                      {"question": "submit payroll"})
    assert code == 409
    token = body["confirm_token"]
    code, done = _req(base, "POST",
                      f"/v1/instances/fake/confirm/{token}", {})
    assert code == 200
    assert done["results"][0]["submitted"] is True
    assert done["mode"] == "confirmed"
    # single use
    code, _ = _req(base, "POST", f"/v1/instances/fake/confirm/{token}", {})
    assert code == 404


def test_probe_run_valid_invalid_unknown(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    code, body = _req(base, "POST",
                      "/v1/instances/fake/probes/count_rows/run", {"args": {}})
    assert code == 200
    assert body["probe"] == "count_rows"
    assert body["results"][0]["count"] == 3

    code, _ = _req(base, "POST",
                   "/v1/instances/fake/probes/rows_by_region/run", {"args": {}})
    assert code == 400
    code, _ = _req(base, "POST",
                   "/v1/instances/fake/probes/rows_by_region/run",
                   {"args": {"region": "NOPE"}})
    assert code == 400
    code, _ = _req(base, "POST",
                   "/v1/instances/fake/probes/nope/run", {"args": {}})
    assert code == 404


# ── plans ────────────────────────────────────────────────────────────────

def test_plan_lifecycle_and_replay(tmp_path):
    base, _d, plan_id, _s = _start(tmp_path)
    code, listing = _req(base, "GET", "/v1/instances/fake/plans")
    assert code == 200
    assert any(p["plan_id"] == plan_id for p in listing["plans"])
    code, plan = _req(base, "GET", f"/v1/instances/fake/plans/{plan_id}")
    assert code == 200 and plan["plan"]["state"] == "proposed"

    code, applied = _req(base, "POST",
                         f"/v1/instances/fake/plans/{plan_id}/apply",
                         {"reason": "approved"})
    assert code == 200 and applied["state"] == "applied"
    code, replay = _req(base, "POST",
                        f"/v1/instances/fake/plans/{plan_id}/apply",
                        {"reason": "approved"})
    assert code == 409

    code, undone = _req(base, "POST",
                        f"/v1/instances/fake/plans/{plan_id}/undo",
                        {"reason": "rollback"})
    assert code == 200 and undone["state"] == "undone"
    code, _ = _req(base, "POST",
                   f"/v1/instances/fake/plans/{plan_id}/apply",
                   {"reason": "again"})
    assert code == 409          # the API only applies a PROPOSED plan

    code, events = _req(base, "GET",
                        f"/v1/instances/fake/plans/{plan_id}/events")
    assert code == 200
    ops = [e.get("op") for e in events["events"]]
    assert ops == ["dry_run", "apply", "undo"]


def test_undo_a_proposed_plan_is_409(tmp_path):
    base, _d, plan_id, _s = _start(tmp_path)
    code, _ = _req(base, "POST", f"/v1/instances/fake/plans/{plan_id}/undo",
                   {"reason": "x"})
    assert code == 409


def test_expired_plan_is_410(tmp_path):
    base, _d, plan_id, _s = _start(tmp_path, plan_ttl=1,
                                   plan_ts=time.time() - 60)
    code, body = _req(base, "GET", f"/v1/instances/fake/plans/{plan_id}")
    assert code == 410
    assert body["code"] == "gone"
    code, _ = _req(base, "POST", f"/v1/instances/fake/plans/{plan_id}/apply",
                   {"reason": "x"})
    assert code == 410


def test_unknown_instance_and_plan_are_404(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    assert _req(base, "GET", "/v1/instances/nope/plans")[0] == 404
    assert _req(base, "GET", "/v1/instances/fake/plans/nope")[0] == 404


def test_idempotency_key_returns_the_same_apply(tmp_path):
    base, d, plan_id, _s = _start(tmp_path)
    headers = {"Idempotency-Key": "k-1"}
    code1, body1 = _req(base, "POST",
                        f"/v1/instances/fake/plans/{plan_id}/apply",
                        {"reason": "once"}, headers=headers)
    code2, body2 = _req(base, "POST",
                        f"/v1/instances/fake/plans/{plan_id}/apply",
                        {"reason": "once"}, headers=headers)
    assert (code1, code2) == (200, 200)
    assert body2.get("replayed") is True
    assert body1["plan_id"] == body2["plan_id"]
    rows = [json.loads(line) for line in
            (d / "actuation_journal.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
    assert sum(1 for r in rows if r.get("op") == "apply") == 1


def test_two_concurrent_applies_one_wins(tmp_path):
    base, _d, plan_id, _s = _start(tmp_path)
    results = []

    def apply():
        results.append(_req(base, "POST",
                            f"/v1/instances/fake/plans/{plan_id}/apply",
                            {"reason": "race"})[0])

    threads = [threading.Thread(target=apply) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == [200, 409]


# ── audit ────────────────────────────────────────────────────────────────

def test_audit_rows_have_no_key_and_no_raw_question(tmp_path):
    base, d, _p, _s = _start(tmp_path)
    _req(base, "POST", "/v1/instances/fake/ask",
         {"question": "how many rows"})
    rows = [json.loads(line) for line in
            (d / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert rows
    text = json.dumps(rows)
    assert "testkey" not in text
    assert "how many rows" not in text          # only its hash is stored
    assert rows[-1]["question_sha256"]
    assert rows[-1]["request_id"]

    code, verify = _req(base, "GET", "/v1/instances/fake/audit/verify")
    assert code == 200 and verify["ok"] is True and verify["chained"] is True
    code, event = _req(base, "GET",
                       f"/v1/instances/fake/audit/{rows[-1]['event_id']}")
    assert code == 200 and event["row"]["event_id"] == rows[-1]["event_id"]


def test_refusals_are_listed_without_the_raw_question(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    _req(base, "POST", "/v1/instances/fake/ask",
         {"question": "quantum entanglement statistics"})
    code, body = _req(base, "GET", "/v1/instances/fake/refusals")
    assert code == 200 and body["refusals"]
    assert body["refusals"][-1]["refusal_reason"] == "no_probe_matches"
    assert "quantum entanglement" not in json.dumps(body)


# ── limits ───────────────────────────────────────────────────────────────

def test_oversized_body_is_413(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    code, body = _req(base, "POST", "/v1/instances/fake/ask",
                      {"question": "x" * 1_100_000})
    assert code == 413


def test_timeout_is_504(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    code, body = _req(base, "POST", "/v1/instances/fake/ask",
                      {"question": "slow probe",
                       "options": {"timeout_ms": 50}})
    assert code == 504
    assert body["code"] == "timeout"


# ── maintenance allowlist ───────────────────────────────────────────────

def test_maintenance_reads(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    for path in ("/v1/instances/fake/lint",
                 "/v1/instances/fake/gaps",
                 "/v1/instances/fake/shadow",
                 "/v1/instances/fake/catalog",
                 "/v1/instances/fake/openapi",
                 "/v1/instances/fake/slo",
                 "/v1/instances/fake/snapshots",
                 "/v1/system/backends",
                 "/v1/system/doors"):
        code, body = _req(base, "GET", path)
        assert code == 200, (path, body)
    code, body = _req(base, "GET", "/v1/system/backends")
    assert {b["name"] for b in body["backends"]} == {"boxlite",
                                                     "microsandbox"}
    code, body = _req(base, "GET", "/v1/instances/fake/catalog")
    assert "count_rows" in body["catalog"]


def test_golden_run_and_gaps(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    code, body = _req(base, "POST", "/v1/instances/fake/golden/run", {})
    assert code == 200, body
    assert body["summary"]["wrong"] == 0
    assert body["summary"]["skipped"] == 0

    _req(base, "POST", "/v1/instances/fake/ask",
         {"question": "quantum entanglement statistics"})
    code, gaps = _req(base, "GET", "/v1/instances/fake/gaps")
    assert code == 200 and gaps["count"] >= 1
    assert gaps["gaps"][0]["reason"] == "no_probe_matches"


def test_shadow_and_mothball(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    code, body = _req(base, "GET", "/v1/instances/fake/shadow")
    assert code == 200 and body["shadow"]["shadowed"] == []

    code, body = _req(base, "POST", "/v1/instances/fake/mothball",
                      {"age_days": 0, "apply": False})
    assert code == 200
    assert "candidates" in body["mothball"]


def test_synonyms_snapshot_and_calibrate(tmp_path):
    base, d, _p, _s = _start(tmp_path)
    code, body = _req(base, "GET", "/v1/instances/fake/synonyms")
    assert code == 200
    code, body = _req(base, "POST", "/v1/instances/fake/snapshot",
                      {"label": "q1"})
    assert code == 200, body
    code, body = _req(base, "GET", "/v1/instances/fake/snapshots")
    assert any(s["label"] == "q1" for s in body["snapshots"])

    code, body = _req(base, "POST", "/v1/instances/fake/calibrate",
                      {"allow_no_traps": True})
    assert code == 200, body
    assert (d / "router.json").exists()


def test_read_only_refuses_maintenance_mutations(tmp_path):
    base, _d, plan_id, _s = _start(tmp_path, read_only=True)
    assert _req(base, "POST", "/v1/instances/fake/snapshot",
                {"label": "q1"})[0] == 403
    assert _req(base, "POST", "/v1/instances/fake/mothball",
                {"age_days": 0, "apply": True})[0] == 403
    assert _req(base, "POST", "/v1/instances/fake/calibrate", {})[0] == 403
    assert _req(base, "POST", "/v1/instances/fake/synonyms/apply", {})[0] == 403


class _FakeMesh:
    def discover(self, skill=""):
        return [{"agent_id": "peer-a", "skills": ["count_rows"],
                 "capabilities": [{"name": "count_rows",
                                   "description": "row counts",
                                   "triggers": ["how many rows"]}]}]


def test_mesh_is_gated_and_discover_works(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    assert _req(base, "POST", "/v1/instances/fake/mesh/discover",
                {"question": "how many rows"})[0] == 403

    mesh_root = tmp_path / "mesh"
    mesh_root.mkdir()
    d, inst = _instance(mesh_root)
    state = ApiState("mesh", str(d), inst)
    port = _free_port()
    threading.Thread(
        target=serve_api,
        kwargs={"instance_dirs": [str(d)], "states": [state],
                "host": "127.0.0.1", "port": port,
                "api_keys": ["testkey"], "allow_mesh": True,
                "mesh_transport": _FakeMesh(), "quiet": True},
        daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    code, body = _req(base, "POST", "/v1/instances/mesh/mesh/discover",
                      {"question": "how many rows"})
    assert code == 200
    assert body["peers"][0]["agent_id"] == "peer-a"
    code, body = _req(base, "POST", "/v1/instances/mesh/mesh/preflight",
                      {"question": "how many rows"})
    assert code == 200 and body["preflight"]["would_answer"] is True
    code, _ = _req(base, "POST", "/v1/instances/mesh/mesh/delegate",
                   {"peer": "ghost", "question": "how many rows"})
    assert code == 404


# ── OpenAPI ──────────────────────────────────────────────────────────────

def test_openapi_paths_match_routes_and_validate(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    code, spec = _req(base, "GET", "/openapi.json", key=None)
    assert code == 200
    assert spec["openapi"] == "3.1.0"
    assert set(spec["paths"]) == route_paths()
    assert set(spec["paths"]) == {r[1] for r in ROUTES}
    assert "bearerAuth" in spec["components"]["securitySchemes"]
    assert "apiKeyAuth" in spec["components"]["securitySchemes"]
    import hashlib
    assert spec["info"]["version"] == __import__("neuralosd").__version__

    try:
        from openapi_spec_validator import validate
    except ImportError:                                # pragma: no cover
        pytest.skip("openapi-spec-validator not installed")
    validate(spec)


def test_docs_and_generated_client_smoke(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    code, html = _req(base, "GET", "/docs", key=None, raw=True)
    assert code == 200 and b"<html" in html and b"cdn" not in html.lower()

    code, spec = _req(base, "GET", "/openapi.json", key=None)
    operations = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            operations[op["operationId"]] = (method, path)

    class Client:
        def __init__(self, base, spec, key):
            self.base, self.ops, self.key = base, {}, key
            for path, item in spec["paths"].items():
                for method, op in item.items():
                    self.ops[op["operationId"]] = (method.upper(), path)

        def call(self, operation_id, path_params=None, body=None):
            method, template = self.ops[operation_id]
            path = template
            for name, value in (path_params or {}).items():
                path = path.replace("{" + name + "}", str(value))
            headers = {"Authorization": "Bearer " + self.key,
                       "Content-Type": "application/json"}
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(self.base + path, data=data,
                                         headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")

    client = Client(base, spec, "testkey")
    assert client.call("get_healthz")[0] == 200
    assert client.call("get_v1_instances")[0] == 200
    assert "get_v1_instances" in operations


# ── legacy /ask compatibility ───────────────────────────────────────────

def test_legacy_ask_requires_auth_when_a_key_is_configured(tmp_path):
    base, _d, _p, _s = _start(tmp_path)      # explicit key: "testkey"
    code, body = _req(base, "POST", "/ask", {"question": "how many rows"})
    assert code == 200
    assert body["probe"] == "count_rows"    # raw envelope, no wrapper
    assert _req(base, "POST", "/ask", {"question": "how many rows"},
                key=None)[0] == 401


def test_legacy_ask_stays_open_on_loopback_with_no_configured_key(tmp_path):
    d, inst = _instance(tmp_path)
    state = ApiState("fake", str(d), inst)
    port = _free_port()
    threading.Thread(
        target=serve_api,
        kwargs={"instance_dirs": [str(d)], "states": [state],
                "host": "127.0.0.1", "port": port, "api_keys": None,
                "quiet": True},
        daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    code, body = _req(base, "POST", "/ask", {"question": "how many rows"},
                      key=None)
    assert code == 200 and body["probe"] == "count_rows"
    # the /v1 surface still requires the default lab key
    assert _req(base, "GET", "/v1/instances", key=None)[0] == 401


# ── multi-instance ──────────────────────────────────────────────────────

def test_multi_instance_routing(tmp_path):
    from neuralosd.api import ApiState

    states = []
    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        (d / "probes.py").write_text(PROBES, encoding="utf-8")
        (d / "golden.json").write_text(json.dumps(GOLDEN), encoding="utf-8")
        inst = load_instance(str(d))
        inst.router.min_question_coverage = 0.9
        states.append(ApiState(name, str(d), inst))
    port = _free_port()
    threading.Thread(
        target=serve_api,
        kwargs={"instance_dirs": [], "states": states, "host": "127.0.0.1",
                "port": port, "api_keys": ["testkey"], "quiet": True},
        daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    code, body = _req(base, "GET", "/v1/instances")
    assert code == 200
    assert {i["name"] for i in body["instances"]} == {"alpha", "beta"}
    assert _req(base, "POST", "/v1/instances/alpha/ask",
                {"question": "how many rows"})[0] == 200
    assert _req(base, "POST", "/v1/instances/beta/ask",
                {"question": "how many rows"})[0] == 200
    assert _req(base, "GET", "/v1/instances/gamma/plans")[0] == 404


# ── golden ───────────────────────────────────────────────────────────────

def test_all_seven_golden_cases_through_ask(tmp_path):
    base, _d, _p, _s = _start(tmp_path)
    for item in GOLDEN["items"]:
        code, body = _req(base, "POST", "/v1/instances/fake/ask",
                          {"question": item["q"]})
        if item.get("expect_refusal"):
            assert code == 422, item
            assert body.get("refusal_reason"), item
        else:
            assert code == 200, (item, body)
            assert body["probe"] == item["expect_probe"], item
            assert body["refusal_reason"] is None
