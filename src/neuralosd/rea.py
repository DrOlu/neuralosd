"""rea evidence-bundle connector — answer questions over a reverse-engineering
export without a live rea session.

`rea` (https://github.com/morluto/rea) can export an evidence bundle: a JSON
object whose sections are lists of records (`findings`, `functions`,
`strings`, `imports`, `artifacts`, …), or a plain list of typed records.
This reader flattens ONE section into rows, so a neuralosd instance can
answer "how many high findings", "which imports", "list functions" offline.

    neuralosd init --source rea:bundle.json --name rea
    neuralosd init --source rea:bundle.json#findings --name findings

The section is part of the source reference; when omitted the first known
section with rows wins. Unknown shapes are read as a single `items` section.
"""
import json

KNOWN_SECTIONS = ("findings", "functions", "imports", "strings",
                  "artifacts", "items")


def _sections(data):
    if isinstance(data, list):
        return {"items": data}
    if not isinstance(data, dict):
        return {}
    found = {k: v for k, v in data.items() if isinstance(v, list)}
    if not found:
        for key in ("items", "results", "records", "data"):
            if isinstance(data.get(key), list):
                found = {key: data[key]}
                break
    return found


def rea_rows(path, section: str = None, cap: int = 5000):
    """Read one section of a rea bundle into rows (bounded).

    Returns ``{"section", "rows", "sections", "parse_errors", "truncated"}``.
    A missing/invalid file reads as zero rows with a parse error.
    """
    cap = max(1, int(cap))
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"section": section or "", "rows": [], "sections": {},
                "parse_errors": 1, "truncated": False}
    sections = _sections(data)
    counts = {k: len(v) for k, v in sections.items()}
    chosen = section if section in sections else None
    if chosen is None:
        for name in KNOWN_SECTIONS:
            if sections.get(name):
                chosen = name
                break
    if chosen is None:
        chosen = next((k for k, v in sections.items() if v), "items")
    records = sections.get(chosen, [])
    errors = 0
    rows = []
    for item in records[:cap]:
        if isinstance(item, dict):
            rows.append({**item, "section": chosen})
        else:
            rows.append({"value": item, "section": chosen})
    return {"section": chosen, "rows": rows, "sections": counts,
            "parse_errors": errors, "truncated": len(records) > cap}
