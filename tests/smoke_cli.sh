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
"$NS" serve --instance-dir ./inst --port 8897 >serve.log 2>&1 &
SPID=$!
UP=0
for _ in $(seq 1 30); do
  sleep 1
  if curl -s --max-time 1 http://127.0.0.1:8897/healthz >/dev/null 2>&1; then UP=1; break; fi
done
if [ "$UP" = "1" ] && kill -0 $SPID 2>/dev/null; then
  r="$(curl -s http://127.0.0.1:8897/healthz 2>/dev/null || true)"
  check "serve /healthz" '"ok": true' "$r"
  r="$(curl -s -X POST http://127.0.0.1:8897/ask \
        -H 'Content-Type: application/json' \
        -d '{"question":"how many rows"}' 2>/dev/null || true)"
  check "serve /ask" 'row_count' "$r"
  kill $SPID 2>/dev/null || true
else
  echo "  ✗ serve failed to start"; head -5 serve.log; fail=$((fail+1))
fi

echo
echo "smoke: $pass passed, $fail failed"
[ "$fail" -eq 0 ]