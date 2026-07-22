import openai
import httpx
import numpy as np
from typing import List
from urllib.parse import urlparse
from config import settings, normalize_api_base


def _is_ollama_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if "ollama" in hostname.lower():
        return True
    if parsed.port == 11434:
        return True
    return False


class EmbeddingClient:
    def __init__(self):
        self.model = settings.EMBED_MODEL
        self.api_base = settings.EMBED_API_BASE.rstrip("/")
        self.api_key = settings.EMBED_API_KEY
        self._openai_client = None
        self._prefer_ollama = _is_ollama_url(self.api_base)

    def _get_openai_client(self) -> openai.AsyncOpenAI:
        if self._openai_client is None:
            self._openai_client = openai.AsyncOpenAI(
                base_url=normalize_api_base(settings.EMBED_API_BASE),
                api_key=self.api_key or "",
            )
        return self._openai_client

    async def embed(self, texts: List[str]) -> np.ndarray:
        if self._prefer_ollama:
            return await self._try_order(["ollama", "openai"], texts)
        else:
            return await self._try_order(["openai", "ollama"], texts)

    async def _try_order(self, order: List[str], texts: List[str]) -> np.ndarray:
        last_error = None
        for method in order:
            try:
                if method == "openai":
                    return await self._embed_openai(texts)
                else:
                    return await self._embed_ollama(texts)
            except Exception as e:
                last_error = e
                method_name = "OpenAI-compatible" if method == "openai" else "Ollama native"
                print(f"[Embedding] {method_name} failed: {e}")
        raise RuntimeError(
            f"All embedding backends failed. Last error: {last_error}"
        )

    async def _embed_openai(self, texts: List[str]) -> np.ndarray:
        client = self._get_openai_client()
        response = await client.embeddings.create(
            model=self.model,
            input=texts,
        )
        vectors = [
            data.embedding
            for data in sorted(response.data, key=lambda x: x.index)
        ]
        return np.array(vectors, dtype=np.float32)

    async def _embed_ollama(self, texts: List[str]) -> np.ndarray:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.api_base}/api/embed",
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
            return np.array(data["embeddings"], dtype=np.float32)
