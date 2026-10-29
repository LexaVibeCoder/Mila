"""
Script to download and setup BGE-Reranker-v2-m3 model locally.
This runs independently to avoid cluttering the main project.
"""

import os
import sys
from pathlib import Path

def install_reranker():
    """Download BGE-Reranker-v2-m3 from HuggingFace."""
    reranker_path = Path("models/rerankers/BGE-Reranker-v2-m3")
    
    if reranker_path.exists():
        print(f"✓ Reranker already exists at {reranker_path}")
        return True
    
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("ERROR: huggingface_hub not found. Install it with:")
        print("  pip install huggingface_hub")
        return False
    
    print(f"Downloading BGE-Reranker-v2-m3 to {reranker_path}...")
    
    try:
        snapshot_download(
            repo_id="BAAI/bge-reranker-v2-m3",
            local_dir=str(reranker_path),
            repo_type="model",
            token=False,
        )
        print(f"✓ Reranker installed successfully at {reranker_path}")
        return True
    except Exception as e:
        print(f"ERROR: Failed to download reranker: {e}")
        return False

if __name__ == "__main__":
    success = install_reranker()
    sys.exit(0 if success else 1)
