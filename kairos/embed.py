"""Optional local embeddings (KAIROS_EMBEDDINGS=1, needs sentence-transformers)."""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    def embed(self, text: str) -> list[float] | None: ...


class NullEmbedder:
    def embed(self, text: str) -> list[float] | None:
        return None


class LocalEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model)

    def embed(self, text: str) -> list[float] | None:
        return self.model.encode(text, normalize_embeddings=True).tolist()


def make_embedder(enabled: bool) -> Embedder:
    if not enabled:
        return NullEmbedder()
    try:
        return LocalEmbedder()
    except ImportError:
        return NullEmbedder()
