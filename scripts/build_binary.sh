#!/usr/bin/env bash
# Build one neuralosd standalone binary variant with Nuitka.
#
#   build_binary.sh <variant> <outname> <version>
#
#   variant: base | boxlite | msb
#     base     — framework + docs + skills + on-device model
#     boxlite  — base + the BoxLite microVM engine
#     msb      — base + the Microsandbox Sim (msb CLI) engine
#
# The extra deps must already be pip-installed before calling this
# (boxlite / microsandbox). Each variant uses a distinct Nuitka cache path
# so the onefile extract caches never collide.
set -euo pipefail

VARIANT="${1:?usage: build_binary.sh <base|boxlite|msb> <outname> <version>}"
OUTNAME="${2:?outname required}"
VERSION="${3:-0.0.0}"

EXTRA=()
case "$VARIANT" in
  base)
    ;;
  boxlite)
    python -c "import boxlite" 2>/dev/null || { echo "boxlite not importable"; exit 1; }
    EXTRA+=(--include-package=boxlite --include-package-data=boxlite)
    ;;
  msb)
    python -c "import microsandbox" 2>/dev/null || { echo "microsandbox not importable"; exit 1; }
    EXTRA+=(--include-package=microsandbox --include-package-data=microsandbox)
    ;;
  *)
    echo "unknown variant: $VARIANT (expected base|boxlite|msb)"; exit 1
    ;;
esac

echo "== building $OUTNAME ($VARIANT) version $VERSION =="
python -m nuitka \
  --onefile \
  --onefile-cache-mode=cached \
  --output-dir=dist \
  --output-filename="$OUTNAME" \
  --include-package=neuralosd \
  --include-package=neuralosd._cmd \
  --include-package=needle \
  --include-package=pydantic \
  --include-package-data=needle \
  --include-package-data=neuralosd \
  "${EXTRA[@]}" \
  --company-name=Hyperspace \
  --product-name="neuralosd-$VARIANT" \
  --product-version="$VERSION" \
  --file-version="$VERSION" \
  --nofollow-import-to=*.tests \
  --nofollow-import-to=*.torch \
  --nofollow-import-to=*.matplotlib \
  --nofollow-import-to=*.tkinter \
  --assume-yes-for-downloads \
  entry_point.py

echo "== built dist/$OUTNAME ($(du -h "dist/$OUTNAME" 2>/dev/null | cut -f1)) =="
