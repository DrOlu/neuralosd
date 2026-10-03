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
            "sheets": order, "columns": _annotate(sheets[primary]["columns"]),
            "rows": sheets[primary]["rows"], "sheet_info": sheets}


_ID_HINTS = ("row id", "postal", "zip", " id", "id ", "_id", "code", "sku",
             "phone", "fax")


def _is_measure(col):
    """Numeric, and not an identifier / code / year-like field."""
    if col.get("type") not in ("int", "float"):
        return False
    low = f" {str(col.get('name', '')).lower()} "
    return not any(h in low for h in _ID_HINTS)


def _is_dimension(col):
    """Categorical with few enough values to be useful as a group-by key."""
    card = col.get("card")
    return (col.get("type") in ("str", "bool")
            and card is not None and 2 <= card <= 100)


def _annotate(columns):
    """Attach date_column / dimensions / measures to a profiled column list."""
    date_col = next((c["name"] for c in columns if c.get("type") == "date"), None)
    dims = []
    if date_col:
        dims += ["year", "quarter", "month"]       # derived from the date column
    dims += [c["name"] for c in columns if _is_dimension(c)]
    measures = [c["name"] for c in columns if _is_measure(c)]
    for c in columns:
        c["dimensions"] = dims
        c["measures"] = measures
        c["date_column"] = date_col
    return columns


def _looks_like_date(values):
    import datetime
    seen = 0
    for v in values[:30]:
        t = str(v).strip()
        if not t:
            continue
        seen += 1
        if not any(sep in t for sep in ("-", "/")):
            return False       # a bare year/number is not a date column
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y",
                    "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                datetime.datetime.strptime(t[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return False
    return seen > 0


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
        if infer == "str" and _looks_like_date(values):
            infer = "date"
        columns.append({"name": col, "type": infer,
                        "card": len({v for v in values if v not in (None, "")})})
    return {"kind": "delimited", "path": os.path.abspath(path),
            "columns": _annotate(columns), "rows": len(rows), "delim": delim}


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
    date_column = ""
    for c in (info.get("columns") or []):
        if c.get("date_column"):
            date_column = c["date_column"]
            break
    bridge = f'''"""Data layer for the scaffolded instance — pure python, no model calls."""
import csv
import json
import os

SOURCE = {json.dumps(os.path.abspath(src))}
SHEET = {json.dumps(sheet)}
DATE_COLUMN = {json.dumps(date_column)}
_DATE_PARTS = ("year", "quarter", "month")


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


def _blank(v):
    """A blank cell is MISSING data, not a category called ''. Without this,
    an empty CSV cell becomes its own group and a distinct value."""
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    return v


def count():
    return {{"count": len(rows())}}


def sample(n=10):
    return {{"rows": rows()[:n]}}


# Every probe below reports what it EXCLUDED. Silence is how a wrong answer
# looks correct: a filtered question that quietly drops its filter, or a total
# that counts rows a breakdown did not. rows_in == rows_counted + sum(skipped).

def distinct(column, cap=100):
    vals = [r.get(column) for r in rows()]
    present = [v for v in vals if _blank(v) is not None]
    clean = sorted({{str(v) for v in present}})
    return {{"column": column, "count": len(clean), "distinct": clean[:cap],
            "rows_in": len(vals), "rows_counted": len(present),
            "truncated": max(0, len(clean) - cap),
            "skipped": {{"null": len(vals) - len(present)}}}}


def total(column):
    n_rows = counted = bad = 0
    acc = 0.0
    for r in rows():
        n_rows += 1
        v = _num(r.get(column))
        if v is None:
            bad += 1
            continue
        counted += 1
        acc += v
    return {{"column": column, "sum": round(acc, 2), "n": counted,
            "rows_in": n_rows, "rows_counted": counted,
            "skipped": {{"non_numeric": bad}}}}


def average(column):
    n_rows = counted = bad = 0
    acc = 0.0
    for r in rows():
        n_rows += 1
        v = _num(r.get(column))
        if v is None:
            bad += 1
            continue
        counted += 1
        acc += v
    avg = round(acc / counted, 2) if counted else None
    return {{"column": column, "average": avg, "n": counted,
            "rows_in": n_rows, "rows_counted": counted,
            "skipped": {{"non_numeric": bad}}}}


def _parse_date(v):
    import datetime
    if v is None:
        return None
    if hasattr(v, "year") and hasattr(v, "month"):
        return v
    text = str(v).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def _date_part(value, part):
    d = _parse_date(value)
    if d is None:
        return None
    if part == "year":
        return str(d.year)
    if part == "quarter":
        return f"{{d.year}}-Q{{(d.month - 1) // 3 + 1}}"
    if part == "month":
        return f"{{d.year}}-{{d.month:02d}}"
    return None


def breakdown(dimension, measure):
    """GROUP BY: sum `measure` per `dimension`.

    `dimension` may be a column, or one of the derived date parts (year,
    quarter, month) read from DATE_COLUMN.
    """
    groups = {{}}
    n_rows = counted = miss_dim = miss_meas = 0
    grand = 0.0
    has_grand = False
    for r in rows():
        n_rows += 1
        value = _num(r.get(measure))
        if value is not None:
            grand += value
            has_grand = True
        if dimension in _DATE_PARTS:
            key = _date_part(r.get(DATE_COLUMN), dimension) if DATE_COLUMN else None
        else:
            key = _blank(r.get(dimension))
        if key is None:
            miss_dim += 1
            continue
        if value is None:
            miss_meas += 1
            continue
        counted += 1
        key = str(key)
        groups[key] = groups.get(key, 0.0) + value
    ordered = sorted(groups.items())
    total = round(sum(groups.values()), 2)
    skipped = {{}}
    if miss_dim:
        skipped["missing_" + dimension] = miss_dim
    if miss_meas:
        skipped["non_numeric_" + measure] = miss_meas
    out = {{"by": dimension, "measure": measure, "groups": len(ordered),
            "rows": [{{dimension: k, measure: round(v, 2)}} for k, v in ordered],
            "total": total, "rows_in": n_rows, "rows_counted": counted}}
    if skipped:
        out["skipped"] = skipped
    # Cross-check the parts against the whole. These differ EXACTLY when rows
    # were dropped for a missing dimension — which used to happen silently,
    # producing a breakdown that added up internally and disagreed with the
    # grand total by 50%.
    if has_grand:
        g = round(grand, 2)
        out["grand_total"] = g
        out["unaccounted"] = round(g - total, 2)
    return out
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

    # group-by: one probe, caged args, so both the router and the model can
    # fill "by <dimension>" and pick a measure.
    dims, measures = [], []
    for c in cols:
        dims = c.get("dimensions") or dims
        measures = c.get("measures") or measures
        if dims and measures:
            break
    if dims and measures:
        date_col = next((c.get("date_column") for c in cols
                         if c.get("date_column")), None)
        triggers = ["breakdown", "break down", "broken down", "breakdown by",
                    "split by", "group by", "per", "by", "trend", "over time"]
        for d in dims:
            triggers += [f"by {d}", f"per {d}", f"{d} breakdown",
                         f"breakdown by {d}"]
            # "year over year" is a real phrase. "region over region" is not,
            # and a trigger that can never match a real question just dilutes
            # the menu. Inlined rather than referencing the bridge template's
            # own _DATE_PARTS, which lives in the GENERATED file's namespace.
            if d in ("year", "quarter", "month"):
                triggers.append(f"{d} over {d}")
        if date_col:
            triggers += ["year over year", "over the years", "by date"]
        lines += [
            '@probe(description="Break a measure down by a dimension '
            '(group by, e.g. sales by year or revenue by region)",',
            '       triggers=' + repr(sorted(set(triggers))) + ',',
            '       args={"dimension": {"type": "enum", "values": '
            + repr(dims) + '},',
            '             "measure": {"type": "enum", "values": '
            + repr(measures) + ', "required": False,',
            '                         "default": ' + repr(measures[0]) + '}})',
            "def breakdown(dimension, measure):",
            "    return bridge.breakdown(dimension, measure)",
            "",
        ]
        probe_names.append("breakdown")

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