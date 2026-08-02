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
        # Add /v1 to the path
        new_path = path + "/v1" if path else "/v1"
        parsed = parsed._replace(path=new_path)
    return urlunparse(parsed)

class Settings(BaseSettings):
    # Telegram
    API_ID: int
    API_HASH: str
    PHONE_NUMBER: str = ""  # Номер телефона для авторизации Pyrogram (+7...)
    DEVICE_MODEL: str = "PC 64bit"
    SYSTEM_VERSION: str = "Windows 11"
    APP_VERSION: str = "5.0.1"

    # LLM Models
    LLM_API_BASE: str
    LLM_API_KEY: str
    
    # Main LLM (Brain - daytime)
    MAIN_LLM_MODEL: str
    MAIN_LLM_MAX_TOKENS: int = 2048
    MAIN_LLM_TEMPERATURE: float = 0.7

    # Embeddings
    EMBED_API_BASE: str
    EMBED_API_KEY: str
    EMBED_MODEL: str
    EMBED_DIM: int = 384  # Vector dimension (nomic-embed-text=384, text-embedding-3-small=1536, etc.)

    # CLIP & Moondream локальные
    CLIP_MODEL_PATH: str = "models/clip-vit-base-patch32"
    MOONDREAM_MODEL_PATH: str = "models/moondream-2b"

    # FAISS & Reranker
    FAISS_TEXT_PATH: str = "data/faiss_text.index"
    FAISS_IMAGE_PATH: str = "data/faiss_image.index"
    FAISS_SAVE_INTERVAL_MIN: int = 10
    RERANKER_MODEL_PATH: str = "models/rerankers/BGE-Reranker-v2-m3"
    RERANKER_THRESHOLD: float = 0.6

    # Memory Decay
    DECAY_LAMBDA: float = 0.01
    FORGET_THRESHOLD: float = 0.3

    # Data
    SQLITE_PATH: str = "data/mila.db"
    PHOTOS_DIR: str = "data/photos"
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

    # Message batching / debounce
    MESSAGE_DEBOUNCE_SECONDS: float = 2.0  # Wait window after the last message before responding
    TYPING_WAIT_MAX_SECONDS: float = 8.0   # Max extra wait if the user is still typing
    ENABLE_TYPING_WAIT: bool = False       # Off by default — typing detection isn't reliable yet

    class Config:
        env_file = ".env"
        extra = "allow"  # Allow extra fields from .env (night_llm, sleep, etc.) to be used by runtime_config

settings = Settings()