"""`neuralosd init` — scaffold a working instance from a data source.

Supported sources (self-contained, no external scripts):
  *.csv / *.tsv   — column profiled; row-count / list / filter probes generated
  *.json / *.jsonl— key profiled; count / list / lookup probes generated
  anything else   — a contract template the user fills in
"""
import json
import os
import sys


def run(a):
    src = a.source
    out = os.path.abspath(a.out or f"./{a.name}")
    os.makedirs(out, exist_ok=True)

    if src.endswith((".csv", ".tsv")):
        info = _profile_delimited(src)
    elif src.endswith((".json", ".jsonl")):
        info = _profile_json(src)
    else:
        info = {"kind": "opaque", "columns": [], "rows": None}

    _write_bridge(out, src, info)
    _write_probes(out, a.name, info)
    _write_readme(out, a.name, src, info)

    print(f"instance '{a.name}' scaffolded at {out}")
    print(f"  probes.py     — {len(info.get('columns', []))} column(s) profiled")
    print(f"  bridge.py     — pure-python data layer")
    print(f"  README.md     — next steps")
    print(f"\ntry:  neuralosd ask --instance-dir {out} \"how many rows\"")


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
        columns.append({"name": col, "type": infer})
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


def _write_bridge(out, src, info):
    bridge = f'''"""Data layer for the scaffolded instance — pure python, no model calls."""
import csv
import json
import os

SOURCE = {json.dumps(os.path.abspath(src))}


def rows():
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


def count():
    return {{"count": len(rows())}}


def sample(n=10):
    return {{"rows": rows()[:n]}}
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
        '       triggers=["how many rows", "count", "row count", "size"])',
        "def row_count():",
        "    return bridge.count()",
        "",
        '@probe(description="Show a sample of rows",',
        '       triggers=["show rows", "list rows", "sample", "example rows"])',
        "def list_rows():",
        "    return bridge.sample(20)",
        "",
    ]

    for col in cols[:8]:
        cname = col["name"]
        safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in cname).lower()
        lines += [
            f'@probe(description="List distinct values for column {cname}",',
            f'       triggers=["{cname} values", "distinct {cname}",',
            f'                 "what {cname}", "list {cname}"])',
            f"def distinct_{safe}():",
            f'    values = {{r.get("{cname}") for r in bridge.rows()}}',
            f'    return {{"column": "{cname}", "distinct": sorted(v for v in values if v is not None)[:100],',
            f'            "count": len(values)}}',
            "",
        ]

    probe_names = ["row_count", "list_rows"] + [
        "distinct_" + "".join(ch if ch.isalnum() or ch == "_" else "_"
                              for ch in c["name"]).lower()
        for c in cols[:8]]
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