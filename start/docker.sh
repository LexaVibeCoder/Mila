#!/usr/bin/env bash
# One-command Docker launcher for Mila (Linux / macOS).
#
# Usage:
#   ./start/docker.sh                 # check env/session/reranker, then start or reuse
#   ./start/docker.sh --login         # first Telegram login by code, then start
#   ./start/docker.sh --login --qr    # first Telegram login by QR, then start
#   ./start/docker.sh --reranker      # download BGE reranker into ./models
#   ./start/docker.sh --logs          # follow logs
#   ./start/docker.sh --restart       # restart existing services
#   ./start/docker.sh --status        # show container status
#   ./start/docker.sh --down          # stop and remove containers (volumes are kept)
#   ./start/docker.sh --recreate      # recreate containers from the existing image
#   ./start/docker.sh --rebuild       # rebuild without cache, then recreate
#   ./start/docker.sh --no-build      # never build; require an existing image
#
# Default flow:
#   .env -> required values -> image -> TDLib session -> reranker -> containers.
# Existing running containers are left alone; stopped ones are started with
# `docker compose start`; only missing ones are created with
# `docker compose up -d --no-recreate`.
#
# .env is created from .env.example on the first run, with a generated
# TD_DATABASE_ENCRYPTION_KEY; fill in the required values and re-run. The TDLib
# session lives in the mila_data volume, so the login must be done inside the
# container (`--login`), never on the host.
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ACTION="up"
DO_BUILD=1
FORCE_REBUILD=0
FORCE_RECREATE=0
QR=0

info() { echo "--> $*"; }
warn() { echo "WARNING: $*" >&2; }
die() {
    echo "ERROR: $*" >&2
    exit 1
}

usage() {
    sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
    case "$arg" in
        --login) ACTION="login" ;;
        --qr) ACTION="login"; QR=1 ;;
        --reranker) ACTION="reranker" ;;
        --logs) ACTION="logs" ;;
        --restart) ACTION="restart" ;;
        --status) ACTION="status" ;;
        --down) ACTION="down" ;;
        --recreate) FORCE_RECREATE=1 ;;
        --rebuild) FORCE_REBUILD=1; FORCE_RECREATE=1 ;;
        --no-build) DO_BUILD=0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $arg (see --help)" ;;
    esac
done

cd "$PROJECT_ROOT" || die "cannot cd to $PROJECT_ROOT"

DC=()
if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
else
    die "Docker Compose not found. Install Docker Engine 20.10+ with Compose 2.x."
fi

dc() { "${DC[@]}" "$@"; }

generate_tdlib_key() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_hex(32))'
    elif command -v python >/dev/null 2>&1; then
        python -c 'import secrets; print(secrets.token_hex(32))'
    else
        return 1
    fi
}

set_env_value() {
    local key="$1" value="$2" file="$PROJECT_ROOT/.env" tmp
    # Keep the temporary file next to .env so mv is atomic and preserves the
    # directory's permissions/SELinux context (important for Docker env_file).
    tmp="$(mktemp "${file}.tmp.XXXXXX")" || return 1
    grep -vE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" > "$tmp" 2>/dev/null || true
    # Ensure the appended line starts on a new line even if the source file
    # has no trailing newline (e.g. a hand-edited .env).
    printf '\n%s=%s\n' "$key" "$value" >> "$tmp" || { rm -f "$tmp"; return 1; }
    mv "$tmp" "$file" || { rm -f "$tmp"; return 1; }
}

ensure_env() {
    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env" \
            || die "cannot create .env from .env.example"

        local tdlib_key
        if tdlib_key="$(generate_tdlib_key)" && [ -n "$tdlib_key" ]; then
            set_env_value TD_DATABASE_ENCRYPTION_KEY "$tdlib_key" \
                || warn "could not write TD_DATABASE_ENCRYPTION_KEY to .env; set it manually."
        else
            warn "could not generate TD_DATABASE_ENCRYPTION_KEY (openssl/python not found); set it manually."
        fi

        cat <<'EOF'
Created .env from .env.example (TD_DATABASE_ENCRYPTION_KEY is generated when
openssl/python is available).

Fill in at least:
  API_ID, API_HASH
  LLM_API_BASE, LLM_API_KEY, MAIN_LLM_MODEL
  EMBED_API_BASE, EMBED_MODEL
  EMBED_DIM (384 by default; must match EMBED_MODEL)

Then re-run: ./start/docker.sh
EOF
        exit 1
    fi
}

env_get() {
    local key="$1" line
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$PROJECT_ROOT/.env" 2>/dev/null | tail -n 1)" || return 0
    [ -n "$line" ] || return 0
    line="${line#*=}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    case "$line" in
        \"*\") line="${line#\"}"; line="${line%\"}" ;;
        \'*\') line="${line#\'}"; line="${line%\'}" ;;
    esac
    printf '%s' "$line"
}

env_has() {
    local key="$1"
    grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$PROJECT_ROOT/.env" 2>/dev/null
}

check_required_env() {
    local key value missing=()
    local required=()
    case "$ACTION" in
        # Reranker download talks to HuggingFace only and does not import
        # config.py, so an otherwise empty .env is enough.
        reranker) required=() ;;
        # create_session.py imports config.py, so login needs the full config.
        *) required=(API_ID API_HASH LLM_API_BASE MAIN_LLM_MODEL EMBED_API_BASE EMBED_MODEL) ;;
    esac
    for key in ${required[@]+"${required[@]}"}; do
        value="$(env_get "$key")"
        [ -n "$value" ] || missing+=("$key")
    done
    # config.py requires these keys to be present even when their values are
    # empty (LM Studio/Ollama can run without an API key).
    if [ "$ACTION" != "reranker" ]; then
        for key in LLM_API_KEY EMBED_API_KEY; do
            env_has "$key" || missing+=("$key")
        done
    fi
    if [ "${#missing[@]}" -gt 0 ]; then
        die ".env is missing required values: ${missing[*]}. See .env.example."
    fi

    if [ "$ACTION" != "reranker" ] && [ -z "$(env_get TD_DATABASE_ENCRYPTION_KEY)" ]; then
        warn "TD_DATABASE_ENCRYPTION_KEY is empty: TDLib will use the built-in default key. Set a unique value before the first login."
    fi

    local publish_host password
    publish_host="$(env_get OBSERVATORY_PUBLISH_HOST)"
    [ -n "$publish_host" ] || publish_host="$(env_get OBSERVATORY_HOST)"
    publish_host="${publish_host:-127.0.0.1}"
    password="$(env_get OBSERVATORY_PASSWORD)"
    if [ "$publish_host" = "0.0.0.0" ] && [ -z "$password" ]; then
        warn "OBSERVATORY_PUBLISH_HOST=0.0.0.0 without OBSERVATORY_PASSWORD — the panel is open to the network."
    fi
}

have_image() {
    [ -n "$(dc images -q bot 2>/dev/null)" ]
}

container_exists() {
    [ -n "$(dc ps -a -q "$1" 2>/dev/null)" ]
}

container_running() {
    [ -n "$(dc ps -q "$1" 2>/dev/null)" ]
}

ensure_image() {
    if [ "$FORCE_REBUILD" -eq 1 ]; then
        [ "$DO_BUILD" -eq 1 ] || die "--rebuild cannot be combined with --no-build"
        info "Rebuilding images without cache..."
        dc build --no-cache || die "docker compose build failed"
        return 0
    fi
    if have_image; then
        info "Image already built; skipping build (use --rebuild to update it)."
        return 0
    fi
    if [ "$DO_BUILD" -eq 0 ]; then
        die "Image is not built and --no-build was given."
    fi
    info "Image not found; building..."
    dc build || die "docker compose build failed"
}

has_session() {
    # The session is inside the mila_data volume. A non-empty TDLib directory
    # alone is not enough: TDLib creates DB files before authorization. The
    # heartbeat is written only after AuthorizationStateReady, so it is a
    # reliable "this session has logged in successfully" marker.
    dc run --rm --no-deps -T bot python -c \
        'import os, sys; from config import settings; d = settings.TD_FILES_DIRECTORY; hb = "data/bot_heartbeat"; sys.exit(0 if os.path.isdir(d) and os.listdir(d) and os.path.exists(hb) else 1)' \
        >/dev/null 2>&1
}

has_reranker() {
    dc run --rm --no-deps -T bot python -c \
        'import os, sys; p = os.environ.get("RERANKER_MODEL_PATH", "").strip() or "models/rerankers/BGE-Reranker-v2-m3"; sys.exit(0 if os.path.isdir(p) and os.listdir(p) else 1)' \
        >/dev/null 2>&1
}

install_reranker() {
    info "Downloading BGE reranker..."
    dc run --rm --no-deps bot python install_reranker.py \
        || die "reranker download failed (check network and ./models permissions)"
}

check_reranker() {
    if has_reranker; then
        info "Reranker already present."
        return 0
    fi
    info "Reranker not found in models/."
    if [ -t 0 ] && [ -t 1 ]; then
        read -r -p "Download it now? [Y/n] " answer || answer=""
        case "$answer" in
            [nN]*)
                warn "Skipping reranker; search_memory will use FAISS order (lower quality)."
                ;;
            *)
                install_reranker
                ;;
        esac
    else
        warn "Non-interactive shell: skipping reranker. Run ./start/docker.sh --reranker."
    fi
}

perform_login() {
    info "Stopping bot before TDLib login (only one client may use the session)..."
    dc stop bot >/dev/null 2>&1 || true

    local args=()
    [ "$QR" -eq 1 ] && args+=(--qr)
    if ! dc run --rm -it --no-deps bot python create_session.py ${args[@]+"${args[@]}"}; then
        if [ "$QR" -eq 1 ]; then
            die "TDLib login failed. Re-run: ./start/docker.sh --login --qr"
        fi
        die "TDLib login failed. Re-run: ./start/docker.sh --login"
    fi
}

check_session() {
    if has_session; then
        info "TDLib session already present."
        return 0
    fi

    cat >&2 <<'EOF'
TDLib session not found in the mila_data volume.
The bot cannot log in without interactive input.
EOF
    if [ -t 0 ] && [ -t 1 ]; then
        read -r -p "Log in to Telegram now? [Y/n] " answer || answer=""
        case "$answer" in
            [nN]*)
                warn "Starting without a session; the bot will wait for authorization."
                ;;
            *)
                perform_login
                ;;
        esac
    else
        cat >&2 <<'EOF'
Non-interactive shell: run `./start/docker.sh --login` (or `--login --qr`) first.
EOF
        exit 1
    fi
}

start_containers() {
    if [ "$FORCE_RECREATE" -eq 1 ]; then
        info "Recreating containers..."
        dc up -d --force-recreate || die "docker compose up failed"
        return 0
    fi

    if container_exists bot && container_exists observatory; then
        if container_running bot && container_running observatory; then
            info "Containers are already running; leaving them as is."
        else
            info "Starting existing containers..."
            dc start || die "docker compose start failed"
        fi
        return 0
    fi

    info "Creating missing containers (existing ones are not recreated)..."
    dc up -d --no-recreate || die "docker compose up failed"
}

show_url() {
    local port host mapping
    port="$(env_get OBSERVATORY_PORT)"
    port="${port:-8080}"
    mapping="$(dc port observatory "$port" 2>/dev/null | head -n 1)" || mapping=""
    if [ -n "$mapping" ]; then
        host="${mapping%:*}"
        case "$host" in
            "0.0.0.0"|"::"|"[::]") host="127.0.0.1" ;;
        esac
        info "Observatory: http://$host:$port"
    else
        info "Observatory port ${port} is not published yet"
    fi
}

case "$ACTION" in
    down)
        ensure_env
        dc down || die "docker compose down failed"
        info "Containers removed; named volumes (data, logs) are kept."
        exit 0
        ;;
    logs)
        ensure_env
        exec "${DC[@]}" logs -f --tail=100
        ;;
    status)
        ensure_env
        dc ps
        exit 0
        ;;
esac

ensure_env
check_required_env

case "$ACTION" in
    reranker)
        ensure_image
        install_reranker
        exit 0
        ;;
esac

ensure_image

case "$ACTION" in
    login)
        perform_login
        check_reranker
        start_containers
        ;;
    restart)
        if container_exists bot && container_exists observatory; then
            dc restart || die "docker compose restart failed"
        else
            start_containers
        fi
        ;;
    up)
        check_session
        check_reranker
        start_containers
        ;;
esac

show_url
dc ps
