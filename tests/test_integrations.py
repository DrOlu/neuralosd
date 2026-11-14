"""Connector integrations: mitmproxy HAR, rea evidence bundles, Plasma.

Each source is a capture/export read at ask time, so the instance answers
questions offline without the original tool running.
"""
import json
import subprocess
import sys

import pytest

from neuralosd._cmd._common import load_instance
from neuralosd._cmd.init import profile_any
from neuralosd.mitm import har_rows
from neuralosd.rea import rea_rows


def _init(tmp_path, source, name):
    out = tmp_path / name
    proc = subprocess.run(
        [sys.executable, "-m", "neuralosd", "init", "--source", source,
         "--name", name, "--out", str(out)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return str(out)


def _ask(inst_dir, q):
    return load_instance(inst_dir).ask(q)


# ── mitmproxy HAR ────────────────────────────────────────────────────────

HAR = {"log": {"entries": [
    {"startedDateTime": "2026-01-01T10:00:00Z", "time": 12.5,
     "request": {"method": "GET",
                 "url": "https://api.example.com/users?page=1"},
     "response": {"status": 200,
                  "content": {"mimeType": "application/json",
                              "size": 120}}},
    {"startedDateTime": "2026-01-01T10:00:01Z", "time": 250.0,
     "request": {"method": "POST", "url": "https://api.example.com/users"},
     "response": {"status": 500,
                  "content": {"mimeType": "text/plain", "size": 20}}},
    {"startedDateTime": "2026-01-01T10:00:02Z", "time": 8.0,
     "request": {"method": "GET",
                 "url": "https://cdn.example.com/app.js"},
     "response": {"status": 404,
                  "content": {"mimeType": "text/javascript", "size": 0}}},
]}}


def test_har_rows_are_flat_and_typed(tmp_path):
    path = tmp_path / "cap.har"
    path.write_text(json.dumps(HAR), encoding="utf-8")
    out = har_rows(str(path))
    assert out["parse_errors"] == 0
    rows = out["rows"]
    assert len(rows) == 3
    assert rows[0]["host"] == "api.example.com"
    assert rows[0]["status"] == 200
    assert rows[1]["time_ms"] == 250.0


def test_har_corrupt_file_is_a_parse_error_not_a_crash(tmp_path):
    path = tmp_path / "bad.har"
    path.write_text("{not json", encoding="utf-8")
    out = har_rows(str(path))
    assert out["rows"] == []
    assert out["parse_errors"] == 1


def test_har_instance_answers_request_questions(tmp_path):
    path = tmp_path / "cap.har"
    path.write_text(json.dumps(HAR), encoding="utf-8")
    inst = _init(tmp_path, f"mitm:{path}", "harinst")
    assert profile_any(f"mitm:{path}")["kind"] == "har"

    assert _ask(inst, "how many requests")["results"][0]["count"] == 3
    assert _ask(inst, "failed requests")["results"][0]["count"] == 2
    status = _ask(inst, "requests by status")["results"][0]["by_status"]
    assert status == {"200": 1, "500": 1, "404": 1}
    hosts = _ask(inst, "requests by host")["results"][0]["by_host"]
    assert hosts["api.example.com"] == 2


# ── rea evidence bundle ──────────────────────────────────────────────────

REA = {
    "findings": [
        {"id": "F1", "severity": "high", "title": "stack overflow",
         "function": "parse_packet", "confidence": 0.9},
        {"id": "F2", "severity": "low", "title": "weak hash",
         "function": "hash_pw", "confidence": 0.6},
    ],
    "functions": [{"name": "parse_packet", "size": 120},
                  {"name": "hash_pw", "size": 40}],
    "imports": [{"name": "memcpy", "library": "libc"}],
}


def test_rea_rows_choose_the_reference_section(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(REA), encoding="utf-8")
    out = rea_rows(str(path))
    assert out["section"] == "findings"
    assert len(out["rows"]) == 2
    assert out["sections"]["functions"] == 2

    explicit = rea_rows(str(path), section="functions")
    assert explicit["section"] == "functions"
    assert explicit["rows"][0]["name"] == "parse_packet"


def test_rea_instance_answers_finding_questions(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(REA), encoding="utf-8")
    inst = _init(tmp_path, f"rea:{path}#findings", "rea-inst")
    assert profile_any(f"rea:{path}#findings")["kind"] == "rea"

    assert _ask(inst, "how many findings")["results"][0]["count"] == 2
    listed = _ask(inst, "list findings")
    assert {r["id"] for r in listed["results"][0]["rows"]} == {"F1", "F2"}


# ── Plasma ───────────────────────────────────────────────────────────────

PLASMA = [
    {"ts": 1767261600.0, "host": "ws1", "event_type": "logon_fail",
     "severity": "high", "source": "security", "process": "lsass",
     "account": "alice", "message": "logon failed 4625"},
    {"ts": 1767261601.0, "host": "ws2", "event_type": "process_start",
     "severity": "low", "source": "sysmon", "process": "cmd.exe",
     "account": "bob", "message": "process start"},
    {"ts": 1767261602.0, "host": "ws1", "event_type": "logon_fail",
     "severity": "critical", "source": "security", "process": "lsass",
     "account": "carol", "message": "logon failed 4625"},
    {"ts": 1767261603.0, "host": "ws3", "event_type": "network_connect",
     "severity": "medium", "source": "tap", "process": "chrome.exe",
     "account": "dave", "message": "outbound connection"},
]


def test_plasma_instance_answers_security_questions(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in PLASMA) + "\n",
                    encoding="utf-8")
    inst = _init(tmp_path, f"plasma:{path}", "plasma-inst")

    critical = _ask(inst, "critical alerts")
    assert critical["probe"] == "critical_alerts"
    assert critical["results"][0]["count"] == 2

    logons = _ask(inst, "failed logons")
    assert logons["probe"] == "failed_logons"
    assert logons["results"][0]["by_account"] == {"alice": 1, "carol": 1}

    by_type = _ask(inst, "events by type")
    assert by_type["results"][0]["count"] == 3

    by_severity = _ask(inst, "events by severity")
    assert by_severity["probe"] == "events_by_severity"
    assert by_severity["results"][0]["by_severity"] == {
        "high": 1, "low": 1, "critical": 1, "medium": 1}

    network = _ask(inst, "network events")
    assert network["probe"] == "network_events"
    assert network["results"][0]["count"] == 1
