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

# NOTE: a plain string (not an array) so this works on bash 3.2 (macOS)
# under `set -u`, where expanding an empty array is an error.
#
# --nofollow-import-to is essential: backends import their runtime lazily
# (`import boxlite` inside a method), and Nuitka still follows that import.
# Without it, every variant built in a job that has boxlite installed ends
# up bundling boxlite — so the msb binary reported BOTH backends available.
NOFOLLOW_BOX="--nofollow-import-to=boxlite"
NOFOLLOW_MSB="--nofollow-import-to=microsandbox"
EXTRA="$NOFOLLOW_BOX $NOFOLLOW_MSB"
case "$VARIANT" in
  base)
    ;;
  boxlite)
    python -c "import boxlite" 2>/dev/null || { echo "boxlite not importable"; exit 1; }
    EXTRA="--include-package=boxlite --include-package-data=boxlite $NOFOLLOW_MSB"
    ;;
  msb)
    python -c "import microsandbox" 2>/dev/null || { echo "microsandbox not importable"; exit 1; }
    # The msb CLI + libs live in microsandbox/_bundled/{bin,lib}. Three
    # Nuitka facts learned the hard way:
    #   - --include-package-data does NOT pull the executables/.dll;
    #   - --include-data-dir is a silent no-op (Nuitka 4.2.2);
    #   - an absolute C:\ path is mangled by Git Bash on Windows.
    # So: stage _bundled to a RELATIVE dir, then copy with explicit globs.
    rm -rf _msb_stage
    python - <<'PY'
import os, shutil, microsandbox
d = os.path.join(os.path.dirname(microsandbox.__file__), "_bundled")
shutil.copytree(d, "_msb_stage")
print("staged msb runtime from", d)
PY
    EXTRA="--include-package=microsandbox --include-package-data=microsandbox $NOFOLLOW_BOX"
    [ -d _msb_stage/bin ] && EXTRA="$EXTRA --include-data-files=_msb_stage/bin/*=microsandbox/_bundled/bin/"
    [ -d _msb_stage/lib ] && EXTRA="$EXTRA --include-data-files=_msb_stage/lib/*=microsandbox/_bundled/lib/"
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
  --include-package=openpyxl \
  --include-package-data=needle \
  --include-package-data=neuralosd \
  --include-package-data=openpyxl \
  $EXTRA \
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
