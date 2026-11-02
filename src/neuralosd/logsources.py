"""Log source connectors — Windows Event Log, journald, syslog.

One contract: every log source normalizes to event ROWS (plain dicts) with
a stable shape — {ts, host, app, level, message} (+ source-specific extras
like event_id) — so the standard generated probes (row_count, distinct_*)
work unchanged on top of them.

Bounded by a cap: a log is read NEWEST-FIRST up to `cap` entries (the
generated bridge embeds the cap resolved at init; default 10000). A capped
read is a deliberate snapshot, never a silent truncation — the profile and
the generated bridge record the cap.

Capture is injectable (`runner=` callable taking (cmd, cap) -> stdout), so
journald / wevtutil profiles are unit-testable on hosts that lack the
native tools, and the generated bridges read the live log at ask time.
"""
import datetime
import json
import os
import re

_LOG_CAP_DEFAULT = 10000

_SYSLOG_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+"
    r"(?P<app>[\w\-./]+?)(?:\[(?P<pid>\d+)\])?:\s?(?P<msg>.*)$")

_RFC5424_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>\d*\s*(?P<ts>\S+)\s+(?P<host>\S+)\s+"
    r"(?P<app>\S+)\s+\S+\s+\S+\s+(?P<msg>.*)$")

_JOURNAL_LEVELS = {0: "emerg", 1: "alert", 2: "crit", 3: "err",
                   4: "warning", 5: "notice", 6: "info", 7: "debug"}

_WEVT_LEVELS = {0: "info", 1: "critical", 2: "error", 3: "warning",
                4: "info", 5: "verbose"}


def log_cap_default():
    try:
        return max(1, int(os.environ.get("NEURALOSD_LOG_CAP",
                        _LOG_CAP_DEFAULT)))
    except ValueError:
        return _LOG_CAP_DEFAULT


def priority_to_level(p):
    """journald PRIORITY (0-7) -> human level."""
    try:
        return _JOURNAL_LEVELS.get(int(p), "info")
    except (TypeError, ValueError):
        return "info"


def wevt_level_to_name(level):
    """wevtutil Event/System/Level (0-5) -> human level."""
    try:
        return _WEVT_LEVELS.get(int(level), "info")
    except (TypeError, ValueError):
        return "info"


# ── syslog (file, stdlib only) ─────────────────────────────────────────────

def parse_syslog_line(line):
    """One RFC3164 line -> {ts, host, app, message, pid?} | None.

    A line that does not parse is NOT an event: the caller counts it as a
    parse error (a hole), never silently drops it into the rows.
    """
    text = line.rstrip("\r\n")
    if not text.strip():
        return None
    if text.startswith("<"):
        m = _RFC5424_RE.match(text)
        if m:
            d = m.groupdict()
            return {"ts": d["ts"], "host": d["host"], "app": d["app"],
                    "level": _syslog_pri_level(d.get("pri")),
                    "message": d["msg"]}
        return None
    m = _SYSLOG_RE.match(text)
    if not m:
        return None
    d = m.groupdict()
    row = {"ts": f"{d['mon']} {int(d['day']):2d} {d['time']}",
           "host": d["host"], "app": d["app"],
           "level": "info", "message": d["msg"]}
    if d.get("pid"):
        row["pid"] = d["pid"]
    return row


def _syslog_pri_level(pri):
    try:
        severity = int(str(pri)) % 8
        return _JOURNAL_LEVELS.get(severity, "info")
    except (TypeError, ValueError):
        return "info"


def parse_syslog_file(path, cap=None):
    """Parse a syslog text file (bounded). Returns a dict — never a bare
    list — because the PARSE ERRORS are part of the answer:

        {"rows": [...], "parse_errors": n, "truncated": bool}
    """
    cap = cap or log_cap_default()
    rows, errors, truncated = [], 0, False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            ev = parse_syslog_line(line)
            if ev is None:
                if line.strip():
                    errors += 1
                continue
            if len(rows) >= cap:
                truncated = True
                break
            rows.append(ev)
    return {"rows": rows, "parse_errors": errors, "truncated": truncated}


# ── journald (journalctl) ──────────────────────────────────────────────────

def journal_rows(cap=None, unit=None, runner=None):
    """Read journald via `journalctl -o json` (json-lines), newest first.

    `runner(cmd, cap) -> stdout` overrides execution (tests inject here).
    Returns a list of normalized event rows. Lines that fail to parse are
    skipped (machine-generated json: a parse failure is a capture bug, not
    a data hole — the exit code check is where capture errors surface).
    """
    cap = cap or log_cap_default()
    cmd = ["journalctl", "-o", "json", "--no-pager", "-n", str(cap)]
    if unit:
        cmd += ["-u", unit]
    out = _execute(cmd, cap, runner)
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        rows.append({
            "ts": _journal_ts(entry.get("__REALTIME_TIMESTAMP")),
            "host": entry.get("_HOSTNAME") or "",
            "app": (entry.get("SYSLOG_IDENTIFIER")
                    or entry.get("_COMM")
                    or entry.get("_SYSTEMD_UNIT") or ""),
            "level": priority_to_level(entry.get("PRIORITY")),
            "unit": entry.get("_SYSTEMD_UNIT") or "",
            "message": entry.get("MESSAGE") or "",
        })
    return rows


def _journal_ts(micros):
    try:
        sec = int(micros) / 1_000_000.0
        return datetime.datetime.fromtimestamp(
            sec, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


# ── Windows Event Log (wevtutil) ───────────────────────────────────────────

def wevt_rows(channel, cap=None, runner=None):
    """Read a Windows Event Log channel via `wevtutil qe <channel>
    /f:XML /rd:true /c:<cap>`. Returns normalized event rows.

    Message text is not part of the XML render (it needs a separate
    /format:Text pass per event) — the row carries the structured System
    fields (event_id, provider, level, computer, time), which is what the
    count/breakdown probes need.
    """
    cap = cap or log_cap_default()
    cmd = ["wevtutil", "qe", channel, f"/c:{cap}", "/rd:true", "/f:XML"]
    out = _execute(cmd, cap, runner)
    return parse_wevt_xml(out)


def parse_wevt_xml(text):
    """Parse concatenated wevtutil XML <Event> fragments into rows."""
    import xml.etree.ElementTree as ET
    rows = []
    if not text or not text.strip():
        return rows
    wrapped = "<__events__>" + text + "</__events__>"
    # wevtutil fragments declare nothing hostile, but namespaces appear on
    # the root Event tag only; strip them for flat System/... paths.
    wrapped = re.sub(r"\sxmlns=\"[^\"]*\"", "", wrapped)
    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError:
        # tolerate a truncated capture: salvage complete <Event> blocks
        blocks = re.findall(r"<Event[\s>].*?</Event>", text, flags=re.S)
        salvaged = []
        for b in blocks:
            try:
                salvaged.append(ET.fromstring(
                    re.sub(r"\sxmlns=\"[^\"]*\"", "", b)))
            except ET.ParseError:
                continue
        root = {"__salvaged__": salvaged}

    events = (root if isinstance(root, list)
              else list(root) if not isinstance(root, dict) else None)
    if events is None:
        events = root["__salvaged__"]
    for ev in events:
        sys_node = ev.find("System") if hasattr(ev, "find") else None
        if sys_node is None:
            continue
        def _t(tag):
            node = sys_node.find(tag)
            return node.text if node is not None and node.text else ""
        provider = sys_node.find("Provider")
        time_node = sys_node.find("TimeCreated")
        rows.append({
            "ts": (time_node.get("SystemTime", "")
                   if time_node is not None else ""),
            "host": _t("Computer"),
            "app": (provider.get("Name", "")
                    if provider is not None else ""),
            "level": wevt_level_to_name(_t("Level")),
            "event_id": _t("EventID"),
            "message": "",
        })
    return rows


# ── execution ──────────────────────────────────────────────────────────────

def decode_tool_output(data):
    """Decode native-tool stdout, BOM-aware.

    wevtutil writes UTF-16LE when its output is redirected (the classic way
    a Windows Event Log capture comes back as zero parseable events: the
    XML is there, every other byte is a NUL). Everything else (journalctl,
    text logs) is UTF-8.
    """
    if isinstance(data, str):
        return data
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8", errors="replace")


def _execute(cmd, cap, runner):
    if runner is not None:
        return runner(cmd, cap)
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except FileNotFoundError:
        raise SystemExit(
            f"error: {' '.join(cmd[:2])} is not available on this host.\n"
            "    (this source must be read on the machine that owns the log)")
    if r.returncode != 0:
        err = decode_tool_output(r.stderr or b"").strip()[:200]
        raise SystemExit(f"error: {' '.join(cmd)} failed: {err}")
    return decode_tool_output(r.stdout)
