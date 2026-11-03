"""Hermetic test bootstrap.

Everything here runs BEFORE any project module is imported, so `config.py`
never sees the developer's real `.env` or the process environment. Tests must
work on a clean checkout: no secrets, no network, no real `data/`.
"""

import os
import tempfile
from pathlib import Path

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="mila-tests-"))

# Required by config.Settings; values are intentionally fake.
_DUMMY_ENV = {
    "API_ID": "1",
    "API_HASH": "test-api-hash",
    "PHONE_NUMBER": "",
    "LLM_API_BASE": "http://127.0.0.1:9/v1",
    "LLM_API_KEY": "test-key",
    "MAIN_LLM_MODEL": "test-model",
    "EMBED_API_BASE": "http://127.0.0.1:9/v1",
    "EMBED_API_KEY": "test-key",
    "EMBED_MODEL": "test-embed",
    # Never touch the real TDLib session or runtime files.
    "TD_FILES_DIRECTORY": str(_TMP_ROOT / "tdlib"),
    "SQLITE_PATH": str(_TMP_ROOT / "mila.db"),
    "CONTEXT_DB_PATH": str(_TMP_ROOT / "context_buffer.db"),
    "FAISS_TEXT_PATH": str(_TMP_ROOT / "faiss_text.index"),
    "FAISS_CHUNK_PATH": str(_TMP_ROOT / "faiss_chunks.index"),
    "FAISS_IMAGE_PATH": str(_TMP_ROOT / "faiss_image.index"),
    "PHOTOS_DIR": str(_TMP_ROOT / "photos"),
    "MEDIA_DIR": str(_TMP_ROOT / "media"),
    "DAILY_REPORTS": str(_TMP_ROOT / "daily reports"),
    "LOG_DIR": str(_TMP_ROOT / "logs"),
    "RERANKER_MODEL_PATH": str(_TMP_ROOT / "reranker"),
    "LOCAL_TIMEZONE": "UTC",
    # Never read/write the developer's real runtime_config.json.
    "MILA_RUNTIME_CONFIG_PATH": str(_TMP_ROOT / "runtime_config.json"),
}
# Force overrides: a real exported variable must not leak into tests.
os.environ.update(_DUMMY_ENV)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Remove the session temp dir unless a test asked to keep it."""
    if os.getenv("MILA_TEST_KEEP_TMP") != "1":
        import shutil

        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
