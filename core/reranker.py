# core/reranker.py
"""
BGE-Reranker-v2-m3 integration for evaluating document relevance.
Threshold-based filtering with hard cutoff at 0.6.
"""

import os
from pathlib import Path
from typing import List, Tuple
import numpy as np
from config import settings

class RerankerClient:
    """
    Local reranker using BGE-Reranker-v2-m3.
    Lazy loads the model on first use.
    """
    
    def __init__(self):
        self.model = None
        self.tokenizer = None
        self.model_path = settings.RERANKER_MODEL_PATH
        self.threshold = settings.RERANKER_THRESHOLD
        self._check_model_exists()
    
    def _check_model_exists(self):
        """Verify reranker model exists locally."""
        if not Path(self.model_path).exists():
            raise FileNotFoundError(
                f"Reranker model not found at {self.model_path}. "
                f"Run: python install_reranker.py"
            )
    
    def _load_model(self):
        """Lazy load the reranker model."""
        if self.model is not None:
            return
        
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            raise ImportError(
                "sentence-transformers not installed. "
                "Run: pip install sentence-transformers"
            )
        
        print(f"Loading reranker from {self.model_path}...")
        self.model = CrossEncoder(self.model_path)
        print("✓ Reranker loaded")
    
    async def rerank(
        self, 
        query: str, 
        documents: List[str]
    ) -> List[Tuple[str, float]]:
        """
        Rerank documents by relevance to query.
        Returns list of (document, score) tuples, filtered by threshold.
        
        Args:
            query: Search query
            documents: List of document texts
        
        Returns:
            List of (document, score) tuples where score >= threshold,
            sorted by score descending
        """
        if not documents:
            return []
        
        self._load_model()
        
        # Prepare pairs: (query, document)
        pairs = [[query, doc] for doc in documents]
        
        # Run reranker
        scores = self.model.predict(pairs)
        
        # Filter by threshold and sort
        results = [
            (doc, float(score))
            for doc, score in zip(documents, scores)
            if float(score) >= self.threshold
        ]
        results.sort(key=lambda x: x[1], reverse=True)
        
        return results
    
    def rerank_sync(
        self,
        query: str,
        documents: List[str]
    ) -> List[Tuple[str, float]]:
        """Synchronous wrapper for rerank (for backwards compatibility)."""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        
        if loop is None:
            # No running loop, run directly
            import nest_asyncio
            nest_asyncio.apply()
            return asyncio.run(self.rerank(query, documents))
        else:
            # Already in async context, create task
            return asyncio.ensure_future(self.rerank(query, documents))
