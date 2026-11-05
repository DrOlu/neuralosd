"""Plasma EDR connector (v2.9.0) — read the events.jsonl that Plasma
produces on Windows nodes.

Plasma's strict PlasmaEvent contract: each row is a JSON object with
{ts, host, source, event_type, severity, ...}. The sensors that produce
these rows (WHIDS/Sysmon, krabsetw ETW tap, Event Log identity) all
converge on one events.jsonl per node.

This connector reads that store, validates each row, and normalizes to
the standard event shape so the generated probes work unchanged. Events
that fail validation are counted (`parse_errors`) — never silently
dropped.

For testing: `runner` is injectable (same pattern as logsources), so the
full flow is testable without a Windows node or a running Plasma.
"""
import json
import os


def parse_plasma_event(line):
    """One PlasmaEvent JSONL row -> a validated event dict, or None.

    A row that fails validation is NOT an event: the caller counts it as
    a parse error (a hole), never silently drops it into the rows.
    """
    text = line.strip()
    if not text:
        return None
    try:
        row = json.loads(text)
    except ValueError:
        return None
    if not isinstance(row, dict):
        return None
    # normalize: the standard probe pipeline works on {ts, host, event_type,
    # severity, ...}. PlasmaEvent rows already carry these; we validate
    # rather than transform.
    required = ("ts", "host", "event_type")
    if any(not row.get(k) for k in required):
        return None
    return row


def parse_plasma_events(path, cap=None):
    """Read a Plasma events.jsonl store (bounded). Returns a dict:
    {"rows": [...], "parse_errors": n, "truncated": bool}
    """
    cap = cap or 10000
    rows, errors, truncated = [], 0, False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            ev = parse_plasma_event(line)
            if ev is None:
                if line.strip():
                    errors += 1
                continue
            if len(rows) >= cap:
                truncated = True
                break
            rows.append(ev)
    return {"rows": rows, "parse_errors": errors, "truncated": truncated}


def plasma_rows(path, cap=None):
    """Convenience: just the rows (for probe functions)."""
    return parse_plasma_events(path, cap=cap)["rows"]
