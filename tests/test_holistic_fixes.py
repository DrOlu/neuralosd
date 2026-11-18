"""Pins for the v3.5.1 holistic-review fixes.

Each test reproduces one verified defect from the review: gate/arg extraction,
masking and cache replay, audit-ledger isolation, and API read-only/body
guards. No network.
"""
import json
import os
import sys
import threading
from decimal import Decimal

import pytest

from neuralosd import probe, enum_arg, int_arg, Router, NoResults
from neuralosd.router import _storage_mask, audit_ledger, mask_pii

sys.path.insert(0, os.path.dirname(__file__))
from test_api import _start, _req  # noqa: E402


def _router(tmp_path, probes, **kw):
    return Router(probes=probes, model_fallback=None,
                  cache_file=str(tmp_path / "cache.json"),
                  audit_file=str(tmp_path / "audit.jsonl"), **kw)


# ── argument extraction ──────────────────────────────────────────────────

def test_enum_values_bounded_by_non_word_characters_match(tmp_path):
    @probe(description="Framework", triggers=["written in"],
           args={"fw": enum_arg(["Java", ".NET"])})
    def framework(fw):
        return {"fw": fw}

    env = _router(tmp_path, [framework]).ask("written in .NET")
    assert env["results"][0]["fw"] == ".NET"


def test_an_optional_enum_is_not_replaced_by_the_first_value(tmp_path):
    calls = []

    @probe(description="Runtime", triggers=["written in"],
           args={"fw": {"type": "enum", "values": ["Java", ".NET"],
                        "required": False}})
    def runtime(fw=None):
        calls.append(fw)
        return {"fw": fw}

    # The subject "Cobol" is not in the menu: the gate must refuse, never
    # answer as if the optional enum's first value ("Java") had been named.
    with pytest.raises(NoResults) as exc:
        _router(tmp_path, [runtime]).ask("written in Cobol")
    assert exc.value.envelope["refusal_reason"] == "no_probe_matches"
    assert calls == []


def test_multiple_integer_args_are_matched_by_name(tmp_path):
    @probe(description="Orders for one user", triggers=["orders for user"],
           args={"uid": int_arg(1, 9999), "limit": int_arg(1, 100)})
    def orders(uid, limit):
        return {"uid": uid, "limit": limit}

    env = _router(tmp_path, [orders]).ask(
        "orders for user uid 42 limit 5")
    row = env["results"][0]
    assert (row["uid"], row["limit"]) == (42, 5)


def test_ambiguous_multiple_integers_refuse_instead_of_guessing(tmp_path):
    @probe(description="Orders for one user", triggers=["orders for user"],
           args={"uid": int_arg(1, 9999), "limit": int_arg(1, 100)})
    def orders(uid, limit):
        return {"uid": uid, "limit": limit}

    with pytest.raises(NoResults) as exc:
        _router(tmp_path, [orders]).ask("top 5 orders for user 42")
    assert exc.value.envelope["refusal_reason"] == "args_not_extractable"


def test_multi_word_qualifier_can_be_satisfied(tmp_path):
    @probe(description="Work orders on hold",
           triggers=["work orders on hold"])
    def on_hold():
        return {"on_hold": 7}

    env = _router(tmp_path, [on_hold]).ask(
        "how many work orders are on hold")
    assert env["results"][0]["on_hold"] == 7


def test_numeric_enum_values_do_not_crash_normalisation(tmp_path):
    @probe(description="Revenue by year", triggers=["revenue by year"],
           args={"year": {"type": "enum", "values": [2024, 2025],
                          "required": True}})
    def revenue(year):
        return {"year": year}

    env = _router(tmp_path, [revenue]).ask("revenue by year 2024")
    assert env["results"][0]["year"] == 2024


def test_chain_accepts_initial_args_and_probe_name_templates():
    from neuralosd.chains import ChainRunner

    calls = []

    def first(region):
        return {"region": region, "total": 7}

    def second(total):
        calls.append(total)
        return {"total": total}

    runner = ChainRunner({"first": first, "second": second})
    runner.register("c", [
        {"probe": "first", "args": {"region": "{{region}}"}},
        {"probe": "second", "args": {"total": "{{first.total}}"}},
    ])
    runner.run("c", {"region": "EMEA"})
    assert calls == ["7"]


# ── masking and cache ────────────────────────────────────────────────────

def test_secret_values_are_masked_whatever_their_type():
    from neuralosd.router import MASKED
    masked = mask_pii({"api_key": b"sk-live-secret",
                       "token": Decimal("998877")})
    assert masked == {"api_key": MASKED, "token": MASKED}


def test_secret_key_hints_match_tokens_not_substrings():
    cleared = mask_pii({"shipping": 12.5, "mapping_count": 4})
    assert cleared == {"shipping": 12.5, "mapping_count": 4}
    from neuralosd.router import MASKED
    assert mask_pii({"user_pin": 4321}) == {"user_pin": MASKED}


def test_pii_used_as_a_mapping_key_is_masked():
    from neuralosd.router import MASKED
    masked = mask_pii({"alice@example.com": 12})
    assert list(masked) == [MASKED]
    stored = _storage_mask({"alice@example.com": 12})
    assert list(stored) == [MASKED]


def test_pii_bearing_answers_are_not_cached(tmp_path):
    @probe(description="Customer email", triggers=["customer email"])
    def email():
        return {"email": "alice@example.com"}

    rt = _router(tmp_path, [email], pii_mask=False)
    first = rt.ask("customer email")
    second = rt.ask("customer email")
    assert first["results"] == second["results"]
    assert second["results"][0]["email"] == "alice@example.com"
    assert not (tmp_path / "cache.json").exists() or \
        "alice@example.com" not in (tmp_path / "cache.json").read_text(
            encoding="utf-8")


def test_cache_hit_reports_the_current_question_not_the_first(tmp_path):
    @probe(description="Row count", triggers=["row count", "how many rows"])
    def rows():
        return {"count": 3}

    rt = _router(tmp_path, [rows], pii_mask=False)
    rt.ask("row count")
    env = rt.ask("how many rows")
    assert env["question"] == "how many rows"
    assert env["normalized"] == "how many rows"


# ── audit-ledger isolation ───────────────────────────────────────────────

def test_selftest_ledger_is_scoped_to_the_thread(tmp_path):
    @probe(description="Row count", triggers=["row count"])
    def rows():
        return {"count": 3}

    user = tmp_path / "user.jsonl"
    selftest = tmp_path / "selftest.jsonl"
    rt = Router(probes=[rows], model_fallback=None,
                cache_file=str(tmp_path / "cache.json"),
                audit_file=str(user))

    with audit_ledger(str(selftest)):
        rt.ask("row count")

    thread = threading.Thread(target=lambda: rt.ask("row count"))
    thread.start()
    thread.join()

    selftest_rows = selftest.read_text(encoding="utf-8").splitlines()
    user_rows = user.read_text(encoding="utf-8").splitlines()
    assert len(selftest_rows) == 1
    assert len(user_rows) == 1


# ── HTTP API guards ──────────────────────────────────────────────────────

def test_read_only_refuses_hitl_confirmation(tmp_path):
    base, _d, _p, _s = _start(tmp_path, read_only=True)
    code, body = _req(base, "POST", "/v1/instances/fake/ask",
                      {"question": "submit payroll"})
    assert code == 409, body
    token = body["confirm_token"]
    assert _req(base, "POST", f"/v1/instances/fake/confirm/{token}",
                {})[0] == 403


def test_read_only_refuses_shadow_evaluate_and_synonyms(tmp_path):
    base, _d, _p, _s = _start(tmp_path, read_only=True)
    assert _req(base, "GET", "/v1/instances/fake/synonyms")[0] == 403
    assert _req(base, "POST",
                "/v1/instances/fake/shadow/metric/evaluate", {})[0] == 403


def test_chunked_body_is_refused_not_treated_as_empty(tmp_path):
    import socket
    from test_api import _free_port

    base, _d, plan_id, _s = _start(tmp_path)
    port = int(base.rsplit(":", 1)[1])
    body = json.dumps({"dry_run": True}).encode("utf-8")
    req = (f"POST /v1/instances/fake/plans/{plan_id}/apply HTTP/1.1\r\n"
           f"Host: 127.0.0.1:{port}\r\nAuthorization: Bearer testkey\r\n"
           "Transfer-Encoding: chunked\r\n"
           "Content-Type: application/json\r\n\r\n"
           f"{len(body):x}\r\n").encode() + body + b"\r\n0\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(req)
        status = sock.recv(64).split(b" ", 2)[1]
    assert status == b"411"


def test_openapi_declares_refusal_and_readiness_codes():
    from neuralosd.openapi_api import build_spec
    spec = build_spec()
    ask = spec["paths"]["/v1/instances/{instance}/ask"]["post"]
    assert "422" in ask["responses"]
    assert "503" in ask["responses"]
    assert "411" in ask["responses"]
    metrics = spec["paths"]["/v1/instances/{instance}/metrics"]["get"]
    assert "text/plain" in metrics["responses"]["200"]["content"]
    docs = spec["paths"]["/docs"]["get"]
    assert "text/html" in docs["responses"]["200"]["content"]
    legacy = spec["paths"]["/ask"]["post"]["security"]
    assert {} in legacy


# ── multi-instance serve auth ────────────────────────────────────────────

def test_async_run_capacity_is_bounded_and_refused():
    from neuralosd.agentic import RunCapacityError, RunStore

    gate = threading.Event()
    store = RunStore(max_workers=1)
    store.submit(lambda: gate.wait(5))
    with pytest.raises(RunCapacityError):
        store.submit(lambda: None)
    gate.set()


def test_multi_instance_serve_uses_api_key(monkeypatch):
    from types import SimpleNamespace
    from neuralosd._cmd import serve as cmd

    seen = {}
    monkeypatch.setattr("neuralosd.multi.MultiInstanceServer",
                        lambda instances_dir: object())
    monkeypatch.setattr("neuralosd.multi.serve_multi",
                        lambda server, host, port, auth_token:
                        seen.update(host=host, port=port,
                                    auth_token=auth_token))

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        a = SimpleNamespace(instances_dir=d, api_key="s3cret",
                            auth_token=None, api_key_file=None,
                            host="127.0.0.1", port=9999, strict=False)
        cmd.run_multi(a)
    assert seen["auth_token"] == "s3cret"
