"""Data layer for the ITSM template instance.

Reads from CSV files staged alongside this module. Replace the CSVs with your
own data (or rewrite `rows()` to query your ITSM API directly) — every probe
above this layer picks up the change automatically.

The data model is a single flat table of ITSM tickets:
  ticket_id, ticket_type, priority, status, assigned_group, assignee,
  short_description, category, created_date, resolved_date

`ticket_type` distinguishes Incident / Problem / Change / Work Order.
"""
import csv
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(_HERE, "data", "itsm_tickets.csv")
WORKLOG_SOURCE = os.path.join(_HERE, "data", "itsm_worklogs.csv")

OPEN_STATES = {"open", "new", "assigned", "in progress", "pending", "investigation",
               "root cause analysis", "planning", "approval", "implementation",
               "draft", "scheduled"}
RESOLVED_STATES = {"resolved", "closed", "cancelled"}


def rows():
    with open(SOURCE, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def worklog_rows():
    with open(WORKLOG_SOURCE, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _blank(v):
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    return v


def count():
    n_rows = 0
    counted = 0
    for r in rows():
        n_rows += 1
        counted += 1
    return {"count": counted, "rows_in": n_rows, "rows_counted": counted,
            "skipped": {}}


def sample(n=10):
    return {"rows": rows()[:n]}


def distinct(column, cap=100):
    vals = [r.get(column) for r in rows()]
    present = [v for v in vals if _blank(v) is not None]
    clean = sorted({str(v).strip() for v in present})
    return {"column": column, "count": len(clean), "distinct": clean[:cap],
            "rows_in": len(vals), "rows_counted": len(present),
            "truncated": max(0, len(clean) - cap),
            "skipped": {"null": len(vals) - len(present)}}


def _open_tickets(ticket_type=None):
    out = []
    for r in rows():
        ttype = (r.get("ticket_type") or "").strip().lower()
        status = (r.get("status") or "").strip().lower()
        if ticket_type and ttype != ticket_type.lower():
            continue
        if status in RESOLVED_STATES:
            continue
        out.append(r)
    return out


def total_tickets():
    all_rows = rows()
    open_tkts = _open_tickets()
    return {"total_tickets": len(all_rows), "open_tickets": len(open_tkts),
            "rows_in": len(all_rows), "rows_counted": len(open_tkts),
            "skipped": {"resolved_or_closed": len(all_rows) - len(open_tkts)}}


def breakdown_by(field, ticket_type=None):
    """Group open tickets by a column and count."""
    groups = {}
    for r in _open_tickets(ticket_type):
        val = _blank(r.get(field))
        if val is None:
            continue
        key = str(val).strip()
        groups[key] = groups.get(key, 0) + 1
    ordered = sorted(groups.items(), key=lambda x: -x[1])
    return {"by": field, "groups": len(ordered),
            "rows": [{field: k, "count": v} for k, v in ordered],
            "total": sum(groups.values()), "rows_in": len(rows()),
            "rows_counted": sum(groups.values()),
            "skipped": {"no_" + field: len(rows()) - sum(groups.values())}}


def tickets_by_type():
    counts = {}
    for r in rows():
        t = (r.get("ticket_type") or "unknown").strip()
        counts[t] = counts.get(t, 0) + 1
    ordered = sorted(counts.items(), key=lambda x: -x[1])
    return {"ticket_type_counts": ordered, "total": len(rows())}


def high_priority_open():
    out = [r for r in _open_tickets()
           if (r.get("priority") or "").strip().lower() in ("critical", "high")]
    return {"count": len(out),
            "rows": [{"ticket_id": r.get("ticket_id"),
                      "priority": r.get("priority"),
                      "status": r.get("status"),
                      "assigned_group": r.get("assigned_group"),
                      "short_description": r.get("short_description")}
                     for r in out]}


def ticket_detail(ticket_id):
    for r in rows():
        if (r.get("ticket_id") or "").strip().lower() == ticket_id.strip().lower():
            logs = [w for w in worklog_rows()
                    if (w.get("ticket_id") or "").strip().lower()
                    == ticket_id.strip().lower()]
            return {"ticket": r, "worklogs": logs}
    return {"error": f"ticket {ticket_id!r} not found"}


def tickets_by_group(limit=15):
    groups = {}
    for r in _open_tickets():
        grp = _blank(r.get("assigned_group"))
        if grp is None:
            continue
        key = str(grp).strip()
        groups[key] = groups.get(key, 0) + 1
    ordered = sorted(groups.items(), key=lambda x: -x[1])[:limit]
    return {"by": "assigned_group", "groups": len(ordered),
            "rows": [{"assigned_group": k, "open_tickets": v}
                     for k, v in ordered],
            "total": sum(groups.values())}
