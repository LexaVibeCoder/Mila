import openai
import numpy as np
from typing import List
from config import settings, normalize_api_base

class EmbeddingClient:
    def __init__(self):
        self.client = openai.AsyncOpenAI(
            base_url=normalize_api_base(settings.EMBED_API_BASE),
            api_key=settings.EMBED_API_KEY
        )
        self.model = settings.EMBED_MODEL

    async def embed(self, texts: List[str]) -> np.ndarray:
        response = await self.client.embeddings.create(
            model=self.model,
            input=texts
        )
        # Сортируем по индексу, возвращаем массив [n_texts, dim]
        vectors = [data.embedding for data in sorted(response.data, key=lambda x: x.index)]
        return np.array(vectors, dtype=np.float32)