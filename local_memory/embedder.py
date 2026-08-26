"""Lazy, local-only text embeddings shared by the Synth bridge."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from fastembed import TextEmbedding

DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBED_DIMENSION = 384


class EmbeddingUnavailableError(RuntimeError):
    """Raised when the prepared local model cannot serve an embedding request."""


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    model: str
    dimension: int
    embeddings: list[list[float]]


class LocalEmbedder:
    """Load FastEmbed only on first use and never download from a runtime request."""

    def __init__(self, workspace: Path, *, model_name: str | None = None) -> None:
        self._model_name = model_name or os.environ.get(
            "SYNTH_EMBED_MODEL", DEFAULT_EMBED_MODEL
        )
        configured_cache = os.environ.get("SYNTH_FASTEMBED_CACHE_DIR")
        self._cache_dir = (
            Path(configured_cache).expanduser().resolve()
            if configured_cache
            else (workspace / ".synth" / "models" / "fastembed").resolve()
        )
        self._backend: TextEmbedding | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _backend_or_raise(self) -> TextEmbedding:
        if self._backend is not None:
            return self._backend
        try:
            self._backend = TextEmbedding(
                model_name=self._model_name,
                cache_dir=str(self._cache_dir),
                local_files_only=True,
            )
        except Exception as error:
            raise EmbeddingUnavailableError(
                f"local embedding model is unavailable: {self._model_name}"
            ) from error
        return self._backend

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        try:
            raw_vectors = cast(
                Iterable[Sequence[float]], self._backend_or_raise().embed(list(texts))
            )
            embeddings = [
                [float(component) for component in vector] for vector in raw_vectors
            ]
        except EmbeddingUnavailableError:
            raise
        except Exception as error:
            raise EmbeddingUnavailableError(
                f"local embedding failed for model: {self._model_name}"
            ) from error

        if len(embeddings) != len(texts) or not embeddings:
            raise EmbeddingUnavailableError(
                "local embedding returned an unexpected number of vectors"
            )
        dimension = len(embeddings[0])
        if dimension <= 0 or any(len(vector) != dimension for vector in embeddings):
            raise EmbeddingUnavailableError(
                "local embedding returned inconsistent vector dimensions"
            )
        if (
            self._model_name == DEFAULT_EMBED_MODEL
            and dimension != DEFAULT_EMBED_DIMENSION
        ):
            raise EmbeddingUnavailableError(
                f"{DEFAULT_EMBED_MODEL} returned dimension {dimension}, "
                f"expected {DEFAULT_EMBED_DIMENSION}"
            )
        return EmbeddingBatch(
            model=self._model_name,
            dimension=dimension,
            embeddings=embeddings,
        )
