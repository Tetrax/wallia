"""Service de reclassement des passages (cross-encoder CPU).

Backend `transformers` (production, SEUL backend réel) :
`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, révision et poids épinglés
(SHA256 vérifié à chaque chargement), chargement strictement LOCAL
(`local_files_only=True`, `trust_remote_code=False`), CPU 2 threads, paires
(question, texte du passage) uniquement, 512 tokens maximum, lots de 8,
sortie = logits BRUTS (jamais une probabilité, aucune sigmoïde). Aucun réseau
pendant le chargement ou le scoring, aucun appel fournisseur, aucun GPU.

Backend `fixture` : mécanique de TEST déterministe et déclarée — jamais la
production, jamais une preuve métier, interdit si `WALLIA_ENV=production`
(refus au chargement de la configuration). Elle ne reproduit PAS l'ancienne
logique de production (il n'y a plus de barrière cosinus/lexicale
d'éligibilité) : elle ne fait qu'attribuer un score reproductible à chaque
passage, indépendamment de tout autre passage.

Garanties d'exécution : un singleton, UNE SEULE inférence à la fois (verrou
avec attente bornée — au-delà, `RerankerBusy`), chargement protégé par double
vérification. Toute indisponibilité lève `RerankerUnavailable` (sous-type
typé) : l'appelant ne remplace jamais un échec par un résultat factice.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings, get_settings

# ---------------------------------------------------------------------------
# Modèle réel : identité FIXE (décision docs/reranker-probe.md). Seul le
# CHEMIN local du modèle est configurable ; modèle/révision/empreinte ne
# peuvent pas être remplacés par un override divergent (refus au chargement
# de la configuration, aucun override silencieux).
# ---------------------------------------------------------------------------
MODEL_ID = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
MODEL_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
WEIGHTS_SHA256 = "5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4"

MODEL_FILES: dict[str, int] = {
    "README.md": 2278,
    "config.json": 891,
    "special_tokens_map.json": 239,
    "tokenizer.json": 17082660,
    "tokenizer_config.json": 435,
    "sentencepiece.bpe.model": 5069051,
    "model.safetensors": 470592698,
}
WEIGHTS_FILE = "model.safetensors"

# Seuil GELÉ (probe lot3c, docs/reranker-probe.md) : logit brut minimal pour
# servir un passage. Ce n'est ni une probabilité ni un réglage opérateur.
RERANK_LOGIT_THRESHOLD = 1.1491

MAX_LENGTH = 512
BATCH_SIZE = 8
TORCH_THREADS = 2
# Attente bornée d'une inférence : au-delà, l'appel est refusé (saturation).
LOCK_TIMEOUT_S = 30.0


class RerankerUnavailable(RuntimeError):
    """Le reclassement est indisponible (base : erreur technique typée)."""

    error_type = "reranker_unavailable"
    safe_message = "reclassement documentaire indisponible"


class RerankerIntegrityError(RerankerUnavailable):
    """Fichiers du modèle absents ou altérés (taille/empreinte non conformes)."""

    error_type = "reranker_integrity"
    safe_message = "modèle de reclassement absent ou altéré"


class RerankerBusy(RerankerUnavailable):
    """Saturation : une inférence est déjà en cours (attente bornée dépassée)."""

    error_type = "reranker_busy"
    safe_message = "reclassement documentaire saturé (une inférence est déjà en cours)"


class RerankerFailure(RerankerUnavailable):
    """Échec d'exécution du modèle (chargement ou inférence) — message sûr."""

    error_type = "reranker_failure"
    safe_message = "échec technique du reclassement documentaire"


# ---------------------------------------------------------------------------
# Backend fixture : mécanique de TEST déclarée, sans aucune sémantique.
# ---------------------------------------------------------------------------
_FIXTURE_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _fixture_tokens(text: str) -> set[str]:
    """Jetons du fixture : groupes alphanumériques d'au moins 3 caractères,
    minuscules. Les nombres seuls (« 10 ») et tokens courts ne comptent pas."""
    return {token.lower() for token in _FIXTURE_TOKEN_RE.findall(text or "") if len(token) >= 3}


def fixture_logit(question: str, text: str) -> float:
    """Score de TEST, sans sémantique : `-1.0 + 2.0 × jetons communs` (plafond
    3 jetons). 0 jeton commun → -1.0 ; 1 → +1.0 (sous le seuil gelé 1.1491) ;
    ≥ 2 → ≥ +3.0 (au-dessus). Ne dépend QUE de la paire (question, passage) :
    aucun score n'est entraîné par d'autres passages. Réservé aux tests ; le
    backend `transformers` est le seul classement de production."""
    hits = len(_fixture_tokens(question) & _fixture_tokens(text))
    return -1.0 + 2.0 * min(hits, 3)


def sha256_file(path: Path, chunk_bytes: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_bytes)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class RerankerService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backend = settings.reranker_backend
        self.model_dir = Path(settings.reranker_model_dir)
        # Identité FIXE (constantes du module) : un override divergent de
        # modèle/révision/empreinte ne peut jamais être utilisé silencieusement.
        self.model_id = MODEL_ID
        self.revision = MODEL_REVISION
        self.weights_sha256 = WEIGHTS_SHA256
        self.lock_timeout_s = LOCK_TIMEOUT_S
        self._model: Any = None
        self._tokenizer: Any = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        # État de disponibilité RÉEL (jamais un simple drapeau de configuration).
        self._state = "ready" if self.backend == "fixture" else "unloaded"
        self._error_type: str | None = None

    # -- informations publiques (jamais de secret) -------------------------
    @property
    def info(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model_id,
            "revision": self.revision,
            "weights_sha256": self.weights_sha256,
            "threshold": RERANK_LOGIT_THRESHOLD,
            "max_length": MAX_LENGTH,
            "batch_size": BATCH_SIZE,
            "threads": TORCH_THREADS,
            "device": "cpu",
            "state": self._state,
            "error_type": self._error_type,
        }

    # -- vérifications locales (aucun réseau) ------------------------------
    def verify_model_files(self) -> dict[str, Any]:
        """Vérifie présence, tailles ET empreinte des poids — puis retourne le
        détail. Lève `RerankerIntegrityError` au premier écart (aucun réseau)."""
        if not self.model_dir.is_dir():
            raise RerankerIntegrityError(f"modèle de reclassement absent ({self.model_dir})")
        sizes: dict[str, int] = {}
        problems: list[str] = []
        for name, expected in MODEL_FILES.items():
            path = self.model_dir / name
            if not path.is_file():
                problems.append(f"absent: {name}")
                continue
            got = path.stat().st_size
            sizes[name] = got
            if got != expected:
                problems.append(f"taille incorrecte: {name}")
        if problems:
            raise RerankerIntegrityError("modèle non conforme — " + "; ".join(problems))
        weights = sha256_file(self.model_dir / WEIGHTS_FILE)
        if self.weights_sha256 and weights != self.weights_sha256:
            raise RerankerIntegrityError("empreinte des poids différente de l'attendu")
        return {"files": sizes, "weights_sha256_verified": weights}

    def _load_model(self) -> None:
        """Chargement local protégé : double vérification, un seul chargement."""
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            try:
                self.verify_model_files()
                import torch
                from transformers import AutoModelForSequenceClassification, AutoTokenizer

                torch.set_num_threads(TORCH_THREADS)
                tokenizer = AutoTokenizer.from_pretrained(
                    str(self.model_dir), local_files_only=True, trust_remote_code=False
                )
                model = AutoModelForSequenceClassification.from_pretrained(
                    str(self.model_dir), local_files_only=True, trust_remote_code=False
                )
                model.eval()
            except RerankerUnavailable as exc:
                self._state, self._error_type = "unavailable", exc.error_type
                raise
            except Exception as exc:  # noqa: BLE001 - jamais de traceback renvoyé
                self._state, self._error_type = "unavailable", RerankerFailure.error_type
                raise RerankerFailure(RerankerFailure.safe_message) from exc
            self._tokenizer, self._model = tokenizer, model
            self._state, self._error_type = "ready", None

    # -- scoring -----------------------------------------------------------
    @contextmanager
    def _inference_slot(self) -> Iterator[None]:
        """UNE SEULE inférence à la fois, attente bornée : au-delà du délai,
        l'appel est refusé (`RerankerBusy`) — jamais une file non bornée."""
        acquired = self._inference_lock.acquire(timeout=self.lock_timeout_s)
        if not acquired:
            raise RerankerBusy(RerankerBusy.safe_message)
        try:
            yield
        finally:
            self._inference_lock.release()

    def score(self, question: str, texts: list[str]) -> list[float]:
        """Logits bruts de chaque paire (question, texte), dans l'ordre fourni.

        Une seule inférence à la fois (attente bornée). Backend fixture :
        scoring mécanique déclaré, sans modèle. Backend transformers : le lot
        est borné (BATCH_SIZE) et tronqué à MAX_LENGTH. Les sorties non finies
        (NaN/Inf) sont un ÉCHEC TECHNIQUE typé — jamais un score servi ; et
        l'état réel du service (ready/unavailable + type d'erreur) est
        actualisé à CHAQUE tentative, pour rester observable après un
        chargement paresseux ou une panne d'inférence.
        """
        if not texts:
            return []
        try:
            if self.backend == "fixture":
                with self._inference_slot():
                    values = [fixture_logit(question, text) for text in texts]
            elif self.backend != "transformers":
                raise RerankerUnavailable(f"backend de reclassement inconnu: {self.backend}")
            else:
                self._load_model()
                with self._inference_slot():
                    values = self._score_with_model(question, texts)
        except RerankerBusy:
            # Saturation bornée : visible dans l'état, jamais confondue avec
            # une indisponibilité du modèle (le service reste utilisable).
            self._error_type = RerankerBusy.error_type
            raise
        except RerankerUnavailable as exc:
            self._state, self._error_type = "unavailable", exc.error_type
            raise
        if len(values) != len(texts):
            self._state, self._error_type = "unavailable", RerankerFailure.error_type
            raise RerankerFailure(RerankerFailure.safe_message)
        floats = [float(value) for value in values]
        if any(not math.isfinite(value) for value in floats):
            # Jamais un « no_relevant_source » fabriqué par une sortie non finie.
            self._state, self._error_type = "unavailable", RerankerFailure.error_type
            raise RerankerFailure(RerankerFailure.safe_message)
        self._state, self._error_type = "ready", None
        return floats

    def _score_with_model(self, question: str, texts: list[str]) -> list[float]:
        import torch

        tokenizer = self._tokenizer
        model = self._model
        scores: list[float] = []
        try:
            for start in range(0, len(texts), BATCH_SIZE):
                batch = texts[start : start + BATCH_SIZE]
                encoded = tokenizer(
                    [question] * len(batch),
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=MAX_LENGTH,
                    return_tensors="pt",
                )
                with torch.inference_mode():
                    logits = model(**encoded).logits
                scores.extend(float(value) for value in logits.reshape(-1).tolist())
        except Exception as exc:  # noqa: BLE001 - message sûr, jamais le détail brut
            raise RerankerFailure(RerankerFailure.safe_message) from exc
        return scores

    def warmup(self) -> None:
        if self.backend == "transformers":
            self._load_model()
        self.score("initialisation", ["initialisation"])


_service: RerankerService | None = None
_service_lock = threading.Lock()


def get_reranker_service() -> RerankerService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = RerankerService(get_settings())
    return _service


def reset_reranker_service() -> None:
    """Tests : force la reconstruction avec une configuration différente."""
    global _service
    _service = None
