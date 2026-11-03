"""Live check for the sleep summarizer model (scaledown/summarize).

Network + paid OpenRouter request, so it is excluded from the default run.
Integration mode keeps the real `LLM_API_KEY` / `LLM_API_BASE` from `.env`
(see `tests/conftest.py`), so no extra environment variable is needed:

    .venv/bin/python -m pytest -m integration \
        tests/test_scaledown_summarize_integration.py

`OPENROUTER_API_KEY` and `SCALEDOWN_API_BASE` still override the `.env` values.

The dialog below is synthetic: no real user data.
"""

import os
import time

import httpx
import pytest

from config import settings

MODEL = "scaledown/summarize"


def _api_key() -> str:
    return (os.getenv("OPENROUTER_API_KEY") or
            os.getenv("SCALEDOWN_API_KEY") or
            settings.LLM_API_KEY or "").strip()


def _api_base() -> str:
    return (os.getenv("SCALEDOWN_API_BASE") or
            settings.LLM_API_BASE or
            "https://openrouter.ai/api/v1").strip()


API_KEY = _api_key()
API_BASE = _api_base()

_SKIP_REASON = ""
if not API_KEY or API_KEY == "test-key":
    _SKIP_REASON = "LLM_API_KEY is not configured in .env"
elif "127.0.0.1:9" in API_BASE:
    _SKIP_REASON = "hermetic env; run with -m integration to use .env"
elif "openrouter.ai" not in API_BASE.lower() and not os.getenv("SCALEDOWN_API_BASE"):
    _SKIP_REASON = "LLM_API_BASE is not OpenRouter; set SCALEDOWN_API_BASE"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON),
]

DIALOG = """[2026-10-01] Пользователь: Мы перенесли деплой проекта «Вега» с пятницы на вторник из-за релизного freeze.
[2026-10-02] Ассистент: Дежурный в эту смену — Игорь, миграция billing_invoice_runs блокирует запись на 40 секунд.
[2026-10-03] Пользователь: Старый мок через jest.spyOn не работает, код кэширует Date.now() при импорте.
[2026-10-04] Ассистент: Задачу оформили как BILL-482, ветка fix/invoice-clock-injection, ревьюер @mara.
"""

INSTRUCTIONS = (
    "Сожми диалог в краткое резюме на русском языке (2–4 предложения). "
    "Сохрани ключевые темы, решения и договорённости, эмоциональный тон. "
    "Используй только факты из диалога, без домыслов и вступлений."
)

FACTS = ["Вега", "вторник", "billing_invoice_runs", "BILL-482"]


def _cyrillic_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    cyrillic = sum(1 for ch in letters if "\u0400" <= ch <= "\u04FF")
    return cyrillic / len(letters)


@pytest.mark.asyncio
async def test_scaledown_summarize_russian_dialog():
    async with httpx.AsyncClient(timeout=60) as client:
        started = time.perf_counter()
        response = await client.post(
            f"{API_BASE.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": INSTRUCTIONS},
                    {"role": "user", "content": DIALOG},
                ],
                "max_tokens": 300,
                "temperature": 0.0,
            },
        )
        elapsed = time.perf_counter() - started

    assert response.status_code == 200, response.text
    body = response.json()
    summary = (body["choices"][0]["message"]["content"] or "").strip()

    assert summary, "summarizer returned an empty summary"
    assert elapsed < 60
    # The model follows the instruction language, not the input language.
    assert _cyrillic_ratio(summary) >= 0.5, summary
    # Abstractive summary must actually shorten the input.
    assert len(summary) < len(DIALOG) * 0.8, summary
    # One-off facts may be dropped; require at least half of the planted ones.
    kept = [fact for fact in FACTS if fact.lower() in summary.lower()]
    assert len(kept) >= 2, f"kept {kept} in {summary!r}"


@pytest.mark.asyncio
async def test_scaledown_summarize_rejects_tool_messages():
    """Tool-role messages are a 400: the sleep worker must pass plain text."""
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{API_BASE.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": MODEL,
                "messages": [
                    {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
                ],
            },
        )

    assert response.status_code == 400
    assert "tool" in response.json()["error"]["message"].lower()
