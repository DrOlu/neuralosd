"""mitmproxy HAR connector — capture HTTP traffic once, answer offline.

mitmproxy exports HAR (`File → Export → HAR`) and `mitmdump --save-har`
writes one. This reader flattens that file into request rows so a neuralosd
instance can answer questions about the capture without replaying it:

    neuralosd init --source mitm:capture.har --name api

Rows are tolerant: a malformed entry is counted, never fatal. The capture
is read at ask time by the generated bridge, so a new export is picked up
without re-scaffolding the instance.
"""
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit


def _epoch(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def har_rows(path, cap: int = 5000):
    """Read a HAR capture into flat request rows (bounded).

    Returns ``{"rows", "parse_errors", "truncated"}``; a missing or invalid
    file reads as zero rows with a parse error, never an exception.
    """
    cap = max(1, int(cap))
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"rows": [], "parse_errors": 1, "truncated": False}
    entries = []
    if isinstance(data, dict):
        log = data.get("log")
        if isinstance(log, dict) and isinstance(log.get("entries"), list):
            entries = log["entries"]
    errors = 0
    rows = []
    for entry in entries[:cap]:
        if not isinstance(entry, dict):
            errors += 1
            continue
        req = entry.get("request")
        resp = entry.get("response")
        req = req if isinstance(req, dict) else {}
        resp = resp if isinstance(resp, dict) else {}
        content = resp.get("content")
        content = content if isinstance(content, dict) else {}
        url = req.get("url") if isinstance(req.get("url"), str) else ""
        parts = urlsplit(url)
        host = parts.netloc
        if "@" in host:
            # A capture URL often carries user:pass@. Do not persist
            # credentials into the instance's data layer.
            host = host.rsplit("@", 1)[1]
            url = url.replace(parts.netloc, host, 1)
        rows.append({
            "ts": _epoch(entry.get("startedDateTime")),
            "method": (req.get("method") or "")[:16],
            "url": url[:2000],
            "host": host[:200],
            "path": (parts.path or "/")[:1000],
            "status": _int(resp.get("status")),
            "mime": (content.get("mimeType") or "")[:100],
            "size": _int(content.get("size", resp.get("bodySize"))),
            "time_ms": _float(entry.get("time")),
            "error": str(entry.get("_error") or "")[:200],
        })
    return {"rows": rows, "parse_errors": errors,
            "truncated": len(entries) > cap}
