"""Streaming ingestion (v2.8.1) — `neuralosd watch`.

One poll cycle: read NEW events from the configured sources, normalize
them into the standard event shape, append to a BOUNDED local buffer
(watch_buffer.jsonl in the instance dir), evaluate threshold rules over
sliding windows, and fire webhooks for alerts.

Bounded by design: the buffer keeps the newest `cap` events; tail reads
are offset-based with rotation detection; a rule without a webhook just
lands in the report. Everything injectable (poster, clock) — the whole
cycle is unit-testable without a daemon or network.
"""
import json
import os
import re
import time

from .logsources import parse_syslog_line


class RingBuffer:
    """A bounded JSONL event file: newest kept, oldest trimmed."""

    def __init__(self, path, cap=10000):
        self.path = path
        self.cap = max(1, int(cap))
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def append(self, events):
        if isinstance(events, dict):
            events = [events]                # a single event is one row
        existing = []
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        existing.append(json.loads(line))
                    except ValueError:
                        continue
        existing.extend(events)
        existing = existing[-self.cap:]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in existing:
                fh.write(json.dumps(e, ensure_ascii=False, default=str)
                         + "\n")
        os.replace(tmp, self.path)
        return len(events)

    def read(self, limit=None):
        if not os.path.isfile(self.path):
            return []
        events = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        return events[-limit:] if limit else events


def tail_new_lines(path, state):
    """Read only the NEW content of a text file, offset-based, with
    rotation detection: a file that shrank below the stored offset was
    rotated/truncated — re-read it from the start. `state` is the caller's
    dict; the offset is stored under the file's key."""
    key = "offset:" + os.path.realpath(path)
    offset = state.get(key, 0)
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], state
    if size < offset:
        offset = 0                                  # rotated or truncated
    lines = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        for line in fh:
            lines.append(line.rstrip("\r\n"))
    state[key] = os.path.getsize(path)
    return lines, state


def evaluate_rules(rules, events, now=None):
    """Threshold rules over sliding windows.

    rule = {"name": str, "match": {field: value, ...} (subset match),
            "threshold": int, "window_minutes": int}
    An event matches when every match-field equals the event's field.
    Window filtering applies only to events with a parseable ts; events
    without one are excluded from windowed rules (an unknown when is not a
    recent when).
    """
    import datetime
    now = now if now is not None else time.time()
    alerts = []
    for rule in rules or []:
        match = rule.get("match") or {}
        window_min = rule.get("window_minutes")
        threshold = max(1, int(rule.get("threshold", 1)))
        matched_events = []
        for e in events:
            if any(e.get(k) != v for k, v in match.items()):
                continue
            if window_min:
                ts = e.get("ingested_at") or e.get("ts")
                if ts is None:
                    continue
                try:
                    if isinstance(ts, str):
                        t = datetime.datetime.fromisoformat(
                            ts.replace("Z", "+00:00"))
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=datetime.timezone.utc)
                        age_s = now - t.timestamp()
                    else:
                        age_s = now - float(ts)
                except (ValueError, TypeError, OSError, OverflowError):
                    continue
                if age_s > window_min * 60:
                    continue
            matched_events.append(e)
        if len(matched_events) >= threshold:
            alerts.append({"rule": rule.get("name") or "unnamed",
                           "matches": len(matched_events),
                           "threshold": threshold,
                           "sample": [e.get("message", "")
                                      for e in matched_events[:3]]})
    return alerts


def post_webhook(url, payload, poster=None, timeout=30):
    """POST an alert to a webhook. Returns True/False — a failed webhook
    is REPORTED by the caller, never allowed to kill the watch loop."""
    if poster is not None:
        try:
            return bool(poster(url, payload))
        except Exception:                                  # noqa: BLE001
            return False
    import urllib.request
    try:
        data = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:                                      # noqa: BLE001
        return False


def run_cycle(instance_dir, sources, rules=None, once=True, cap=10000,
              state=None, poster=None, webhook=None, now=None):
    """One poll cycle over the configured sources.

    sources: [{"kind": "syslog", "path": str}] (syslog files are
    offset-tailed with rotation detection). Returns a report
    {ingested, buffer, alerts, notifications}.
    """
    instance_dir = os.path.abspath(instance_dir)
    own_state = state is None
    state = state if state is not None else {}
    state_path = os.path.join(instance_dir, "watch_state.json")
    if not state and os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (ValueError, OSError):
            state = {}

    buf = RingBuffer(os.path.join(instance_dir, "watch_buffer.jsonl"),
                     cap=cap)
    ingested = 0
    new_events = []
    seen_at = now if now is not None else time.time()
    for src in sources or []:
        kind = src.get("kind")
        if kind == "syslog":
            lines, state = tail_new_lines(src["path"], state)
            for line in lines:
                ev = parse_syslog_line(line)
                if ev is not None:
                    # ingested_at = when THIS cycle saw the event; rules
                    # with windows use it (RFC3164 ts has no year/timezone)
                    ev["ingested_at"] = seen_at
                    new_events.append(ev)
    ingested += buf.append(new_events)
    # persist tail offsets so the next cycle reads only NEW content
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, state_path)

    # alert over THIS cycle's new events: a buffer-wide replay would
    # re-fire the same alert every cycle forever. The bounded buffer keeps
    # history queryable; firing is per-cycle.
    new_alert_events = new_events
    events = buf.read()
    alerts = evaluate_rules(rules, new_alert_events, now=now)

    notifications = []
    for a in alerts:
        hook = a.get("webhook") or webhook
        if hook:
            ok = post_webhook(hook, {"instance":
                                     os.path.basename(instance_dir),
                                     **a}, poster=poster)
            notifications.append({"rule": a["rule"], "delivered": ok})

    report = {"ingested": ingested, "buffer": buf.path,
              "buffer_events": len(events), "alerts": alerts,
              "notifications": notifications, "ts": time.time()}
    rpt_path = os.path.join(instance_dir, "watch_report.json")
    with open(rpt_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    report["report_path"] = rpt_path
    return report
