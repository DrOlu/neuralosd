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
echo "smoke: $pass passed, $fail failed"
[ "$fail" -eq 0 ]