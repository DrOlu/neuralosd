#!/usr/bin/env bash
# End-to-end CLI smoke test for neuralosd.
# Usage: bash tests/smoke_cli.sh [path-to-neuralosd-binary]
# Default command is the installed `neuralosd` on PATH.
set -uo pipefail

NS="${1:-neuralosd}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

pass=0; fail=0
check() { # name, expected-substring, actual
  if grep -q "$2" <<<"$3"; then
    echo "  ✓ $1"; pass=$((pass+1))
  else
    echo "  ✗ $1"
    echo "    expected: $2"
    echo "    got:      $(head -c 400 <<<"$3")"
    fail=$((fail+1))
  fi
}
check_not() { # name, forbidden-substring, actual
  if grep -q "$2" <<<"$3"; then
    echo "  ✗ $1"
    echo "    must NOT contain: $2"
    fail=$((fail+1))
  else
    echo "  ✓ $1"; pass=$((pass+1))
  fi
}
run() { # capture stdout+stderr, never abort on non-zero
  "$NS" "$@" 2>&1 || true
}

echo "== neuralosd CLI smoke test ($NS) =="
"$NS" --help >/dev/null 2>&1 || { echo "  ✗ --help failed"; exit 1; }

# 1. init from CSV
cat > sales.csv <<'CSV'
region,product,units,revenue
North,Widget,120,1450.00
South,Gadget,80,960.00
East,Widget,200,2400.00
West,Gadget,45,540.00
CSV
run init --source sales.csv --name sales --out ./inst >/dev/null
if [ -f ./inst/probes.py ]; then echo "  ✓ init created probes.py"; pass=$((pass+1)); else echo "  ✗ init created probes.py"; fail=$((fail+1)); fi

# 2. ask — deterministic routing
r="$(run ask --instance-dir ./inst 'how many rows')"
check "ask: how many rows -> row_count" '"probe": "row_count"' "$r"
check "ask: row_count returns 4" '"count": 4' "$r"

# 2a. init from a SQLite database — profiled, scaffolded, immediately
# askable (v2.1.0: stdlib sqlite3, zero new deps).
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("ledger.db")
con.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, region TEXT, amount REAL)")
con.executemany("INSERT INTO payments (region, amount) VALUES (?, ?)",
                [("North", 100.0), ("South", 200.0), ("North", 50.0)])
con.commit(); con.close()
PY
run init --source ledger.db --name ledger --out ./inst-db >/dev/null
if [ -f ./inst-db/probes.py ]; then echo "  ✓ init (sqlite) created probes.py"; pass=$((pass+1)); else echo "  ✗ init (sqlite) created probes.py"; fail=$((fail+1)); fi
check "init (sqlite) profiled the table" 'payments' "$(cat ./inst-db/probes.py)"
r="$(run ask --instance-dir ./inst-db 'how many rows')"
check "ask (sqlite): how many rows -> 3" '"count": 3' "$r"
check "ask (sqlite): auto-trap banked at birth" 'how many rows row_count' "$(cat ./inst-db/traps.json)"

# 2b. deep Excel — formulas, multi-sheet, named ranges (needs openpyxl)
PYBIN="$(dirname "$NS")/python3"
command -v "$PYBIN" >/dev/null 2>&1 || PYBIN=python3
if "$PYBIN" -c "import openpyxl" 2>/dev/null; then
  "$PYBIN" - <<'PY'
import openpyxl
from openpyxl.workbook.defined_name import DefinedName
wb = openpyxl.Workbook()
ws = wb.active; ws.title = "Ledger"
ws.append(["region", "amount"])
for r in (("North", 100), ("South", 200), ("East", 50), ("West", 25)):
    ws.append(list(r))
ws["B6"] = "=SUM(B2:B5)"
notes = wb.create_sheet("Notes"); notes.append(["memo"]); notes.append(["ok"])
wb.defined_names.add(DefinedName("SalesRange", attr_text="Ledger!$B$2:$B$5"))
wb.save("book.xlsx")
PY
  run init --source book.xlsx --name book --out ./inst-xl >/dev/null
  if [ -f ./inst-xl/probes.py ]; then echo "  ✓ init (xlsx deep) created probes.py"; pass=$((pass+1)); else echo "  ✗ init (xlsx deep) created probes.py"; fail=$((fail+1)); fi
  check "xlsx deep: named range profiled" 'SalesRange' "$(cat ./inst-xl/probes.py)"
  check "xlsx deep: sheets probe scaffolded" 'def sheets' "$(cat ./inst-xl/probes.py)"
  r="$(run ask --instance-dir ./inst-xl 'how many rows')"
  check "xlsx deep: row_count = 4 data rows (header skipped)" '"count": 4' "$r"
  r="$(run ask --instance-dir ./inst-xl 'named ranges')"
  check "xlsx deep: named ranges resolve" 'SalesRange' "$r"
  r="$(run ask --instance-dir ./inst-xl 'formula scan')"
  check "xlsx deep: formula scan finds the SUM" 'SUM' "$r"
else
  echo "  - skip xlsx deep block (no openpyxl in $PYBIN)"
fi

# 2c. syslog source — stdlib only, always available
printf 'Nov  2 03:11:22 web01 sshd[4123]: Accepted publickey for olu\nNov  2 03:12:01 web01 systemd[1]: Started nginx.service.\nNov  2 03:13:44 db01 postgres[900]: checkpoint complete\nnot syslog at all\n' > system.log
# RELATIVE source: init resolves it against its own cwd and embeds the
# absolute path — a literal "$PWD/..." would be an MSYS path the native
# Windows binary cannot open.
run init --source "syslog:system.log" --name syslog --out ./inst-syslog >/dev/null
if [ -f ./inst-syslog/probes.py ]; then echo "  ✓ init (syslog) created probes.py"; pass=$((pass+1)); else echo "  ✗ init (syslog) created probes.py"; fail=$((fail+1)); fi
r="$(run ask --instance-dir ./inst-syslog 'how many rows')"
check "syslog: 3 events parsed, garbage line excluded" '"count": 3' "$r"
check "syslog: parse error recorded, not silent" 'parse_errors' "$(cat ./inst-syslog/bridge.py)"
r="$(run ask --instance-dir ./inst-syslog 'distinct app')"
check "syslog: distinct app routes" '"probe": "distinct_app"' "$r"

# 2e. loop improvements — clustering, feedback, mothball (v2.3.0; stdlib only)
# NOTE: its own instance dir — the loop mutates menus, and ./inst belongs to
# the deterministic-routing checks above.
printf 'region,units\nNorth,100\nSouth,200\n' > loop.csv
run init --source loop.csv --name loop --out ./inst-loop >/dev/null
printf '%s\n' \
 '{"ts":1,"question":"shipping cost per unit","probe":null,"results":null,"refusal_reason":"no_probe_matches"}' \
 '{"ts":2,"question":"what is the shipping cost for each unit","probe":null,"results":null,"refusal_reason":"no_probe_matches"}' \
 '{"ts":3,"question":"how many rows","probe":"row_count","results":[{"count":2}]}' > audit.jsonl
run gaps audit.jsonl --out gaps.json >/dev/null
check "gaps: refusals cluster into a pattern" 'shipping' "$(cat gaps.json)"
check "gaps: cluster carries cost framing" 'refused queries' "$(cat gaps.json)"
run feedback --instance-dir ./inst-loop --question "delete everything" --verdict invalid --by tester >/dev/null
run feedback --instance-dir ./inst-loop --apply >/dev/null
check "feedback: proven refusal promoted to traps" 'delete everything' "$(cat ./inst-loop/traps.json)"
run badge --instance-dir ./inst-loop --out badge.json >/dev/null
check "badge: golden-bank health endpoint" '"label": "golden bank"' "$(cat badge.json)"
# temporal durable activities run WITHOUT a server (plain functions):
r="$(run temporal --run-activity golden_run --instance-dir ./inst-loop)"
check "temporal: golden_run activity executes" '"wrong"' "$r"
run mothball --instance-dir ./inst-loop --age-days 0 --apply >/dev/null
# deterministic invariant: the demoted distinct_region probe must now
# refuse (it left the menu), whatever probe survived as the anchor
r="$(run ask --instance-dir ./inst-loop 'distinct region')"
check "mothball: demoted probe refuses" '"refusal_reason"' "$r"
r="$(run ask --instance-dir ./inst-loop 'how many rows')"
check "mothball: surviving anchor still answers" '"mode": "deterministic"' "$r"

# 2c-bis. multi-source joins with a sqlite extra (v2.8.0)
python3 - <<'PYEOF'
import sqlite3
con = sqlite3.connect("prices.db")
con.execute("CREATE TABLE prices (region TEXT, price REAL)")
con.executemany("INSERT INTO prices VALUES (?, ?)",
                [("North", 10.0), ("South", 20.0)])
con.commit(); con.close()
PYEOF
run init --source sales.csv --extra-source prices.db --on region=region \
    --name priced --out ./inst-priced >/dev/null
if [ -f ./inst-priced/probes.py ]; then echo "  ✓ init (sqlite extra) created probes.py"; pass=$((pass+1)); else echo "  ✗ init (sqlite extra) failed"; fail=$((fail+1)); fi
r="$(run ask --instance-dir ./inst-priced 'joined count')"
check "joins: sqlite extra joins with main" '"count": 2' "$r"
r="$(run ask --instance-dir ./inst-priced 'how many rows in prices')"
check "joins: caged count_in on the extra" '"count": 2' "$r"

# 2c-ter. roles — RBAC management (v2.8.0)
run roles --instance-dir ./inst-priced --op set --role analyst \
    --allow row_count,joined_count --row-filter "region=in:North" >/dev/null
r="$(run ask --instance-dir ./inst-priced --role analyst 'joined count')"
check "roles: row filter serves only the allowed region" '"count": 1' "$r"
r="$(run roles --instance-dir ./inst-priced --op list)"
check "roles: analyst listed" 'analyst' "$r"

# 2d. journald / wevtutil — only where the native tool exists
if command -v pi >/dev/null 2>&1; then
  r="$(run pi --setup)"
  check "pi setup: live model ping" '"ping_ok": true' "$r"
  r="$(run doctor --instance-dir ./inst-loop --json || true)"
  check "doctor: reports every integration" '"pi"' "$r"
else
  echo "  - skip pi-setup block (no pi on this host)"
fi
if command -v agent-browser >/dev/null 2>&1; then
  r="$(run browser --setup --yes)"
  check "browser setup: already installed, live ping" '"ping_ok": true' "$r"
else
  echo "  - skip browser-setup block (no agent-browser on this host)"
fi
if command -v journalctl >/dev/null 2>&1; then
  run init --source journal: --name journal --out ./inst-journal >/dev/null
  r="$(run ask --instance-dir ./inst-journal 'how many rows')"
  check "journald: events counted" '"probe": "row_count"' "$r"
else
  echo "  - skip journald block (no journalctl on this host)"
fi
if command -v wevtutil >/dev/null 2>&1; then
  run init --source wevtutil:System --name wevt --out ./inst-wevt >/dev/null
  r="$(run ask --instance-dir ./inst-wevt 'how many rows')"
  check "wevtutil: events counted" '"probe": "row_count"' "$r"
else
  echo "  - skip wevtutil block (no wevtutil on this host)"
fi

r="$(run ask --instance-dir ./inst 'distinct region')"
check "ask: distinct region" '"probe": "distinct_region"' "$r"

# 2b. the discarded ledger — absent when nothing was dropped. (The positive
# case needs a filter no candidate can consume, which this menu has none of;
# it is covered exhaustively in tests/test_discarded.py.)
check_not "ask: clean answer carries no discarded ledger" '"discarded"' "$r"
r="$(run ask --instance-dir ./inst 'total revenue by region')"
check "ask: a named dimension routes to the breakdown" '"probe": "breakdown"' "$r"
check_not "ask: a consumed filter is not reported as dropped" 'missing_' "$r"
r="$(run ask --instance-dir ./inst 'total revenue')"
check "ask: no dimension still hits the flat total" '"probe": "total_revenue"' "$r"

# 2c. refuse, don't clamp — a refusal must be distinguishable from an answer
"$NS" ask --instance-dir ./inst 'xyzzy plugh' >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ]; then
  echo "  ✓ ask: a refusal exits 2"; pass=$((pass+1))
else
  echo "  ✗ ask: a refusal exits 2 (got $rc)"; fail=$((fail+1))
fi

# 2d. scrub — remediation of records already on disk
r="$(run scrub --instance-dir ./inst --dry-run)"
check "scrub: examines the state" 'entries examined\|audit' "$r"

# 2e. reason — the DETERMINISTIC gate runs before the model is ever contacted,
# so this half is testable with no Ollama anywhere near it.
r="$(run reason --instance-dir ./inst --dry-run 'how many rows')"
check "reason: gate declines an already-answerable question" 'do not escalate' "$r"
r="$(run reason --instance-dir ./inst --dry-run 'how many rows per region')"
check "reason: gate recognises a ratio qualifier" 'per region' "$r"

# 3. invariants
r="$(run invariants --dir ./inst)"
check "invariants hold" 'all invariants hold' "$r"

# 4. lint
r="$(run lint ./inst)"
check "lint runs" 'probes\|collision\|trigger\|"' "$r"

# 5. openapi
r="$(run openapi ./inst --agent sales)"
check "openapi 3.1" '"openapi": "3.1.0"' "$r"
check "openapi has probe path" '/probes/row_count' "$r"

# 6. docs + backends
r="$(run docs usage)"
check "docs usage" 'neuralOS Usage' "$r"
r="$(run backends)"
check "backends list" 'boxlite' "$r"

# 6b. classic menu instance whose bridge imports sqlite3 at module level.
# Frozen binaries once shipped without the sqlite3 stdlib module (Nuitka
# cannot see dynamic imports from user bridge.py files), so such instances
# failed with the misleading "declares no probe that a callable actually
# backs". This check fails in the frozen binary unless sqlite3 is bundled —
# and now also asserts the diagnostic names the real cause.
mkdir -p ./inst_sqlite
cat > ./inst_sqlite/needle_menu.json <<'JSON'
[{"name": "x_count", "description": "Count rows in the demo table", "triggers": ["how many x"], "parameters": {"type": "object", "properties": {}}}]
JSON
cat > ./inst_sqlite/bridge.py <<'PY'
import sqlite3


def x_count():
    con = sqlite3.connect(":memory:")
    n = con.execute("SELECT 1").fetchone()[0]
    con.close()
    return {"n": n}
PY
r="$(run ask --instance-dir ./inst_sqlite 'how many x')"
check "menu bridge importing sqlite3 answers" '"n": 1' "$r"
check_not "sqlite3 instance not misdiagnosed" 'declares no probe' "$r"

# 6c. a bridge that fails to import must name the cause, not just the hint
mkdir -p ./inst_bad
cat > ./inst_bad/needle_menu.json <<'JSON'
[{"name": "x_count", "description": "x", "triggers": ["how many x"], "parameters": {"type": "object", "properties": {}}}]
JSON
printf 'import sqlite3  # fine
import module_that_does_not_exist_xyz


def x_count():
    return {"n": 0}
' > ./inst_bad/bridge.py
r="$(run ask --instance-dir ./inst_bad 'how many x')"
check "import failure names the module" 'module_that_does_not_exist_xyz' "$r"
check "import failure names the file" 'bridge.py:' "$r"

# 7. serve + HTTP
# Two hazards this guards against:
#  - a Nuitka onefile runs the real server in a CHILD of the pid we launch,
#    so killing the parent leaves the child holding the port;
#  - the port must sit BELOW the OS ephemeral range (Linux 32768-60999,
#    macOS 49152-65535) or the kernel may have already handed it to an
#    outgoing connection, giving 'Address already in use'.
# So: pick a non-ephemeral port, retry a few on failure, and always tear
# the whole listener down afterwards.
SPID=""
PORT=""

cleanup_serve() {
  [ -n "$SPID" ] && kill "$SPID" 2>/dev/null || true
  if [ -n "$PORT" ]; then
    if command -v lsof >/dev/null 2>&1; then
      for p in $(lsof -ti "tcp:$PORT" 2>/dev/null); do kill "$p" 2>/dev/null || true; done
    elif command -v fuser >/dev/null 2>&1; then
      fuser -k "$PORT/tcp" >/dev/null 2>&1 || true
    fi
    for _ in $(seq 1 15); do
      curl -s --max-time 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 || break
      sleep 1
    done
  fi
}

BASE=$(( 18000 + ($$ % 2000) ))   # 18000-19999: below every ephemeral range
UP=0
for i in 0 1 2 3 4 5 6 7; do
  PORT=$(( BASE + i ))
  "$NS" serve --instance-dir ./inst --port "$PORT" >serve.log 2>&1 &
  SPID=$!
  for _ in $(seq 1 20); do
    sleep 1
    if curl -s --max-time 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then UP=1; break; fi
    kill -0 "$SPID" 2>/dev/null || break   # server died (e.g. port in use)
  done
  [ "$UP" = "1" ] && break
  cleanup_serve
  SPID=""
done

if [ "$UP" = "1" ] && kill -0 "$SPID" 2>/dev/null; then
  r="$(curl -s "http://127.0.0.1:$PORT/healthz" 2>/dev/null || true)"
  check "serve /healthz" '"ok": true' "$r"
  r="$(curl -s -X POST "http://127.0.0.1:$PORT/ask" \
        -H 'Content-Type: application/json' \
        -d '{"question":"how many rows"}' 2>/dev/null || true)"
  check "serve /ask" 'row_count' "$r"
else
  echo "  ✗ serve failed to start"; echo "----- serve.log -----"; cat serve.log; echo "---------------------"; fail=$((fail+1))
fi
cleanup_serve

echo
# ── auth: serve with a token, prove the door locks ─────────────────────────
AUTH_PORT=8871
"$NS" serve --instance-dir ./inst --port "$AUTH_PORT" --auth-token sdk-secret \
    >auth_serve.log 2>&1 &
AUTH_PID=$!
for i in 1 2 3 4 5; do
  sleep 1
  curl -s --max-time 1 "http://127.0.0.1:$AUTH_PORT/healthz" >/dev/null 2>&1 && break
done
CODE_NO=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  -H 'Content-Type: application/json' \
  -d '{"question":"how many rows"}' "http://127.0.0.1:$AUTH_PORT/ask")
CODE_OK=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer sdk-secret' \
  -d '{"question":"how many rows"}' "http://127.0.0.1:$AUTH_PORT/ask")
CODE_HZ=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$AUTH_PORT/healthz")
kill "$AUTH_PID" 2>/dev/null; wait "$AUTH_PID" 2>/dev/null
if [ "$CODE_NO" = "401" ]; then echo "  ✓ auth: no token -> 401"; pass=$((pass+1)); else echo "  ✗ auth: no token -> $CODE_NO (want 401)"; fail=$((fail+1)); fi
if [ "$CODE_OK" = "200" ]; then echo "  ✓ auth: correct token -> 200"; pass=$((pass+1)); else echo "  ✗ auth: correct token -> $CODE_OK (want 200)"; fail=$((fail+1)); fi
if [ "$CODE_HZ" = "200" ]; then echo "  ✓ auth: /healthz stays open"; pass=$((pass+1)); else echo "  ✗ auth: /healthz -> $CODE_HZ (want 200)"; fail=$((fail+1)); fi

echo "smoke: $pass passed, $fail failed"
[ "$fail" -eq 0 ]