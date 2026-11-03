import os
from urllib.parse import urlparse, urlunparse
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()


def normalize_api_base(url: str) -> str:
    """Ensure the API base URL ends with /v1.
    
    LM Studio expects /v1/chat/completions, but if user provides
    http://127.0.0.1:1234 (without /v1), the openai client will
    POST to /chat/completions which fails.
    OpenRouter already has /v1 in the path, so it stays unchanged.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        new_path = path + "/v1" if path else "/v1"
        parsed = parsed._replace(path=new_path)
    return urlunparse(parsed)

class Settings(BaseSettings):
    # Telegram
    API_ID: int
    API_HASH: str
    PHONE_NUMBER: str = ""  # Phone number for auth
    DEVICE_MODEL: str = "PC 64bit"
    SYSTEM_VERSION: str = "Windows 11"
    APP_VERSION: str = "5.0.1"

    # TDLib (pytdbot) storage.
    # files_directory holds both the TDLib database and downloaded files.
    # It replaces the old Pyrogram *.session file.
    TD_FILES_DIRECTORY: str = "data/tdlib"
    TD_DATABASE_ENCRYPTION_KEY: str = "mila-tdlib-key-change-me"
    TD_VERBOSITY: int = 2

    # LLM Models
    LLM_API_BASE: str
    LLM_API_KEY: str
    # Optional separate key for the night LLM. Empty = use LLM_API_KEY.
    NIGHT_LLM_API_KEY: str = ""
    
    # Main LLM (Brain - daytime)
    MAIN_LLM_MODEL: str
    MAIN_LLM_MAX_TOKENS: int = 2048
    MAIN_LLM_TEMPERATURE: float = 0.7

    # Embeddings
    EMBED_API_BASE: str
    EMBED_API_KEY: str
    EMBED_MODEL: str
    EMBED_DIM: int = 384  # Vector dimension (nomic-embed-text=384, text-embedding-3-small=1536, etc.)

    # CLIP & Moondream local. Did't use yet
    CLIP_MODEL_PATH: str = "models/clip-vit-base-patch32"
    MOONDREAM_MODEL_PATH: str = "models/moondream-2b"

    # FAISS & Reranker
    FAISS_TEXT_PATH: str = "data/faiss_text.index"
    # Conversation chunks live in a separate index so their integer ids do
    # not collide with memories.faiss_id and search_memory stays user-fact only.
    FAISS_CHUNK_PATH: str = "data/faiss_chunks.index"
    FAISS_IMAGE_PATH: str = "data/faiss_image.index"
    FAISS_SAVE_INTERVAL_MIN: int = 10
    # Background reindex of rows whose faiss_id is NULL (embedding failure,
    # crash between insert and index, or rows written by an older version).
    MEMORY_REINDEX_INTERVAL_SEC: int = 120
    MEMORY_REINDEX_BATCH: int = 200
    RERANKER_MODEL_PATH: str = "models/rerankers/BGE-Reranker-v2-m3"
    RERANKER_THRESHOLD: float = 0.6

    # Memory Decay
    DECAY_LAMBDA: float = 0.01
    FORGET_THRESHOLD: float = 0.3

    # Data
    SQLITE_PATH: str = "data/mila.db"
    PHOTOS_DIR: str = "data/photos"
    MEDIA_DIR: str = "data/media"
    MAX_MEDIA_MB: int = 20
    DAILY_REPORTS: str = "data/daily reports"

    # Pipeline
    MAX_CONTEXT_TOKENS: int = 10000
    MAX_STM_MESSAGES: int = 30
    SIMILARITY_THRESHOLD_IMAGE: float = 0.85
    MAIN_LLM_THOUGHT_SENTENCES: int = 3
    LOG_DIR: str = "logs"

    # Context buffer
    CONTEXT_DB_PATH: str = "data/context_buffer.db"
    CONTEXT_MAX_TOKENS: int = 30000  # Max tokens in context before summarization (messages only, excludes system prompt)
    CONTEXT_SUMMARIZE_CHUNK_TOKENS: int = 5000  # How many tokens worth of oldest messages to compress at once
    CONTEXT_MAX_MESSAGES: int = 40  # Legacy — kept for backward compat, but token limit takes precedence
    CONTEXT_SUMMARIZE_CHUNK: int = 20  # Legacy

    # Local time for message metadata (IANA name, e.g. "Europe/Moscow").
    # Empty = use the system's local timezone (honors TZ env / mounted /etc/localtime),
    # which works both inside Docker (when TZ is set) and on the host.
    LOCAL_TIMEZONE: str = ""

    # Observatory web panel. `OBSERVATORY_HOST` is the bind address used by a
    # direct (non-Docker) run; docker-compose overrides it with
    # OBSERVATORY_BIND_HOST (default 0.0.0.0) and publishes the port on
    # OBSERVATORY_PUBLISH_HOST:OBSERVATORY_PORT (default 127.0.0.1:8080).
    # If OBSERVATORY_PASSWORD is empty, auth is off: keep the panel localhost-only.
    OBSERVATORY_USERNAME: str = "admin"
    OBSERVATORY_PASSWORD: str = ""
    OBSERVATORY_HOST: str = "127.0.0.1"
    OBSERVATORY_PORT: int = 8080

    # Message batching / debounce
    MESSAGE_DEBOUNCE_SECONDS: float = 2.0  # Wait window after the last message before responding
    # Full peer-typing support (TDLib updateChatAction):
    # if the human is typing, the bot stops and waits for their message.
    ENABLE_TYPING_WAIT: bool = True        # Master switch for typing-aware waits/aborts
    TYPING_WAIT_MAX_SECONDS: float = 8.0   # Upper bound for waiting while peer keeps typing
    TYPING_FRESHNESS_SECONDS: float = 6.0  # How long one typing event counts as "still typing"
    TYPING_POLL_INTERVAL: float = 0.5      # Poll step while waiting for typing/message
    TYPING_GRACE_SECONDS: float = 2.0      # Wait after typing stops for the message to arrive
    TYPING_ABORT_GENERATION: bool = True   # Cancel LLM run/send if peer starts typing

    # Presence: auto-exit from dialog to rest after silence in focus chat.
    DIALOG_IDLE_TIMEOUT_SECONDS: float = 300.0  # 5 min without messages in focus chat → rest
    DIALOG_IDLE_CHECK_INTERVAL_SECONDS: float = 30.0  # how often the watcher ticks

    class Config:
        env_file = ".env"
        extra = "allow"  # Allow extra fields from .env (night_llm, sleep, etc.) to be used by runtime_config

settings = Settings()


def get_night_llm_api_key() -> str:
    """API key for the night LLM: NIGHT_LLM_API_KEY from .env, else LLM_API_KEY."""
    return (getattr(settings, "NIGHT_LLM_API_KEY", "") or "").strip() or settings.LLM_API_KEY
