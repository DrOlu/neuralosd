"""ITSM template probes — 20 domain capabilities for any ITSM instance.

Every probe inherits the refusal gate, the accounting ledger, and the
behavioural banks from the runtime. The data comes from bridge.py, which
reads from CSV files staged alongside this module.
"""
from neuralosd import probe, enum_arg, pattern_arg, int_arg
import bridge


@probe(description="Count open (not resolved, closed or cancelled) incidents",
       triggers=["how many open incidents", "open incidents", "incident count",
                 "how many incidents"],
       name="count_incidents")
def count_incidents():
    open_count = bridge._open_tickets("Incident")
    return {"count": len(open_count), "rows_in": len(bridge.rows()),
            "rows_counted": len(open_count)}


@probe(description="Count unresolved incidents (including resolved-awaiting-closure)",
       triggers=["how many unresolved incidents", "unresolved incidents",
                 "unresolved incident count"],
       name="unresolved_incidents")
def unresolved_incidents():
    open_count = bridge._open_tickets("Incident")
    return {"count": len(open_count), "rows_in": len(bridge.rows()),
            "rows_counted": len(open_count)}


@probe(description="Incident status distribution across all incidents",
       triggers=["incident status breakdown", "incident status distribution",
                 "incidents by status"],
       name="incident_status_breakdown")
def incident_status_breakdown():
    return bridge.breakdown_by("status", ticket_type="Incident")


@probe(description="Open incident counts grouped by priority",
       triggers=["incidents by priority", "open incidents by priority",
                 "incident priority breakdown"],
       name="open_incidents_by_priority")
def open_incidents_by_priority():
    return bridge.breakdown_by("priority", ticket_type="Incident")


@probe(description="List open critical and high priority incidents",
       triggers=["critical incidents", "high priority incidents",
                 "critical and high priority incidents",
                 "show critical incidents", "critical high incidents"],
       name="critical_incidents")
def critical_incidents():
    return bridge.high_priority_open()


@probe(description="Show open incidents with assignee and group",
       triggers=["list open incidents", "show open incidents", "open incident list"],
       args={"limit": int_arg(15, 1, 50)},
       name="list_incidents")
def list_incidents(limit=15):
    rows = bridge._open_tickets("Incident")[:limit]
    return {"rows": rows, "returned": len(rows)}


@probe(description="Look up one ticket by its ticket ID",
       triggers=["ticket detail", "ticket status", "show ticket"],
       args={"ticket_id": pattern_arg(r"[A-Za-z]{3}\d+", True)},
       name="ticket_detail")
def ticket_detail(ticket_id):
    return bridge.ticket_detail(ticket_id)


@probe(description="Count open change requests",
       triggers=["how many changes", "open changes", "change count"],
       name="count_changes")
def count_changes():
    open_changes = bridge._open_tickets("Change")
    return {"count": len(open_changes), "rows_in": len(bridge.rows()),
            "rows_counted": len(open_changes)}


@probe(description="Change request status distribution",
       triggers=["change status breakdown", "changes by status",
                 "change status distribution"],
       name="change_status_breakdown")
def change_status_breakdown():
    return bridge.breakdown_by("status", ticket_type="Change")


@probe(description="Count open work orders",
       triggers=["how many work orders", "open work orders", "work order count"],
       name="count_work_orders")
def count_work_orders():
    open_wo = bridge._open_tickets("Work Order")
    return {"count": len(open_wo), "rows_in": len(bridge.rows()),
            "rows_counted": len(open_wo)}


@probe(description="Work order status distribution",
       triggers=["work order status breakdown", "work orders by status",
                 "work order status distribution"],
       name="workorder_status_breakdown")
def workorder_status_breakdown():
    return bridge.breakdown_by("status", ticket_type="Work Order")


@probe(description="Count open problem investigations",
       triggers=["how many problems", "open problems", "problem count"],
       name="count_problems")
def count_problems():
    open_problems = bridge._open_tickets("Problem")
    return {"count": len(open_problems), "rows_in": len(bridge.rows()),
            "rows_counted": len(open_problems)}


@probe(description="Count known error records",
       triggers=["how many known errors", "known errors", "known error count"],
       name="count_known_errors")
def count_known_errors():
    open_problems = bridge._open_tickets("Problem")
    known = [r for r in open_problems
             if (r.get("known_error") or "").strip().lower() == "yes"]
    return {"count": len(known), "rows_in": len(open_problems),
            "rows_counted": len(known)}


@probe(description="Count service requests",
       triggers=["how many service requests", "service request count"],
       name="count_service_requests")
def count_service_requests():
    open_sr = bridge._open_tickets("Service Request")
    return {"count": len(open_sr), "rows_in": len(bridge.rows()),
            "rows_counted": len(open_sr)}


@probe(description="Count SLA measurement records",
       triggers=["how many slas", "sla count", "sla measurements"],
       name="count_slas")
def count_slas():
    all_rows = bridge.rows()
    slas = [r for r in all_rows
            if (r.get("ticket_type") or "").strip().lower() == "sla"]
    return {"count": len(slas)}


@probe(description="List support groups with open ticket counts",
       triggers=["support groups", "list support groups",
                 "support group breakdown"],
       name="support_groups")
def support_groups():
    return bridge.breakdown_by("assigned_group")


@probe(description="Open tickets grouped by assigned group — who is busiest",
       triggers=["tickets by group", "busiest support group",
                 "incidents per group", "workload by group"],
       args={"limit": int_arg(15, 1, 50)},
       name="tickets_by_group")
def tickets_by_group(limit=15):
    return bridge.tickets_by_group(limit)


@probe(description="Count worklog entries",
       triggers=["how many worklog entries", "worklog count",
                 "work log entries"],
       name="worklog_count")
def worklog_count():
    logs = bridge.worklog_rows()
    return {"count": len(logs), "rows_in": len(logs), "rows_counted": len(logs)}


@probe(description="Overall ITSM digest: open counts by type",
       triggers=["itsm overview", "it service management overview",
                 "ticket overview"],
       name="itsm_overview")
def itsm_overview():
    all_rows = bridge.rows()
    by_type = {}
    for r in all_rows:
        t = (r.get("ticket_type") or "unknown").strip()
        by_type[t] = by_type.get(t, 0) + 1
    open_tkts = bridge._open_tickets()
    return {"total_tickets": len(all_rows),
            "open_tickets": len(open_tkts),
            "by_type": by_type,
            "rows_in": len(all_rows), "rows_counted": len(all_rows)}


PROBES = [
    count_incidents, unresolved_incidents, incident_status_breakdown,
    open_incidents_by_priority, critical_incidents, list_incidents,
    ticket_detail, count_changes, change_status_breakdown,
    count_work_orders, workorder_status_breakdown, count_problems,
    count_known_errors, count_service_requests, count_slas,
    support_groups, tickets_by_group, worklog_count, itsm_overview,
]
