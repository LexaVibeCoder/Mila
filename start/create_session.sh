#!/usr/bin/env bash
# One-shot TDLib login for Mila (Linux / macOS).
#
# Usage:
#   ./start/create_session.sh              # вход по коду из Telegram
#   ./start/create_session.sh --qr         # вход по QR-ссылке (без кода)
#   (run from anywhere; paths are resolved relative to this script;
#    arguments are passed through to create_session.py)
#
# What it does:
#   1. Refuses to run as root/sudo (override with MILA_ALLOW_ROOT=1):
#      a venv created by root points to root's uv-managed Python and is
#      unusable for a regular user afterwards.
#   2. Drops a stale venv from a previous run; if it cannot be removed
#      (e.g. it belongs to another user) it fails with a clear message.
#   3. If system python3.11 exists -> creates a venv with it.
#      Otherwise -> installs uv (if missing), creates the venv with
#      `uv venv --python 3.11` (uv downloads Python 3.11 by itself).
#   4. Makes sure pip exists inside the venv (ensurepip, uv as fallback).
#   5. Installs only what create_session.py needs, with pytdbot/tdjson
#      pinned to matching versions.
#   6. Runs `python create_session.py` from the project root.
#   7. On success deletes the venv. On failure keeps it for debugging.
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv-session"
VENV_PY="$VENV_DIR/bin/python"

PYTDBOT_SPEC="Pytdbot[tdjson]==0.10.1"
TDJSON_SPEC="tdjson==1.8.65"
# Minimal deps of create_session.py: pytdbot + config.py (pydantic-settings,
# dotenv) + qrcode for the terminal QR login.
DEPS=("$PYTDBOT_SPEC" "$TDJSON_SPEC" "pydantic-settings" "python-dotenv" "qrcode>=7.4")

info() { echo "--> $*"; }
die() {
    echo "ERROR: $*" >&2
    exit 1
}

echo "=== Mila one-shot TDLib login ==="
echo "Project: $PROJECT_ROOT"

if [ "$(id -u)" -eq 0 ] && [ "${MILA_ALLOW_ROOT:-0}" != "1" ]; then
    cat >&2 <<'EOF'
ERROR: do not run this script as root/sudo.
       The venv would belong to root and its interpreter would live under
       /root, so a normal user could neither use nor delete it.
       If you really need root, re-run with: MILA_ALLOW_ROOT=1
EOF
    exit 1
fi

# Fresh start: drop a stale venv from a previous failed run.
if [ -e "$VENV_DIR" ] || [ -L "$VENV_DIR" ]; then
    info "Removing stale venv: $VENV_DIR"
    if ! rm -rf -- "$VENV_DIR" 2>/dev/null; then
        die "cannot remove $VENV_DIR (owned by another user?).
       Remove it manually and retry:
         sudo rm -rf '$VENV_DIR'"
    fi
fi

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    echo "--> uv not found, installing uv..."
    command -v curl >/dev/null 2>&1 \
        || die "curl is required to install uv. Install curl and retry."
    curl -LsSf https://astral.sh/uv/install.sh | sh \
        || die "uv installation failed."
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 \
        || die "uv not found after installation. Add \$HOME/.local/bin to PATH."
    info "uv installed: $(uv --version)"
}

if command -v python3.11 >/dev/null 2>&1; then
    info "Found system python3.11: $(command -v python3.11)"
    if python3.11 -m venv "$VENV_DIR"; then
        info "venv created with python3.11"
    else
        info "python3.11 -m venv failed (missing python3.11-venv?), falling back to uv"
        rm -rf -- "$VENV_DIR" 2>/dev/null || true
    fi
fi

if [ ! -x "$VENV_PY" ]; then
    info "System python3.11 not available, using uv"
    ensure_uv
    uv venv --python 3.11 "$VENV_DIR" || die "uv venv failed"
    info "venv created with uv (Python 3.11)"
fi

[ -x "$VENV_PY" ] || die "venv python not found at $VENV_PY"
VENV_PY_VERSION="$("$VENV_PY" --version 2>&1)" \
    || die "venv python is not runnable: $VENV_PY ($VENV_PY_VERSION)"
info "venv python: $VENV_PY_VERSION"

# uv venvs (and some distro venvs) ship without pip by default.
if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    info "pip missing in venv, installing pip..."
    if "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1 \
        && "$VENV_PY" -m pip --version >/dev/null 2>&1; then
        info "pip installed via ensurepip"
    else
        ensure_uv
        uv pip install --python "$VENV_PY" pip \
            || die "cannot install pip into the venv."
        info "pip installed via uv"
    fi
fi

info "Installing session deps: ${DEPS[*]}"
"$VENV_PY" -m pip install --upgrade pip || die "pip upgrade failed"
"$VENV_PY" -m pip install "${DEPS[@]}" || die "dependency installation failed"

echo "--> Running create_session.py (workdir: project root)"
cd "$PROJECT_ROOT" || die "cannot cd to $PROJECT_ROOT"
"$VENV_PY" create_session.py "$@"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    info "Login OK, removing temporary venv"
    rm -rf -- "$VENV_DIR" \
        || echo "WARNING: could not remove $VENV_DIR (remove it manually)." >&2
else
    echo "--> create_session.py failed (exit $STATUS). Venv kept at:" >&2
    echo "    $VENV_DIR" >&2
    echo "    Re-run this script to start fresh." >&2
fi

exit "$STATUS"
