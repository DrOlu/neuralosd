"""Tests-first: deep Excel connector (formulas / multi-sheet / named ranges)
+ log source connectors (Windows Event Log / journald / syslog).

RED state by design — these tests fail until the features land:

  1. Deep Excel: `neuralosd init --source book.xlsx` now profiles FORMULAS
     (count + samples per sheet), ALL sheets (per-sheet row/column/formula
     counts, scaffolded as a `sheets` probe), and NAMED RANGES (defined names
     with target + resolvable value preview, scaffolded as a `named_ranges`
     probe) — plus a caged `formula_scan` probe per sheet.
  2. Log sources: three new `init` source prefixes, normalized to event rows
     ({ts, host, app, level?, message}):
       `syslog:<path>`          — RFC3164/RFC5424 text, stdlib only
       `journal:[unit]`         — journald via `journalctl -o json` (bounded)
       `wevtutil:<Channel>`     — Windows Event Log via `wevtutil ... /f:XML`
     The capture layer is injectable, so journald/wevtutil profiles are unit-
     tested on this Mac (which has neither tool); true end-to-end ask tests
     skip when the native tool is absent.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from neuralosd._cmd._common import load_instance


def _res(env):
    """The router wraps probe results in a single-element list on the walk
    path and returns the bare dict on the direct path — normalize."""
    res = env["results"]
    return res[0] if isinstance(res, list) else res


def _make_workbook(path):
    """Ledger sheet (4 data rows + a formula), Notes sheet, one named range."""
    openpyxl = pytest.importorskip("openpyxl",
                                   reason="deep Excel tests need openpyxl")
    from openpyxl.workbook.defined_name import DefinedName
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ledger"
    ws.append(["region", "amount"])
    ws.append(["North", 100])
    ws.append(["South", 200])
    ws.append(["East", 50])
    ws.append(["West", 25])
    ws["B6"] = "=SUM(B2:B5)"          # a formula on the primary sheet
    ws["B6"].comment = None
    notes = wb.create_sheet("Notes")
    notes.append(["memo"])
    notes.append(["reconciled"])
    wb.defined_names.add(DefinedName("SalesRange",
                                     attr_text="Ledger!$B$2:$B$5"))
    wb.save(path)
    return str(path)


def _init(a_source, a_out, a_name="inst"):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd

    os.makedirs(a_out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=a_source, out=a_out, name=a_name,
                                 packages=None))


# ── §1: deep Excel ─────────────────────────────────────────────────────────

def test_init_profiles_formulas_sheets_and_named_ranges(tmp_path):
    """The profile names every sheet, counts formulas, and lists named
    ranges — and the scaffolded probes.py mentions all three."""
    book = _make_workbook(str(tmp_path / "book.xlsx"))
    out = str(tmp_path / "inst")
    _init(book, out)

    probes_src = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "Notes" in probes_src                 # second sheet named
    assert "SalesRange" in probes_src            # named range named
    assert "formula" in probes_src.lower()       # formula probes scaffolded


def test_excel_instance_answers_sheets_and_named_ranges(tmp_path):
    book = _make_workbook(str(tmp_path / "book.xlsx"))
    out = str(tmp_path / "inst")
    _init(book, out)
    inst = load_instance(out)

    env = inst.ask("how many rows", use_cache=False)
    assert env["probe"] == "row_count"
    assert _res(env)["count"] == 4    # primary sheet DATA rows (header skipped)

    env = inst.ask("list sheets", use_cache=False)
    assert env["probe"] == "sheets"
    names = [s["sheet"] for s in _res(env)["sheets"]]
    assert names == ["Ledger", "Notes"]

    env = inst.ask("named ranges", use_cache=False)
    assert env["probe"] == "named_ranges"
    assert "SalesRange" in _res(env)["named_ranges"]
    entry = _res(env)["named_ranges"]["SalesRange"]
    assert entry["target"] == "Ledger!$B$2:$B$5"


def test_formula_scan_reports_formulas_per_sheet(tmp_path):
    book = _make_workbook(str(tmp_path / "book.xlsx"))
    out = str(tmp_path / "inst")
    _init(book, out)
    inst = load_instance(out)

    env = inst.ask("formula scan", use_cache=False)
    assert env["probe"] == "formula_scan"
    res = _res(env)
    assert res["formulas"] >= 1
    assert any("SUM" in str(s) for s in res["samples"])


# ── §2: log connectors ─────────────────────────────────────────────────────

SYSLOG_FIXTURE = (
    "Nov  2 03:11:22 web01 sshd[4123]: Accepted publickey for olu\n"
    "Nov  2 03:12:01 web01 systemd[1]: Started nginx.service.\n"
    "Nov  2 03:13:44 db01 postgres[900]: checkpoint complete\n"
    "this line is not syslog at all\n"
)


def test_syslog_parser_handles_rfc3164_and_skips_garbage():
    from neuralosd.logsources import parse_syslog_line
    ev = parse_syslog_line("Nov  2 03:11:22 web01 sshd[4123]: Accepted publickey")
    assert ev["host"] == "web01"
    assert ev["app"] == "sshd"
    assert ev["message"] == "Accepted publickey"
    assert parse_syslog_line("this line is not syslog at all") is None


def test_init_profiles_syslog_file(tmp_path):
    log = tmp_path / "system.log"
    log.write_text(SYSLOG_FIXTURE, encoding="utf-8")
    out = str(tmp_path / "inst")
    _init(f"syslog:{log}", out, "syslog")

    probes_src = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "sshd" in probes_src or "app" in probes_src

    inst = load_instance(out)
    env = inst.ask("how many rows", use_cache=False)
    assert env["probe"] == "row_count"
    assert _res(env)["count"] == 3               # the garbage line is a hole,
    # not an event — and the bridge REPORTS it (parse_errors), not silently.


JOURNALD_FIXTURE = "\n".join([
    json.dumps({"__REALTIME_TIMESTAMP": "1730512282000000",
                "_HOSTNAME": "web01", "_COMM": "sshd",
                "SYSLOG_IDENTIFIER": "sshd", "PRIORITY": "6",
                "_SYSTEMD_UNIT": "ssh.service",
                "MESSAGE": "Accepted publickey for olu"}),
    json.dumps({"__REALTIME_TIMESTAMP": "1730512344000000",
                "_HOSTNAME": "web01", "_COMM": "systemd",
                "PRIORITY": "3", "_SYSTEMD_UNIT": "nginx.service",
                "MESSAGE": "Failed to start nginx"}),
]) + "\n"

WEVT_FIXTURE = (
    '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
    "<System><EventID>4624</EventID><Level>0</Level>"
    "<Provider Name=\"Microsoft-Windows-Security-Auditing\"/>"
    "<Computer>WS1</Computer>"
    "<TimeCreated SystemTime=\"2026-11-01T09:11:22.000Z\"/></System>"
    "</Event>"
    '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
    "<System><EventID>7031</EventID><Level>2</Level>"
    "<Provider Name=\"Service Control Manager\"/><Computer>WS1</Computer>"
    "<TimeCreated SystemTime=\"2026-11-01T09:12:01.000Z\"/></System>"
    "</Event>"
)


def _patch_capture(monkeypatch, text):
    from neuralosd._cmd import init as init_cmd
    calls = []

    def fake_capture(cmd, cap):
        calls.append((cmd, cap))
        return text

    monkeypatch.setattr(init_cmd, "_capture", fake_capture)
    return calls


def test_init_profiles_journald_via_injected_capture(tmp_path, monkeypatch):
    """journald profile is unit-tested through the injectable capture layer —
    the host running the tests does not need journalctl."""
    calls = _patch_capture(monkeypatch, JOURNALD_FIXTURE)
    out = str(tmp_path / "inst")
    _init("journal:", out, "journal")

    assert calls and "journalctl" in " ".join(calls[0][0])
    probes_src = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "app" in probes_src                  # app column profiled (values
    # live in the data, not the menu)
    bridge_src = open(os.path.join(out, "bridge.py"), encoding="utf-8").read()
    assert "journal_rows" in bridge_src          # bridge reads live journald


def test_init_profiles_wevtutil_via_injected_capture(tmp_path, monkeypatch):
    calls = _patch_capture(monkeypatch, WEVT_FIXTURE)
    out = str(tmp_path / "inst")
    _init("wevtutil:System", out, "wevt")

    assert calls and "wevtutil" in " ".join(calls[0][0])
    assert "System" in " ".join(calls[0][0])     # the channel is targeted
    probes_src = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    assert "event_id" in probes_src or "4624" in probes_src
    bridge_src = open(os.path.join(out, "bridge.py"), encoding="utf-8").read()
    assert "wevt_rows" in bridge_src


def test_wevtutil_utf16_output_still_parses():
    """wevtutil writes UTF-16LE when redirected — the classic way a Windows
    Event Log capture comes back as ZERO parseable events (every other byte
    a NUL). The decode layer must be BOM-aware. This is the Windows CI
    failure from the v2.2.0 tag, pinned here so it cannot come back.
    """
    from neuralosd.logsources import decode_tool_output, parse_wevt_xml
    raw = WEVT_FIXTURE.encode("utf-16")          # includes the BOM
    rows = parse_wevt_xml(decode_tool_output(raw))
    assert len(rows) == 2
    assert rows[0]["event_id"] == "4624"
    assert rows[1]["level"] == "error"
    # and the decode alone is transparent for utf-8 tool output:
    assert decode_tool_output(JOURNALD_FIXTURE.encode("utf-8")) == \
        JOURNALD_FIXTURE


@pytest.mark.skipif(not (os.path.isfile("/bin/journalctl")
                         or os.path.exists("/usr/bin/journalctl")),
                    reason="journald not present on this host")
def test_journald_end_to_end_when_journalctl_exists(tmp_path):
    out = str(tmp_path / "inst")
    _init("journal:", out, "journal")
    inst = load_instance(out)
    env = inst.ask("how many rows", use_cache=False)
    assert env["probe"] == "row_count"
    assert _res(env)["count"] > 0


@pytest.mark.skipif(os.system("where wevtutil >NUL 2>&1") != 0
                    if os.name == "nt" else True,
                    reason="wevtutil only exists on Windows")
def test_wevtutil_end_to_end_when_wevtutil_exists(tmp_path):
    out = str(tmp_path / "inst")
    _init("wevtutil:System", out, "wevt")
    inst = load_instance(out)
    env = inst.ask("how many rows", use_cache=False)
    assert env["probe"] == "row_count"
    assert _res(env)["count"] > 0
