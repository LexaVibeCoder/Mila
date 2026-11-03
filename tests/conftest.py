"""Hermetic test bootstrap.

Everything here runs BEFORE any project module is imported, so unit tests never
see the developer's real `.env` or process environment. Tests must work on a
clean checkout: no secrets, no network, no real `data/`.

Integration tests are the exception: when they are explicitly selected
(`pytest -m integration`), the LLM_* variables are left alone so `config.py`
can load the real `.env` (`LLM_API_KEY`). Everything else (DB/FAISS/runtime
paths) still points at the temp directory.
"""

import os
import sys
import tempfile
from pathlib import Path

from dotenv import find_dotenv

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

# Real LLM settings are allowed only when integration tests are selected.
_LLM_KEYS = ("LLM_API_BASE", "LLM_API_KEY", "MAIN_LLM_MODEL")


def _integration_requested() -> bool:
    """Detect `-m integration` in argv / PYTEST_ADDOPTS.

    conftest runs before pytest parses options, so read the raw arguments.
    `not integration` (the default addopts in pytest.ini) keeps tests hermetic.
    """
    if os.getenv("MILA_TEST_INTEGRATION") == "1":
        return True

    argv = list(sys.argv[1:])
    extra = os.getenv("PYTEST_ADDOPTS", "")
    if extra:
        argv += extra.split()

    for index, arg in enumerate(argv):
        expr = None
        if arg == "-m" and index + 1 < len(argv):
            expr = argv[index + 1]
        elif arg.startswith("-m") and len(arg) > 2:
            expr = arg[2:]
        if expr and "integration" in expr and "not integration" not in expr:
            return True
    return False


_INTEGRATION = _integration_requested()
_ENV = dict(_DUMMY_ENV)
if _INTEGRATION:
    # Keep the stubs on a clean checkout (no .env, no exported LLM vars) so
    # collection still succeeds and the integration tests skip themselves.
    # `find_dotenv(usecwd=True)` matches config.py's `load_dotenv()` search.
    has_real_config = bool(find_dotenv(usecwd=True)) or any(
        os.getenv(key) for key in _LLM_KEYS
    )
    if has_real_config:
        for key in _LLM_KEYS:
            _ENV.pop(key, None)
os.environ.update(_ENV)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Remove the session temp dir unless a test asked to keep it."""
    if os.getenv("MILA_TEST_KEEP_TMP") != "1":
        import shutil

        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
