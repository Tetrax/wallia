"""Service d'embeddings.

Backend `e5` (production) : intfloat/multilingual-e5-small, révision épinglée,
CPU uniquement, préfixes `query: `/`passage: `, normalisation L2, fenêtre 512.
Backend `fixture` (tests CI uniquement, interdit en production) : vecteurs
déterministes dérivés de trigrammes — mécanique testable, aucune sémantique.
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any

from .config import Settings, get_settings


class EmbeddingsUnavailable(RuntimeError):
    """Le modèle d'embeddings n'est pas disponible."""


class EmbeddingService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backend = settings.embedding_backend
        self.dim = settings.embedding_dim
        self._model: Any = None
        self._lock = threading.Lock()

    # -- informations publiques (jamais de secret) -------------------------
    @property
    def info(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.settings.embedding_model,
            "revision": self.settings.embedding_revision,
            "dim": self.dim,
            "device": "cpu",
        }

    def _load_e5(self) -> Any:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            model_dir = self.settings.model_dir
            if not model_dir.is_dir():
                raise EmbeddingsUnavailable(
                    f"modèle e5 introuvable dans {model_dir} (image incomplète ?)"
                )
            try:
                from sentence_transformers import SentenceTransformer
            except Exception as exc:  # pragma: no cover - dépendance image
                raise EmbeddingsUnavailable(f"sentence-transformers indisponible: {exc}") from exc
            try:
                self._model = SentenceTransformer(
                    str(model_dir), device="cpu", local_files_only=True
                )
            except Exception as exc:
                raise EmbeddingsUnavailable(f"chargement du modèle e5 impossible: {exc}") from exc
            return self._model

    def warmup(self) -> None:
        if self.backend == "e5":
            self._load_e5()
            self.encode(["initialisation"], kind="query")
        else:
            self.encode(["initialisation"], kind="query")

    # -- encodage ----------------------------------------------------------
    def encode(self, texts: list[str], kind: str) -> list[list[float]]:
        if kind not in ("query", "passage"):
            raise ValueError("kind doit être 'query' ou 'passage'")
        cleaned = [self._strip_prefix(t) for t in texts]
        if self.backend == "e5":
            model = self._load_e5()
            prefixed = [f"{kind}: {t}" for t in cleaned]
            vectors = model.encode(
                prefixed,
                batch_size=self.settings.embedding_batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            return [[float(x) for x in vec] for vec in vectors]
        if self.backend == "fixture":
            return [self._fixture_vector(t) for t in cleaned]
        raise EmbeddingsUnavailable(f"backend d'embeddings inconnu: {self.backend}")

    @staticmethod
    def _strip_prefix(text: str) -> str:
        for prefix in ("query: ", "query:", "passage: ", "passage:"):
            if text.startswith(prefix):
                return text[len(prefix):]
        return text

    def _fixture_vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = [text[i : i + 3].lower() for i in range(max(0, len(text) - 2))]
        if not tokens:
            tokens = [text.lower() or "_"]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[index] += sign
        norm = sum(x * x for x in vec) ** 0.5
        if norm == 0:
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]


_service: EmbeddingService | None = None
_service_lock = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = EmbeddingService(get_settings())
    return _service


def reset_embedding_service() -> None:
    """Tests : force le rechargement avec une configuration différente."""
    global _service
    _service = None
