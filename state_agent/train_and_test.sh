#!/usr/bin/env bash
# Train the State Agent MLP and test it interactively.
#
# Usage:
#   ./state_agent/train_and_test.sh              train if state_agent.pt is missing/unloadable, then test
#   ./state_agent/train_and_test.sh --retrain    force retraining on dataset.json
#   ./state_agent/train_and_test.sh --no-test    train (or skip) and exit without testing
#   -h, --help                                   show this help
#
# The script uses state_agent/.venv, creating it with Python 3.11 (via uv if
# needed) and installing state_agent/requirements.txt only when the
# imports fail. An existing working venv and an existing model are left untouched.
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
MODEL_PATH="$SCRIPT_DIR/state_agent.pt"

RETRAIN=0
RUN_TEST=1

info() { echo "--> $*"; }
die() {
    echo "ERROR: $*" >&2
    exit 1
}

usage() {
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
    case "$arg" in
        --retrain) RETRAIN=1 ;;
        --no-test) RUN_TEST=0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $arg (see --help)" ;;
    esac
done

echo "=== Mila State Agent: train & test ==="
echo "Project: $PROJECT_ROOT"

if [ "$(id -u)" -eq 0 ] && [ "${MILA_ALLOW_ROOT:-0}" != "1" ]; then
    cat >&2 <<'EOF'
ERROR: do not run this script as root/sudo.
       The venv would belong to root, so a normal user could not use it.
       If you really need root, re-run with: MILA_ALLOW_ROOT=1
EOF
    exit 1
fi

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    info "uv not found, installing uv..."
    command -v curl >/dev/null 2>&1 \
        || die "curl is required to install uv. Install curl and retry."
    curl -LsSf https://astral.sh/uv/install.sh | sh \
        || die "uv installation failed."
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 \
        || die "uv not found after installation. Add \$HOME/.local/bin to PATH."
    info "uv installed: $(uv --version)"
}

# ---------------------------------------------------------------------------
# 1. Virtual environment + dependencies
# ---------------------------------------------------------------------------
echo
info "Python environment: $VENV_DIR"

if [ -e "$VENV_DIR" ] && [ ! -x "$VENV_PY" ]; then
    die "$VENV_DIR exists but $VENV_PY is missing or not executable.
       Remove the broken venv and re-run: rm -rf '$VENV_DIR'"
fi

if [ ! -x "$VENV_PY" ]; then
    if command -v python3.11 >/dev/null 2>&1; then
        info "Found system python3.11: $(command -v python3.11)"
        if python3.11 -m venv "$VENV_DIR"; then
            info "venv created with python3.11"
        else
            info "python3.11 -m venv failed, falling back to uv"
            rm -rf -- "$VENV_DIR" 2>/dev/null || true
        fi
    fi

    if [ ! -x "$VENV_PY" ]; then
        info "System python3.11 not available, using uv"
        ensure_uv
        uv venv --python 3.11 "$VENV_DIR" || die "uv venv failed"
        info "venv created with uv (Python 3.11)"
    fi
else
    info "Using existing venv, not recreating it"
fi

[ -x "$VENV_PY" ] || die "venv python not found at $VENV_PY"
info "venv python: $("$VENV_PY" --version 2>&1)"

if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    info "pip missing in venv, installing pip..."
    if ! "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1; then
        ensure_uv
        uv pip install --python "$VENV_PY" pip || die "cannot install pip into the venv"
    fi
fi

[ -f "$REQ_FILE" ] || die "requirements.txt not found at $REQ_FILE"

if "$VENV_PY" -c "import torch, sklearn, numpy, tqdm" >/dev/null 2>&1; then
    info "Dependencies already available, skipping install"
else
    info "Installing dependencies from $REQ_FILE (may take a while)..."
    "$VENV_PY" -m pip install --upgrade pip || die "pip upgrade failed"
    "$VENV_PY" -m pip install -r "$REQ_FILE" || die "dependency installation failed"
    info "Dependencies installed"
fi

cd "$SCRIPT_DIR" || die "cannot cd to $SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 2. Training (skipped when the model already exists)
# ---------------------------------------------------------------------------
echo
MODEL_OK=0
if [ -f "$MODEL_PATH" ] && "$VENV_PY" test_interactive.py --check >/dev/null 2>&1; then
    MODEL_OK=1
fi

if [ "$RETRAIN" -eq 1 ] || [ "$MODEL_OK" -eq 0 ]; then
    if [ -f "$MODEL_PATH" ] && [ "$RETRAIN" -eq 0 ]; then
        info "Model file exists but failed to load; retraining: $MODEL_PATH"
    fi
    info "Training on dataset.json..."
    "$VENV_PY" train_state_agent.py || die "training failed"
    [ -f "$MODEL_PATH" ] || die "training finished but $MODEL_PATH was not created"
    info "Model saved: $MODEL_PATH"
else
    info "Model exists and loads: $MODEL_PATH"
    info "Skipping training (use --retrain to force retraining)"
fi

# ---------------------------------------------------------------------------
# 3. Interactive test
# ---------------------------------------------------------------------------
if [ "$RUN_TEST" -eq 0 ]; then
    info "--no-test given, done"
    exit 0
fi

echo
if [ -t 0 ]; then
    printf 'Run the interactive test? [Y/n]: '
    read -r answer || answer=""
    case "$answer" in
        [nN]*) info "Testing skipped"; exit 0 ;;
    esac
fi

"$VENV_PY" test_interactive.py || exit $?
