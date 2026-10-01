"""`neuralosd init` — scaffold a working instance from a data source.

Supported sources (self-contained, no external scripts):
  *.csv / *.tsv    — column profiled; count / list / distinct / sum probes
  *.xlsx / *.xlsm  — Excel workbook profiled (needs openpyxl; all sheets)
  *.json / *.jsonl — key profiled; count / list / lookup probes
  anything else    — a contract template the user fills in
"""
import json
import os
import sys


def run(a):
    src = a.source
    out = os.path.abspath(a.out or f"./{a.name}")
    os.makedirs(out, exist_ok=True)

    if src.endswith((".xlsx", ".xlsm")):
        info = _profile_xlsx(src)
    elif src.endswith((".csv", ".tsv")):
        info = _profile_delimited(src)
    elif src.endswith((".json", ".jsonl")):
        info = _profile_json(src)
    else:
        info = {"kind": "opaque", "columns": [], "rows": None}

    _write_bridge(out, src, info)
    _write_probes(out, a.name, info)
    _write_readme(out, a.name, src, info)

    n = len(info.get("columns", []))
    print(f"instance '{a.name}' scaffolded at {out}")
    print(f"  bridge.py     — pure-python data layer ({info.get('kind')})")
    print(f"  probes.py     — {n} column(s) profiled")
    if info.get("sheets"):
        print(f"  sheets        — {', '.join(info['sheets'])}")
    print("  README.md     — next steps")
    print(f"\ntry:  neuralosd ask --instance-dir {out} \"how many rows\"")


def _profile_xlsx(path):
    """Profile an Excel workbook with openpyxl. First sheet drives the menu."""
    try:
        import openpyxl
    except ImportError:
        raise SystemExit(
            "error: reading .xlsx needs openpyxl.\n"
            "    pip install openpyxl       # or: pip install 'neuralosd[data]'")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {}
    order = []
    for ws in wb.worksheets:
        order.append(ws.title)
        it = ws.iter_rows(values_only=True)
        header = [str(h) if h is not None else f"col{i}"
                  for i, h in enumerate(next(it, []))]
        sample = []
        n = 0
        for r in it:
            n += 1
            if len(sample) < 500:
                sample.append(r)
        cols = []
        for i, name in enumerate(header):
            vals = [r[i] for r in sample if i < len(r) and r[i] is not None]
            cols.append({"name": name, "type": _infer_type(vals),
                         "card": len(set(vals))})
        sheets[ws.title] = {"columns": cols, "rows": n}
    wb.close()

    primary = order[0]
    return {"kind": "xlsx", "path": os.path.abspath(path), "sheet": primary,
            "sheets": order, "columns": sheets[primary]["columns"],
            "rows": sheets[primary]["rows"], "sheet_info": sheets}


def _infer_type(values):
    if not values:
        return "str"
    sample = values[:500]
    if all(isinstance(v, bool) for v in sample):
        return "bool"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in sample):
        return "int"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool)
           for v in sample):
        return "float"
    if all(hasattr(v, "year") for v in sample):
        return "date"
    if all(_is_int(v) for v in sample):
        return "int"
    if all(_is_float(v) for v in sample):
        return "float"
    return "str"


def _profile_delimited(path):
    import csv
    delim = "\t" if path.endswith(".tsv") else ","
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f, delimiter=delim)
        header = next(reader, [])
        rows = [r for r in reader]
    columns = []
    for i, col in enumerate(header):
        values = [r[i] for r in rows if i < len(r)][:1000]
        infer = "int" if values and all(_is_int(v) for v in values) else (
            "float" if values and all(_is_float(v) for v in values) else "str")
        columns.append({"name": col, "type": infer,
                        "card": len({v for v in values if v not in (None, "")})})
    return {"kind": "delimited", "path": os.path.abspath(path),
            "columns": columns, "rows": len(rows), "delim": delim}


def _profile_json(path):
    rows = []
    if path.endswith(".jsonl"):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    else:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        rows = data if isinstance(data, list) else [data]
    keys = []
    if rows and isinstance(rows[0], dict):
        seen = set()
        for r in rows[:1000]:
            if isinstance(r, dict):
                for k in r:
                    if k not in seen:
                        seen.add(k)
                        sample = r.get(k)
                        keys.append({"name": k,
                                     "type": "int" if isinstance(sample, int)
                                     else "float" if isinstance(sample, float)
                                     else "bool" if isinstance(sample, bool)
                                     else "str"})
    return {"kind": "json", "path": os.path.abspath(path),
            "columns": keys, "rows": len(rows)}


def _safe(name):
    return "".join(ch if ch.isalnum() or ch == "_" else "_"
                   for ch in str(name)).lower().strip("_")


def _write_bridge(out, src, info):
    sheet = info.get("sheet") or ""
    bridge = f'''"""Data layer for the scaffolded instance — pure python, no model calls."""
import csv
import json
import os

SOURCE = {json.dumps(os.path.abspath(src))}
SHEET = {json.dumps(sheet)}


def _xlsx_rows():
    import openpyxl
    wb = openpyxl.load_workbook(SOURCE, read_only=True, data_only=True)
    ws = wb[SHEET] if SHEET and SHEET in wb.sheetnames else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [str(h) if h is not None else f"col{{i}}"
              for i, h in enumerate(next(it, []))]
    out = []
    for r in it:
        out.append({{header[i]: (r[i] if i < len(r) else None)
                    for i in range(len(header))}})
    wb.close()
    return out


def rows():
    if SOURCE.endswith((".xlsx", ".xlsm")):
        return _xlsx_rows()
    if SOURCE.endswith((".csv", ".tsv")):
        delim = "\\t" if SOURCE.endswith(".tsv") else ","
        with open(SOURCE, newline="", encoding="utf-8", errors="replace") as f:
            return list(csv.DictReader(f, delimiter=delim))
    r = []
    with open(SOURCE, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                r.append(json.loads(line))
    return r


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def count():
    return {{"count": len(rows())}}


def sample(n=10):
    return {{"rows": rows()[:n]}}


def distinct(column, cap=100):
    values = {{r.get(column) for r in rows()}}
    clean = sorted(str(v) for v in values if v is not None)
    return {{"column": column, "count": len(clean), "distinct": clean[:cap]}}


def total(column):
    vals = [n for n in (_num(r.get(column)) for r in rows()) if n is not None]
    return {{"column": column, "sum": round(sum(vals), 2), "n": len(vals)}}


def average(column):
    vals = [n for n in (_num(r.get(column)) for r in rows()) if n is not None]
    avg = round(sum(vals) / len(vals), 2) if vals else None
    return {{"column": column, "average": avg, "n": len(vals)}}
'''
    with open(os.path.join(out, "bridge.py"), "w", encoding="utf-8") as f:
        f.write(bridge)


def _write_probes(out, name, info):
    cols = info.get("columns", [])
    lines = [f'"""@{name} instance — generated probes."""',
             "from neuralosd import probe",
             "import bridge",
             ""]

    lines += [
        '@probe(description="Count rows in the data source",',
        '       triggers=["how many rows", "count", "row count", "size",',
        '                 "how many records", "dataset size"])',
        "def row_count():",
        "    return bridge.count()",
        "",
        '@probe(description="Show a sample of rows",',
        '       triggers=["show rows", "list rows", "sample", "example rows",',
        '                 "preview the data", "first rows"])',
        "def list_rows():",
        "    return bridge.sample(20)",
        "",
    ]
    probe_names = ["row_count", "list_rows"]

    # categorical columns (few distinct values) -> a distinct-values probe
    for col in cols:
        if col.get("type") not in ("str", "bool"):
            continue
        card = col.get("card")
        if card is None or not (2 <= card <= 100):
            continue
        cname = col["name"]
        low = cname.lower()
        safe = _safe(cname)
        lines += [
            f'@probe(description="List the distinct values of {cname}",',
            f'       triggers=["{low} values", "distinct {low}", "what {low}",',
            f'                 "list {low}", "which {low}", "{low} list"])',
            f"def distinct_{safe}():",
            f'    return bridge.distinct("{cname}")',
            "",
        ]
        probe_names.append(f"distinct_{safe}")

    # numeric columns -> total + average (skip ids, codes, years)
    skip = ("row id", "postal", "id", "code", "zip", "year", "date")
    for col in cols:
        if col.get("type") not in ("int", "float"):
            continue
        cname = col["name"]
        if any(s in cname.lower() for s in skip):
            continue
        low = cname.lower()
        safe = _safe(cname)
        lines += [
            f'@probe(description="Total (sum) of {cname}",',
            f'       triggers=["total {low}", "sum of {low}", "sum {low}",',
            f'                 "{low} total", "overall {low}"])',
            f"def total_{safe}():",
            f'    return bridge.total("{cname}")',
            "",
            f'@probe(description="Average of {cname}",',
            f'       triggers=["average {low}", "mean {low}", "avg {low}"])',
            f"def average_{safe}():",
            f'    return bridge.average("{cname}")',
            "",
        ]
        probe_names += [f"total_{safe}", f"average_{safe}"]

    lines.append("PROBES = [" + ", ".join(probe_names) + "]")
    lines.append("")

    with open(os.path.join(out, "probes.py"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_readme(out, name, src, info):
    readme = f"""# {name}

Scaffolded neuralOS instance from `{src}`.

Kind: **{info.get('kind')}**  ·  rows: **{info.get('rows')}**
Columns: {', '.join(c['name'] for c in info.get('columns', [])) or '(none detected)'}

## Files

| File | Purpose |
|---|---|
| `probes.py` | `@probe` declarations (edit the triggers and add your own) |
| `bridge.py` | Pure-python data layer the probes call |
| `README.md` | This file |

## Use it

```bash
neuralosd ask --instance-dir . "how many rows"
neuralosd ask --instance-dir . "list the <column> values"
neuralosd lint probes.py
neuralosd serve --instance-dir . --port 8877
```

## Next steps

1. Tighten the `triggers=[...]` in `probes.py` to the phrasings your users use.
2. Add domain probes (joins, aggregates) as new `@probe` functions.
3. Add a `golden.json` and run `neuralosd golden --dir .` in CI.
"""
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)


def _is_int(v):
    try:
        int(v)
        return True
    except (ValueError, TypeError):
        return False


def _is_float(v):
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False