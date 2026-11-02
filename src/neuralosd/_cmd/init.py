"""`neuralosd init` — scaffold a working instance from a data source.

Supported sources (self-contained, no external scripts):
  *.csv / *.tsv    — column profiled; count / list / distinct / sum probes
  *.xlsx / *.xlsm  — Excel workbook profiled (needs openpyxl; all sheets)
  *.json / *.jsonl — key profiled; count / list / lookup probes
  *.db / *.sqlite  — SQLite database profiled (stdlib sqlite3; tables,
                     columns, row counts; first table drives the probes)
  syslog:<path>    — syslog text file (RFC3164/RFC5424, stdlib only)
  journal:[UNIT]   — journald via `journalctl -o json` (bounded, newest first)
  wevtutil:<CH>    — Windows Event Log channel via `wevtutil ... /f:XML`
  anything else    — a contract template the user fills in
"""
import json
import os
import sys


def profile_any(src):
    """Dispatch a source to the right profiler. Shared with the drift
    re-audit (v2.3.0), which re-profiles the source and checks what broke."""
    if src.endswith((".xlsx", ".xlsm")):
        return _profile_xlsx(src)
    if src.endswith((".csv", ".tsv")):
        return _profile_delimited(src)
    if src.endswith((".json", ".jsonl")):
        return _profile_json(src)
    if src.endswith((".db", ".sqlite", ".sqlite3")):
        return _profile_sqlite(src)
    if src.startswith(("journal:", "journalctl:")):
        return _profile_journal(src)
    if src.startswith(("wevt:", "wevtutil:")):
        return _profile_wevt(src)
    if src.startswith("syslog:"):
        return _profile_syslog(src)
    return {"kind": "opaque", "columns": [], "rows": None}


def run(a):
    src = a.source
    out = os.path.abspath(a.out or f"./{a.name}")
    os.makedirs(out, exist_ok=True)

    info = profile_any(src)

    _write_bridge(out, src, info)
    _write_probes(out, a.name, info)
    _write_banks(out, info)
    _write_readme(out, a.name, src, info)

    n = len(info.get("columns", []))
    print(f"instance '{a.name}' scaffolded at {out}")
    print(f"  bridge.py     — pure-python data layer ({info.get('kind')})")
    print(f"  probes.py     — {n} column(s) profiled")
    if info.get("sheets"):
        print(f"  sheets        — {', '.join(info['sheets'])}")
    if info.get("tables"):
        print("  tables        — " + ", ".join(
            f"{t} ({v['rows']} rows)"
            for t, v in info["tables"].items()))
    if info.get("kind") == "xlsx":
        nr = info.get("named_ranges") or {}
        if nr:
            print(f"  named ranges  — {', '.join(sorted(nr))}")
        fc = sum(v.get("formulas", 0) for v in
                 (info.get("sheet_info") or {}).values())
        if fc:
            print(f"  formulas      — {fc} across "
                  f"{len(info.get('sheet_info') or {})} sheet(s)")
    if info.get("log"):
        print(f"  events        — {info.get('rows')} entries "
              f"(cap {info.get('cap')}, newest first)")
        if info.get("parse_errors"):
            print(f"  note          — {info['parse_errors']} unparsable "
                  "line(s) skipped (recorded in bridge.py LOG_INTEGRITY)")
    print("  golden.json   — positive bank (fill from verified sessions)")
    print("  traps.json    — refusal bank (what this instance must NOT answer)")
    print("  README.md     — next steps")
    print(f"\ntry:  neuralosd ask --instance-dir {out} \"how many rows\"")

    # Prevention: a bridge import the frozen binary can't satisfy fails at
    # runtime with (at best) an ImportError and (at worst) silence. Catch it
    # at BIRTH — non-fatal here, loud in the report.
    try:
        from .instance_audit import audit_instance, current_runtime_importable, \
            collect_instance_imports, print_report
        mods = collect_instance_imports(out)
        bundled = current_runtime_importable(mods)
        rep = audit_instance(out, bundled, set(bundled))
        if not rep["ok"]:
            print("\n⚠ import audit — this instance will NOT load in the frozen "
                  "binary until fixed:\n")
            print(print_report({"instances": [rep]}))
            print("\n  fix: install the modules into the sidecar "
                  "(neuralosd sidecar --setup --with <mods>) or set NEURALOSD_SIDECAR")
    except Exception:                                    # noqa: BLE001
        pass                                             # never block scaffolding


def _write_banks(out, info):
    """Ship behavioural banks with EVERY instance.

    golden.json: positive cases, pre-filled with the probes whose routing is
    unambiguous at generation time. Fill the rest from verified sessions.

    traps.json: refusal cases. A generated instance cannot know its real traps,
    but it CAN know that a nonsense question must refuse - and a bank that
    starts empty stays empty, so two safe smoke traps ship in it.

    These banks are what `neuralosd calibrate` fits thresholds from and what a
    menu-changing install replays before it will complete.
    """
    probe_src = ""
    probes_py = os.path.join(out, "probes.py")
    if os.path.isfile(probes_py):
        with open(probes_py, encoding="utf-8") as fh:
            probe_src = fh.read()

    positives = []
    if "def row_count" in probe_src:
        positives.append({"q": "how many rows", "expect_probe": "row_count"})
    if "def list_rows" in probe_src:
        positives.append({"q": "show me some rows", "expect_probe": "list_rows"})

    golden = {
        "_comment": "Positive bank. Every item must route to expect_probe. "
                    "Fill from verified sessions and run: "
                    "neuralosd golden --dir .",
        "items": positives,
    }
    with open(os.path.join(out, "golden.json"), "w", encoding="utf-8") as fh:
        json.dump(golden, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    traps = {
        "_comment": "Refusal bank. A trap PASSES only when the router refuses. "
                    "A confident answer to a trap is the worst failure this "
                    "system has. Add one trap per capability you deliberately "
                    "did NOT build.",
        "items": [
            {"q": "xyzzy plugh", "expect_refusal": True,
             "refusal_reason": "no_probe_matches",
             "note": "nonsense must never route"},
            {"q": "how many rows are blocked", "expect_refusal": True,
             "refusal_reason": "dropped_filter",
             "note": "'blocked' is a status this instance cannot filter on"},
        ],
    }
    with open(os.path.join(out, "traps.json"), "w", encoding="utf-8") as fh:
        json.dump(traps, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _profile_xlsx(path):
    """Profile an Excel workbook with openpyxl. First sheet drives the menu."""
    try:
        import openpyxl
    except ImportError:
        raise SystemExit(
            "error: reading .xlsx needs openpyxl.\n"
            "    pip install openpyxl       # or: pip install 'neuralosd[data]'")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    formulas = _profile_formulas(path, wb.sheetnames)
    named_ranges = _named_ranges(wb)
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

    for t, f in formulas.items():
        if t in sheets:
            sheets[t]["formulas"] = f["formulas"]
            sheets[t]["formula_samples"] = f["samples"]

    primary = order[0]
    return {"kind": "xlsx", "path": os.path.abspath(path), "sheet": primary,
            "sheets": order, "columns": _annotate(sheets[primary]["columns"]),
            "rows": sheets[primary]["rows"], "sheet_info": sheets,
            "named_ranges": named_ranges,
            "formula_count": formulas.get(primary, {}).get("formulas", 0)}


def _profile_formulas(path, sheetnames):
    """Count formula cells per sheet (data_only=False load shows the formula
    strings; the data_only load shows cached values). Sample up to 5."""
    import openpyxl
    formulas = {t: {"formulas": 0, "samples": []} for t in sheetnames}
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        for ws in wb.worksheets:
            count = 0
            samples = []
            for row in ws.iter_rows():
                for cell in row:
                    v = cell.value
                    if isinstance(v, str) and v.startswith("="):
                        count += 1
                        if len(samples) < 5:
                            samples.append(v)
            if ws.title in formulas:
                formulas[ws.title] = {"formulas": count, "samples": samples}
    finally:
        wb.close()
    return formulas


def _named_ranges(wb):
    """Workbook defined names -> {name: target}. Tolerates the pre-3.1
    openpyxl shape (a .definedName collection) defensively."""
    out = {}
    try:
        dns = wb.defined_names
    except AttributeError:
        return out
    try:
        for k, v in dns.items():
            out[str(k)] = getattr(v, "value", "") or getattr(v, "attr_text",
                                                             "") or ""
    except AttributeError:
        try:
            for d in dns.definedName:
                out[str(d.name)] = d.value or ""
        except Exception:
            return out
    return out


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


def _profile_sqlite(path):
    """Profile a SQLite database with stdlib sqlite3 — zero new deps, which
    is the whole point: the bundled-sqlite3 story means a frozen neuralosd
    binary can profile a .db file anywhere.

    Every table is profiled (columns, declared types, row counts); the FIRST
    table in creation order is the primary and drives the column-driven
    probes, exactly the scaffold a single-sheet workbook gets. All table
    names ship in the generated probes.py so the menu documents the schema.
    """
    import sqlite3
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")]
        tables = {}
        for t in names:
            info_rows = list(conn.execute(f'PRAGMA table_info("{t}")'))
            sample = [tuple(r) for r in conn.execute(
                f'SELECT * FROM "{t}" LIMIT 500')]
            n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            cols = []
            for i, cid in enumerate(info_rows):
                cname, decl = cid[1], (cid[2] or "").upper()
                if decl.startswith(("INT", "BIGINT", "SMALLINT", "TINYINT")):
                    infer = "int"
                elif decl.startswith(("REAL", "FLOA", "DOUB", "NUM",
                                      "DEC")):
                    infer = "float"
                else:
                    vals = [r[i] for r in sample
                            if i < len(r) and r[i] is not None]
                    infer = _infer_type([str(v) for v in vals])
                seen = {r[i] for r in sample if i < len(r)
                        and r[i] is not None}
                cols.append({"name": cname, "type": infer,
                             "card": len(seen)})
            tables[t] = {"columns": _annotate(cols), "rows": n}
    finally:
        conn.close()
    primary = names[0] if names else ""
    return {"kind": "sqlite", "path": os.path.abspath(path),
            "table": primary, "tables": tables,
            "columns": tables[primary]["columns"] if primary else [],
            "rows": tables[primary]["rows"] if primary else 0,
            "total_rows": sum(v["rows"] for v in tables.values())}


# Injectable for tests (and exotic deployments): set init._capture to a
# callable (cmd, cap) -> stdout to run a log source's capture from fixtures.
_capture = None


def _capture_cmd(cmd, cap):
    if _capture is not None:
        return _capture(cmd, cap)
    import subprocess
    from ..logsources import decode_tool_output
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


def _log_info(kind, rows, cap, columns, parse_errors=0, truncated=False,
              **extra):
    """Common profile shape for log sources. `rows` are the normalized event
    dicts from neuralosd.logsources; a parse error or a capped read is
    RECORDED, never silent."""
    from ..logsources import log_cap_default
    cols = []
    for name in columns:
        vals = [str(r.get(name)) for r in rows[:500]
                if r.get(name) not in (None, "")]
        cols.append({"name": name,
                     "type": "date" if name == "ts" else "str",
                     "card": len(set(vals))})
    info = {"kind": kind, "log": True,
            "columns": _annotate(cols), "rows": len(rows),
            "cap": cap or log_cap_default(),
            "parse_errors": parse_errors, "truncated": truncated}
    info.update(extra)
    return info


def _profile_journal(src):
    from ..logsources import journal_rows, log_cap_default
    unit = src.split(":", 1)[1].strip() or None
    cap = log_cap_default()
    rows = journal_rows(cap=cap, unit=unit, runner=_capture_cmd)
    return _log_info("journald", rows, cap,
                     ("ts", "host", "app", "level", "unit", "message"),
                     unit=unit or "")


def _profile_wevt(src):
    from ..logsources import wevt_rows, log_cap_default
    channel = src.split(":", 1)[1].strip()
    cap = log_cap_default()
    rows = wevt_rows(channel, cap=cap, runner=_capture_cmd)
    return _log_info("wevtutil", rows, cap,
                     ("ts", "host", "app", "level", "event_id", "message"),
                     channel=channel)


def _profile_syslog(src):
    from ..logsources import parse_syslog_file, log_cap_default
    path = os.path.abspath(src.split(":", 1)[1].strip())
    cap = log_cap_default()
    parsed = parse_syslog_file(path, cap=cap)
    return _log_info("syslog", parsed["rows"], cap,
                     ("ts", "host", "app", "level", "message"),
                     parse_errors=parsed["parse_errors"],
                     truncated=parsed["truncated"], path=path)


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
    table = info.get("table") or ""
    kind = info.get("kind") or "opaque"
    date_column = ""
    for c in (info.get("columns") or []):
        if c.get("date_column"):
            date_column = c["date_column"]
            break
    bridge = f'''"""Data layer for the scaffolded instance — pure python, no model calls."""
import csv
import json
import os
import re

SOURCE = {json.dumps(os.path.abspath(src))}
SHEET = {json.dumps(sheet)}
TABLE = {json.dumps(table)}
KIND = {json.dumps(kind)}
DATE_COLUMN = {json.dumps(date_column)}
_DATE_PARTS = ("year", "quarter", "month")


def _sqlite_rows():
    import sqlite3
    conn = sqlite3.connect("file:" + SOURCE + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM "' + TABLE + '"')]
    finally:
        conn.close()


def _xlsx_rows():
    import openpyxl
    wb = openpyxl.load_workbook(SOURCE, read_only=True, data_only=True)
    ws = wb[SHEET] if SHEET and SHEET in wb.sheetnames else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [str(h) if h is not None else f"col{{i}}"
              for i, h in enumerate(next(it, []))]
    out = []
    for r in it:
        # row 1 was consumed as the header — data starts at row 2, exactly
        # like the CSV path (a header row is not a data point).
        if all(v is None for v in r):
            continue
        out.append({{header[i]: (r[i] if i < len(r) else None)
                    for i in range(len(header))}})
    wb.close()
    return out


def rows():
    if KIND in ("journald", "wevtutil", "syslog"):
        return _log_rows()
    if KIND == "sqlite":
        return _sqlite_rows()
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
    if info.get("kind") == "xlsx":
        bridge += _xlsx_bridge_extra(info)
    elif info.get("log"):
        bridge += _log_bridge_extra(info)

    with open(os.path.join(out, "bridge.py"), "w", encoding="utf-8") as f:
        f.write(bridge)


def _xlsx_bridge_extra(info):
    """Deep-Excel data layer, appended to the generated bridge.py: per-sheet
    stats, named-range resolution and a formula scan."""
    sheet_info = {t: {"rows": v.get("rows"),
                      "columns": len(v.get("columns") or []),
                      "formulas": v.get("formulas", 0)}
                  for t, v in (info.get("sheet_info") or {}).items()}
    si = json.dumps(sheet_info, ensure_ascii=False)
    nr = json.dumps(info.get("named_ranges") or {}, ensure_ascii=False)
    return (
        "\n\n"
        "SHEET_INFO = " + si + "\n"
        "NAMED_RANGES = " + nr + "\n"
        "\n"
        "\n"
        "def sheet_names():\n"
        "    return {\"sheets\": [dict(sheet=s, **v)\n"
        "                         for s, v in SHEET_INFO.items()]}\n"
        "\n"
        "\n"
        "def _resolve_ref(ws, ref):\n"
        "    from openpyxl.utils import column_index_from_string\n"
        "    m = re.match(r\"^\\$?([A-Z]+)\\$?(\\d+)(?::\\$?([A-Z]+)\\$?(\\d+))?$\", ref)\n"
        "    if not m:\n"
        "        raise ValueError(\"unsupported range: \" + ref)\n"
        "    c1 = column_index_from_string(m.group(1))\n"
        "    r1 = int(m.group(2))\n"
        "    c2 = column_index_from_string(m.group(3)) if m.group(3) else c1\n"
        "    r2 = int(m.group(4)) if m.group(4) else r1\n"
        "    for row in ws.iter_rows(min_row=r1, max_row=r2, min_col=c1,\n"
        "                            max_col=c2, values_only=True):\n"
        "        for v in row:\n"
        "            yield v\n"
        "\n"
        "\n"
        "def named_ranges():\n"
        "    \"\"\"Every defined name with its target and a bounded value preview.\n"
        "\n"
        "    A range that cannot be resolved reports the error on the entry — a\n"
        "    named range that resolves to nothing is a finding, not a silence.\n"
        "    \"\"\"\n"
        "    import openpyxl\n"
        "    out = {}\n"
        "    if not NAMED_RANGES:\n"
        "        return {\"named_ranges\": out}\n"
        "    wb = openpyxl.load_workbook(SOURCE, read_only=True)\n"
        "    try:\n"
        "        for name, target in NAMED_RANGES.items():\n"
        "            entry = {\"target\": target}\n"
        "            try:\n"
        "                sheet_part, ref = target.rsplit(\"!\", 1)\n"
        "                ws = wb[sheet_part.strip(\"'\")]\n"
        "                cells = []\n"
        "                for v in _resolve_ref(ws, ref.replace(\"$\", \"\")):\n"
        "                    cells.append(v)\n"
        "                    if len(cells) >= 100:\n"
        "                        break\n"
        "                entry[\"cells\"] = len(cells)\n"
        "                entry[\"preview\"] = cells[:10]\n"
        "            except Exception as e:\n"
        "                entry[\"error\"] = str(e)\n"
        "            out[name] = entry\n"
        "    finally:\n"
        "        wb.close()\n"
        "    return {\"named_ranges\": out}\n"
        "\n"
        "\n"
        "def formula_scan(sheet=None):\n"
        "    import openpyxl\n"
        "    wb = openpyxl.load_workbook(SOURCE, read_only=True)\n"
        "    try:\n"
        "        targets = [sheet] if sheet else list(SHEET_INFO)\n"
        "        by_sheet = {}\n"
        "        samples = []\n"
        "        for s in targets:\n"
        "            count = 0\n"
        "            for row in wb[s].iter_rows():\n"
        "                for cell in row:\n"
        "                    v = cell.value\n"
        "                    if isinstance(v, str) and v.startswith(\"=\"):\n"
        "                        count += 1\n"
        "                        if len(samples) < 5:\n"
        "                            samples.append(v)\n"
        "            by_sheet[s] = count\n"
        "    finally:\n"
        "        wb.close()\n"
        "    return {\"sheet\": sheet or \"__all__\",\n"
        "            \"formulas\": sum(by_sheet.values()),\n"
        "            \"samples\": samples, \"by_sheet\": by_sheet}\n")


def _log_bridge_extra(info):
    """Log-source data layer, appended to the generated bridge.py: reads the
    live log (bounded, newest first) at ask time via neuralosd.logsources."""
    cap = info.get("cap") or 10000
    kind = info.get("kind")
    # repr(), not json.dumps(): these constants are PYTHON literals — json
    # booleans ("false") are a NameError in the generated bridge.
    header = ("\n\n"
              "LOG_CAP = " + repr(int(cap)) + "\n"
              "LOG_INTEGRITY = " + repr(
                  {"parse_errors": info.get("parse_errors", 0),
                   "truncated": bool(info.get("truncated"))}) + "\n")
    if kind == "journald":
        return (header
                + "LOG_UNIT = " + repr(info.get("unit") or "") + "\n"
                + "\n\n"
                + "def _log_rows():\n"
                + "    from neuralosd.logsources import journal_rows\n"
                + "    return journal_rows(cap=LOG_CAP,\n"
                + "                        unit=LOG_UNIT or None)\n")
    if kind == "wevtutil":
        return (header
                + "LOG_CHANNEL = " + repr(info.get("channel") or "")
                + "\n\n\n"
                + "def _log_rows():\n"
                + "    from neuralosd.logsources import wevt_rows\n"
                + "    return wevt_rows(LOG_CHANNEL, cap=LOG_CAP)\n")
    # syslog: static text file — the SOURCE is the log path
    return (header
            + "\n\n"
            + "def _log_rows():\n"
            + "    from neuralosd.logsources import parse_syslog_file\n"
            + "    parsed = parse_syslog_file("
            + repr(info.get("path") or "") + ", cap=LOG_CAP)\n"
            + "    return parsed[\"rows\"]\n")




def _write_probes(out, name, info):
    cols = info.get("columns", [])
    lines = [f'"""@{name} instance — generated probes."""',
             "from neuralosd import probe",
             "import bridge",
             ""]
    tables = info.get("tables") or {}
    if tables:
        # The menu documents the whole schema, not just the primary table.
        lines.append("# Source tables: " + ", ".join(
            f"{t} ({v['rows']} rows)" for t, v in tables.items()))
        lines.append(f"TABLES = {sorted(tables)!r}")
        lines.append("")

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

    # deep-Excel probes: sheets, named ranges, formula scan (v2.2.0)
    if info.get("kind") == "xlsx" and info.get("sheets"):
        sheet_names = list(info["sheets"])
        lines += [
            '@probe(description="List the workbook sheets with row, column '
            'and formula counts",',
            '       triggers=["list sheets", "sheets", "sheet names",',
            '                 "what sheets", "tabs", "which sheets"])',
            "def sheets():",
            "    return bridge.sheet_names()",
            "",
        ]
        probe_names.append("sheets")
        if info.get("named_ranges"):
            nr = ", ".join(sorted(info["named_ranges"]))
            lines += [
                '@probe(description="List the workbook named ranges '
                f'({nr}) with a value preview",',
                '       triggers=["named ranges", "named range",',
                '                 "defined names", "range names",',
                '                 "list named ranges", "named ranges list"])',
                "def named_ranges():",
                "    return bridge.named_ranges()",
                "",
            ]
            probe_names.append("named_ranges")
        ftrig = ["formula scan", "formulas", "scan formulas",
                 "find formulas", "formula audit", "formulas in"]
        ftrig += [f"formulas in {s.lower()}" for s in sheet_names]
        ftrig += [f"{s.lower()} formulas" for s in sheet_names]
        lines += [
            '@probe(description="Scan a sheet for formulas (count + samples)",',
            "       triggers=" + repr(sorted(set(ftrig))) + ",",
            '       args={"sheet": {"type": "enum", "values": '
            + repr(sheet_names) + ', "required": False}})',
            "def formula_scan(sheet):",
            "    return bridge.formula_scan(sheet)",
            "",
        ]
        probe_names.append("formula_scan")

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