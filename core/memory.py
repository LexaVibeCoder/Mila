import faiss
import numpy as np
import os
import time
from typing import List, Tuple
from config import settings

class VectorMemory:
    def __init__(self, index_path: str, dim: int):
        self.index_path = index_path
        self.dim = dim
        self.index = None
        self.load_or_create()

    def load_or_create(self):
        if os.path.exists(self.index_path):
            self.index = faiss.read_index(self.index_path)
            print(f"Loaded index from {self.index_path}, size: {self.index.ntotal}")
        else:
            # IndexIDMap над IndexFlatL2
            base_index = faiss.IndexFlatL2(self.dim)
            self.index = faiss.IndexIDMap(base_index)
            print(f"Created new index with dim {self.dim}")

    def add(self, vectors: np.ndarray, ids: List[int]):
        """Добавление векторов с заданными ID (инкрементально)."""
        if self.index is None:
            raise RuntimeError("Index not initialized")
        self.index.add_with_ids(vectors.astype(np.float32), np.array(ids, dtype=np.int64))
        print(f"Added {len(ids)} vectors, total: {self.index.ntotal}")

    def search(self, query_vector: np.ndarray, k: int = 5) -> Tuple[np.ndarray, np.ndarray]:
        """Возвращает расстояния и ID ближайших соседей."""
        if self.index is None:
            return np.array([]), np.array([])
        query = query_vector.astype(np.float32).reshape(1, -1)
        distances, indices = self.index.search(query, k)
        return distances[0], indices[0]

    def save(self):
        if self.index and self.index.ntotal > 0:
            faiss.write_index(self.index, self.index_path)
            print(f"Index saved to {self.index_path}")

    def rebuild_from_active(self, active_ids: List[int], all_vectors: np.ndarray):
        """Пересоздаёт индекс только с активными ID (чистка забытых фактов)."""
        # active_ids – список int ID, all_vectors – массив векторов для этих ID (в том же порядке)
        new_index = faiss.IndexIDMap(faiss.IndexFlatL2(self.dim))
        if len(active_ids) > 0:
            new_index.add_with_ids(all_vectors.astype(np.float32), np.array(active_ids, dtype=np.int64))
        self.index = new_index
        self.save()