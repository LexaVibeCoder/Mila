"""Opt-in comparison of OpenRouter decisions and cosine similarity on Russian drafts."""

import os

import numpy as np
import pytest
from openai import AsyncOpenAI, OpenAIError

from config import OPENROUTER_API_BASE, OPENROUTER_DECISION_API_BASE, settings
from core.diary.decision import decision_api_base, review_draft


_REFLECTIONS = [
    {"id": 1, "content_md": "Когда все смотрят, я боюсь выступать у доски."},
    {"id": 2, "content_md": "Мне легче сосредоточиться на задаче, если вокруг тихо."},
    {"id": 3, "content_md": "Я расстроилась, когда эксперимент с бумажным мостом не удался."},
    {"id": 4, "content_md": "Мне нравится рассматривать звёзды в одиночестве."},
]
# (Draft, expected decision, expected entry ID)
_CASES = [
    ("Я нервничаю, когда нужно выйти к доске перед всеми.", "duplicate", 1),
    ("В тишине мне проще удерживать внимание на задаче.", "duplicate", 2),
    ("Неудача опыта с бумажным мостом меня огорчила.", "duplicate", 3),
    ("Мне по-прежнему страшно у доски, но сегодня помогло заранее написать план.", "update_suggested", 1),
    ("На звёзды теперь хочется смотреть вместе с кем-нибудь, одной стало скучно.", "update_suggested", 4),
    ("После неудачи бумажного моста я нашла ошибку: опоры стояли слишком далеко.", "update_suggested", 3),
    ("Я нервничаю, когда впервые иду по незнакомому маршруту.", "new", None),
    ("Теперь мне проще сосредоточиться на дыхании во время тренировки.", "new", None),
    ("Меня радует, когда после ремонта часы снова начинают тикать.", "new", None),
]


@pytest.mark.integration
async def test_openrouter_diary_benchmark(monkeypatch):
    if os.getenv("MILA_RUN_DIARY_BENCHMARK") != "1":
        pytest.skip("Opt in with MILA_RUN_DIARY_BENCHMARK=1")
    key = os.getenv("DECISION_API_KEY", "").strip()
    model = os.getenv("DECISION_MODEL", "").strip()
    api_base = decision_api_base(os.getenv("DECISION_API_BASE", ""))
    if not key or not model:
        pytest.fail("Set DECISION_API_KEY and DECISION_MODEL in the process environment", pytrace=False)
    if api_base != OPENROUTER_DECISION_API_BASE:
        pytest.fail("This benchmark uses OpenRouter for both decisions and embeddings", pytrace=False)
    monkeypatch.setattr(settings, "DECISION_API_BASE", api_base)
    monkeypatch.setattr(settings, "DECISION_API_KEY", key)
    monkeypatch.setattr(settings, "DECISION_MODEL", model)

    texts = [row["content_md"] for row in _REFLECTIONS] + [text for text, _, _ in _CASES]
    try:
        async with AsyncOpenAI(base_url=OPENROUTER_API_BASE, api_key=key, timeout=15.0, max_retries=1) as client:
            response = await client.embeddings.create(model="openai/text-embedding-3-small", input=texts)
    except OpenAIError as exc:
        pytest.fail(f"OpenRouter embedding request failed ({type(exc).__name__})", pytrace=False)
    vectors = np.array([row.embedding for row in sorted(response.data, key=lambda row: row.index)])
    assert len(vectors) == len(texts)
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    scores = vectors[len(_REFLECTIONS):] @ vectors[:len(_REFLECTIONS)].T
    baseline = ["duplicate" if float(row.max()) >= 0.85 else "new" for row in scores]

    predictions = []
    correct_decisions = 0
    for draft, expected, expected_id in _CASES:
        review = await review_draft(draft, _REFLECTIONS)
        if review is None:
            pytest.fail("OpenRouter decision response unavailable or invalid", pytrace=False)
        actual, candidate = review.match or ("new", None)
        if actual == "replace":
            actual = "update_suggested"
        actual_id = candidate["id"] if candidate else None
        predictions.append(actual)
        correct_decisions += (actual, actual_id) == (expected, expected_id)

    for name, results in (("decision", predictions), ("embeddings", baseline)):
        missed_duplicates = sum(
            expected == "duplicate" and actual == "new"
            for (_, expected, _), actual in zip(_CASES, results)
        )
        new_entries_blocked = sum(
            expected == "new" and actual != "new"
            for (_, expected, _), actual in zip(_CASES, results)
        )
        print(f"{name}: missed_duplicates={missed_duplicates}/3, new_entries_blocked={new_entries_blocked}/3")
    print(f"decision: correct_label_and_id={correct_decisions}/{len(_CASES)}")
