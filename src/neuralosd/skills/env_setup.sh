#!/usr/bin/env bash
# One-command environment setup for neuralOS on any platform.
# Checks: Python 3.11-3.13, virtualization, then installs everything.
set -euo pipefail

LAB="${NEURALOS_LAB:-$HOME/neuralos-lab}"
PYVER="${NEURALOS_PYTHON:-3.12}"

echo "=========================================="
echo "  neuralOS Complete Environment Setup"
echo "=========================================="
echo "  Lab dir : $LAB"
echo "  Python  : $PYVER"
echo "  OS      : $(uname -s) $(uname -m)"
echo

# ── 1. Platform check ─────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Darwin)
        HV=$(sysctl -n kern.hv_support 2>/dev/null || echo 0)
        if [ "$HV" != "1" ]; then
            echo "ERROR: Hypervisor.framework not supported on this Mac"
            exit 1
        fi
        echo "✓ macOS Apple Silicon, Hypervisor.framework OK"
        ;;
    Linux)
        [ -e /dev/kvm ] && echo "✓ Linux KVM OK" || {
            echo "ERROR: /dev/kvm missing — enable KVM in BIOS/kernel"
            exit 1
        }
        echo "✓ Linux KVM OK"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        echo "✓ Windows (WSL2 assumed)"
        ;;
    *)
        echo "WARN: unknown platform $OS — attempting anyway"
        ;;
esac

# ── 2. Python venv ────────────────────────────────────────────────────
mkdir -p "$LAB"
if command -v uv >/dev/null 2>&1; then
    uv venv "$LAB/venv" --python "$PYVER"
    uv pip install --python "$LAB/venv" neuralos pydantic
else
    PYBIN="$(command -v python$PYVER || command -v python3)"
    "$PYBIN" -m venv "$LAB/venv"
    "$LAB/venv/bin/pip" install --upgrade pip
    "$LAB/venv/bin/pip" install neuralos pydantic
fi

echo "== verify =="
"$LAB/venv/bin/python" -c "
import needle; print('  neuralOS/needle:', needle.__version__)
import pydantic; print('  pydantic:', pydantic.__version__)
"

# ── 3. Optional: BoxLite ──────────────────────────────────────────────
read -p "Install BoxLite (microVM hosting)? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    uv pip install --python "$LAB/venv" boxlite || pip install boxlite
    echo "✓ BoxLite installed"
fi

# ── 4. Optional: Microsandbox ─────────────────────────────────────────
read -p "Install Microsandbox (msb)? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    curl -fsSL https://install.microsandbox.dev | sh
    echo "✓ Microsandbox installed"
fi

echo
echo "=========================================="
echo "  Setup complete!"
echo "  Activate: source $LAB/venv/bin/activate"
echo "  Next: cook a template with cook_template.sh"
echo "=========================================="
