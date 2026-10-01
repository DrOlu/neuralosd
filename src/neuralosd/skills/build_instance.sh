#!/usr/bin/env bash
# Build a new neuralOS instance from ANY data source, end to end.
#
# usage: build_instance.sh --source <path-or-dsn> \
#           --name my-instance \
#           [--kind auto|csv|json|log|database|api|text] \
#           [--out ~/neuralos-lab/instances/my-instance] \
#           [--dsn-env MY_DSN] \
#           [--packages "pymysql requests"]
set -euo pipefail

SOURCE=""; NAME=""; KIND="auto"; OUT=""; PACKAGES=""
DSN_ENV="NEURALOS_DSN"
while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --kind) KIND="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --packages) PACKAGES="$2"; shift 2;;
    --dsn-env) DSN_ENV="$2"; shift 2;;
    *) echo "unknown arg $1"; exit 1;;
  esac
done
[ -z "$SOURCE" ] && { echo "--source required"; exit 1; }
[ -z "$NAME" ] && NAME=$(basename "$SOURCE" | sed 's/\.[^.]*$//')
[ -z "$OUT" ] && OUT="${NEURALOS_LAB:-$HOME/neuralos-lab}/instances/$NAME"

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts"
[ -d "$SCRIPTS" ] || SCRIPTS="$HOME/agent-skills/skills/neuralos/scripts"
PY=python3

echo "== Building neuralOS instance: $NAME =="
echo "   source: $SOURCE"
echo "   output: $OUT"
echo

echo "== [1/5] Profile =="
$PY "$SCRIPTS/profile_data.py" --source "$SOURCE" --out "$OUT/profile.json"

echo "== [2/5] Generate Pydantic models =="
$PY "$SCRIPTS/gen_pydantic.py" --profile "$OUT/profile.json" --out "$OUT/models.py"

echo "== [3/5] Generate full instance =="
$PY "$SCRIPTS/gen_needle_instance.py" \
    --profile "$OUT/profile.json" --models "$OUT/models.py" \
    --out "$OUT" --agent-name "$NAME"

echo "== [4/5] Verify =="
cd "$OUT"
$PY verify.py --full 2>&1 | tail -10 || true

echo "== [5/5] Done =="
echo "Instance: $OUT"
echo "Ask:      $PY ask.py \"your question here\""
echo "Serve:    $PY serve.py --port 8877"
