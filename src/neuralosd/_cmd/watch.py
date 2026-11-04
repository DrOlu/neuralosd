"""`neuralosd watch` — streaming ingestion + threshold alerts.

    neuralosd watch --instance-dir . \\
        --source syslog:/var/log/system.log \\
        --rules rules.json [--once] [--webhook https://hooks...]

One poll cycle: new events from each source are normalized into the
bounded buffer, rules are evaluated over the cycle's new events, and
alerts fire webhooks. Without --once, polls every --interval seconds.
rules.json: [{"name": "ssh failures", "match": {"app": "sshd"},
"threshold": 5, "window_minutes": 10, "webhook": "https://..."}]
"""
import json
import os
import sys
import time


def run(a):
    from ..watch import run_cycle

    instance_dir = os.path.abspath(a.instance_dir)
    sources = []
    for s in (getattr(a, "source", None) or []):
        if s.startswith("syslog:"):
            sources.append({"kind": "syslog", "path": s.split(":", 1)[1]})
        elif s.startswith("journal"):
            sources.append({"kind": "journal"})
        else:
            sources.append({"kind": "syslog", "path": s})
    if not sources:
        print("error: at least one --source is required", file=sys.stderr)
        return 2
    rules = []
    if getattr(a, "rules", None) and os.path.isfile(a.rules):
        try:
            with open(a.rules, encoding="utf-8") as fh:
                rules = json.load(fh)
        except ValueError as e:
            print(f"error: rules file invalid: {e}", file=sys.stderr)
            return 2

    while True:
        try:
            report = run_cycle(instance_dir, sources, rules=rules,
                               once=True, cap=a.cap,
                               webhook=getattr(a, "webhook", None))
        except Exception as e:                             # noqa: BLE001
            print(f"error: watch cycle failed: {str(e)[:200]}",
                  file=sys.stderr)
            return 1
        mark = f"alerts: {len(report['alerts'])}" if report["alerts"] \
            else f"ingested {report['ingested']}"
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {mark}", flush=True)
        for al in report["alerts"]:
            print(f"  ALERT {al['rule']}: {al['matches']} matching events; "
                  f"sample: {al['sample'][:2]}", flush=True)
        if getattr(a, "once", False):
            return 0
        time.sleep(getattr(a, "interval", 30))
