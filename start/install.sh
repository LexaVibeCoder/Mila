#!/usr/bin/env bash
# One-shot installer for Mila (Linux / macOS).
#
# Usage:
#   ./start/install.sh                 # install everything that is missing
#   ./start/install.sh --qr            # first login via QR link instead of code
#   ./start/install.sh --no-reranker   # never download the reranker
#   ./start/install.sh --skip-session  # do not touch the Telegram session
#   ./start/install.sh --no-encrypt    # do not encrypt the TDLib database
#   ./start/install.sh --encrypt       # encrypt the TDLib database (default)
#   ./start/install.sh --yes           # non-interactive: accept defaults
#
# The script is idempotent: each step is skipped when its result already exists.
#   1. .venv + requirements.txt (skip when the venv is healthy and deps match)
#   2. .env from .env.example (skip when present); generates the TDLib key and
#      waits for the user to fill the required fields.
#   3. TDLib session (create_session.py; an already authorized session exits
#      immediately, so it effectively verifies and skips). On the first login
#      it asks whether to encrypt the database.
#   4. BGE reranker (asks Y/n; default yes, skipped when the model is present).
#
# It never starts the bot. Run it afterwards with: .venv/bin/python main.py
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"
STAMP="$VENV_DIR/.mila-requirements.sha256"
ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"
REQ_FILE="$PROJECT_ROOT/requirements.txt"
RERANKER_DIR="$PROJECT_ROOT/models/rerankers/BGE-Reranker-v2-m3"

ASSUME_YES=0
RERANKER_MODE="ask"   # ask | yes | no
ENCRYPT_MODE="ask"    # ask | yes | no
RUN_SESSION=1
SESSION_ARGS=()

info() { echo "--> $*"; }
warn() { echo "WARNING: $*" >&2; }
die() {
    echo "ERROR: $*" >&2
    exit 1
}

ask_yes_no() {
    local prompt="$1"
    if [ "$ASSUME_YES" -eq 1 ]; then
        return 0
    fi
    # Non-interactive runs accept the default (yes).
    if [ ! -t 0 ]; then
        return 0
    fi
    local answer=""
    printf '%s [Y/n] ' "$prompt"
    read -r answer || answer=""
    case "$answer" in
        [nN]*) return 1 ;;
        *) return 0 ;;
    esac
}

usage() {
    sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
    case "$arg" in
        --qr) SESSION_ARGS+=("--qr") ;;
        --yes|-y) ASSUME_YES=1 ;;
        --reranker) RERANKER_MODE="yes" ;;
        --no-reranker) RERANKER_MODE="no" ;;
        --encrypt) ENCRYPT_MODE="yes" ;;
        --no-encrypt) ENCRYPT_MODE="no" ;;
        --skip-session) RUN_SESSION=0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $arg (see --help)" ;;
    esac
done

echo "=== Mila installer ==="
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

# Everything below relies on relative paths (.env, requirements.txt, data/...).
cd "$PROJECT_ROOT" || die "cannot cd to $PROJECT_ROOT"

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
info "Step 1/4: Python environment"

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
else
    info "Using existing venv: $VENV_DIR"
fi

[ -x "$VENV_PY" ] || die "venv python not found at $VENV_PY"
VENV_PY_VERSION="$("$VENV_PY" --version 2>&1)" \
    || die "venv python is not runnable: $VENV_PY"
info "venv python: $VENV_PY_VERSION"
case "$VENV_PY_VERSION" in
    *" 3.11."*|*" 3.12."*|*" 3.13."*) ;;
    *) warn "target Python is 3.11; $VENV_PY_VERSION may break pytdbot/tdjson pins" ;;
esac

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

[ -f "$REQ_FILE" ] || die "requirements.txt not found at $REQ_FILE"
REQ_HASH="$("$VENV_PY" -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$REQ_FILE")"

DEPS_OK=0
if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$REQ_HASH" ] \
    && "$VENV_PY" -c "import pytdbot, faiss, fastapi, openai, sentence_transformers" >/dev/null 2>&1; then
    DEPS_OK=1
fi

if [ "$DEPS_OK" -eq 1 ]; then
    info "Dependencies already installed (requirements.txt unchanged), skipping"
else
    info "Installing dependencies from requirements.txt (may take a while)..."
    "$VENV_PY" -m pip install --upgrade pip || die "pip upgrade failed"
    "$VENV_PY" -m pip install -r "$REQ_FILE" || die "dependency installation failed"
    printf '%s\n' "$REQ_HASH" > "$STAMP"
    info "Dependencies installed"
fi

# ---------------------------------------------------------------------------
# 2. .env
# ---------------------------------------------------------------------------
echo
info "Step 2/4: Configuration (.env)"

CONFIG_CHECK=("$VENV_PY" "$PROJECT_ROOT/start/check_config.py")
config_ok() { "${CONFIG_CHECK[@]}" >/dev/null 2>&1; }

if [ -f "$ENV_FILE" ]; then
    info ".env already exists, skipping creation"
else
    [ -f "$ENV_EXAMPLE" ] || die ".env.example not found at $ENV_EXAMPLE"
    cp "$ENV_EXAMPLE" "$ENV_FILE" || die "cannot create .env"
    "$VENV_PY" "$PROJECT_ROOT/start/set_env.py" TD_DATABASE_ENCRYPTION_KEY --random \
        || die "cannot generate TD_DATABASE_ENCRYPTION_KEY"
    info ".env created from .env.example with a generated TDLib key"
fi

if ! config_ok; then
    echo ""
    "${CONFIG_CHECK[@]}" || true
    echo "Edit $ENV_FILE and fill in the values above (see comments inside the file)."
    if [ "$ASSUME_YES" -eq 1 ] || [ ! -t 0 ]; then
        die "cannot continue in non-interactive mode with an invalid .env"
    fi
    while ! config_ok; do
        printf 'Press Enter after editing .env (Ctrl-C to abort)... '
        read -r _ || die "aborted"
        "${CONFIG_CHECK[@]}" || true
    done
fi
info ".env is ready"

# ---------------------------------------------------------------------------
# 3. TDLib session
# ---------------------------------------------------------------------------
echo
info "Step 3/4: Telegram session"

if [ "$RUN_SESSION" -eq 0 ]; then
    info "--skip-session given, skipping"
else
    TD_DIR="$("$VENV_PY" -c 'from config import settings;print(settings.TD_FILES_DIRECTORY)' 2>/dev/null || true)"
    if [ -n "$TD_DIR" ] && { [ -d "$TD_DIR" ] || [ -d "$PROJECT_ROOT/$TD_DIR" ]; }; then
        info "Existing TDLib data found ($TD_DIR), verifying the session..."
        if [ "$ENCRYPT_MODE" != "ask" ]; then
            warn "encryption flag ignored: the existing database keeps its current mode"
        fi
    else
        info "No TDLib data yet, starting the first login..."
        ENCRYPT=1
        if [ "$ENCRYPT_MODE" = "no" ]; then
            ENCRYPT=0
        elif [ "$ENCRYPT_MODE" = "yes" ]; then
            ENCRYPT=1
        elif ! ask_yes_no "Encrypt the TDLib database? (recommended)"; then
            ENCRYPT=0
        fi

        if [ "$ENCRYPT" -eq 1 ]; then
            if ! grep -Eq '^[[:space:]]*TD_DATABASE_ENCRYPTION_KEY=[^[:space:]]' "$ENV_FILE"; then
                "$VENV_PY" "$PROJECT_ROOT/start/set_env.py" TD_DATABASE_ENCRYPTION_KEY --random \
                    || die "cannot generate TD_DATABASE_ENCRYPTION_KEY"
            fi
            info "TDLib database will be encrypted"
        else
            "$VENV_PY" "$PROJECT_ROOT/start/set_env.py" TD_DATABASE_ENCRYPTION_KEY --empty \
                || die "cannot clear TD_DATABASE_ENCRYPTION_KEY"
            warn "TDLib database will NOT be encrypted (key cleared in .env)"
        fi
    fi
    # An already authorized session reaches AuthorizationStateReady immediately
    # and create_session.py exits 0, so this doubles as the "is it set up?" check.
    "$VENV_PY" create_session.py ${SESSION_ARGS[@]+"${SESSION_ARGS[@]}"} \
        || die "Telegram login failed. Fix the error above and re-run this script."
    info "Telegram session is ready"
fi

# ---------------------------------------------------------------------------
# 4. Reranker
# ---------------------------------------------------------------------------
echo
info "Step 4/4: Reranker model"

if [ -d "$RERANKER_DIR" ]; then
    info "Reranker already present at $RERANKER_DIR, skipping"
elif [ "$RERANKER_MODE" = "no" ]; then
    info "--no-reranker given, skipping"
elif [ "$RERANKER_MODE" = "yes" ] || ask_yes_no "Download BGE-Reranker-v2-m3 (~2 GB)?"; then
    info "Downloading reranker (this can take a while)..."
    if "$VENV_PY" install_reranker.py; then
        info "Reranker installed"
    else
        warn "reranker download failed; search_memory will not work."
        warn "Retry later with: $VENV_PY install_reranker.py"
    fi
else
    info "Skipping reranker"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo
echo "=== Installation complete ==="
echo "Bot:      cd '$PROJECT_ROOT' && .venv/bin/python main.py"
echo "Panel:    cd '$PROJECT_ROOT' && .venv/bin/python -m Mila_observatory.server"
echo "Frontend: cd '$PROJECT_ROOT/Mila_observatory/frontend' && npm ci && npm run dev"
