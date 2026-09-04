#!/usr/bin/env bash
# One-shot TDLib login for Mila (Linux / macOS).
#
# Usage:
#   ./start/create_session.sh
#   (run from anywhere; paths are resolved relative to this script)
#
# What it does:
#   1. If system python3.11 exists -> creates a venv with it.
#      Otherwise -> installs uv (if missing), creates the venv with
#      `uv venv --python 3.11` (uv downloads Python 3.11 by itself)
#      and installs pip into it.
#   2. Installs only what create_session.py needs:
#      pytdbot (with prebuilt tdjson), pydantic-settings, python-dotenv.
#   3. Runs `python create_session.py` from the project root.
#   4. On success deletes the venv. On failure keeps it for debugging.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv-session"
VENV_PY="$VENV_DIR/bin/python"

# Minimal deps of create_session.py: pytdbot + config.py (pydantic-settings, dotenv).
DEPS=("Pytdbot[tdjson]>=0.10.0" "pydantic-settings" "python-dotenv")

echo "=== Mila one-shot TDLib login ==="
echo "Project: $PROJECT_ROOT"

# Fresh start: drop a stale venv from a previous failed run.
rm -rf "$VENV_DIR"

HAVE_PY311=0
if command -v python3.11 >/dev/null 2>&1; then
    HAVE_PY311=1
fi

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    echo "--> uv not found, installing uv..."
    if ! command -v curl >/dev/null 2>&1; then
        echo "ERROR: curl is required to install uv. Install curl and retry." >&2
        return 1
    fi
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        echo "ERROR: uv installation failed." >&2
        return 1
    fi
    echo "--> uv installed: $(uv --version)"
}

HAVE_UV_VENV=0
if [ "$HAVE_PY311" -eq 1 ]; then
    echo "--> Found system python3.11: $(command -v python3.11)"
    if python3.11 -m venv "$VENV_DIR"; then
        echo "--> venv created with python3.11"
    else
        echo "--> python3.11 -m venv failed (missing python3.11-venv?), falling back to uv"
        HAVE_PY311=0
    fi
fi

if [ "$HAVE_PY311" -eq 0 ]; then
    echo "--> System python3.11 not available, using uv"
    ensure_uv || exit 1
    uv venv --python 3.11 "$VENV_DIR" || exit 1
    echo "--> venv created with uv (Python 3.11)"
    HAVE_UV_VENV=1
fi

VENV_PY="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: venv python not found at $VENV_PY" >&2
    exit 1
fi
echo "--> venv python: $("$VENV_PY" --version)"

# Make sure pip exists inside the venv (uv venvs ship without pip by default).
if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    echo "--> pip missing in venv, installing pip..."
    if "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1; then
        echo "--> pip installed via ensurepip"
    elif [ "$HAVE_UV_VENV" -eq 1 ]; then
        uv pip install --python "$VENV_PY" pip || exit 1
        echo "--> pip installed via uv"
    else
        echo "ERROR: cannot install pip into the venv." >&2
        exit 1
    fi
fi

echo "--> Installing session deps: ${DEPS[*]}"
"$VENV_PY" -m pip install --upgrade pip || exit 1
"$VENV_PY" -m pip install "${DEPS[@]}" || exit 1

echo "--> Running create_session.py (workdir: project root)"
cd "$PROJECT_ROOT" || exit 1
"$VENV_PY" create_session.py
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    echo "--> Login OK, removing temporary venv"
    rm -rf "$VENV_DIR"
else
    echo "--> create_session.py failed (exit $STATUS). Venv kept at:" >&2
    echo "    $VENV_DIR" >&2
    echo "    Re-run this script to start fresh." >&2
fi

exit "$STATUS"
