"""Reclassement : backend fixture déclaré, refus en production, intégrité du
modèle, saturation bornée, états explicites.

L'indisponibilité réelle (`retrieval_unavailable`) doit rester DISTINCTE de
`no_relevant_source` : jamais un échec technique déguisé en absence de source,
jamais un repli sur une ancienne barrière.
"""
from __future__ import annotations

import dataclasses

import pytest

from app.config import ConfigError, load_settings
from app.retrieval import hybrid_search
from app.reranking import (
    MODEL_FILES,
    RERANK_LOGIT_THRESHOLD,
    WEIGHTS_FILE,
    RerankerIntegrityError,
    RerankerService,
    fixture_logit,
)
from tests.test_retrieval import _chunk, _document, _one_hot


def _document_and_chunk(db, text_value="saturation du journal local", vector_index=5):
    document = _document(db, title="Guide", product="Aster", versions=["10.10"])
    _chunk(db, document, seq=1, text_value=text_value, vector_index=vector_index)
    db.commit()
    return document


# ---------------------------------------------------------------------------
# Backend fixture : mécanique déclarée, déterministe, sans sémantique.
# ---------------------------------------------------------------------------
def test_fixture_backend_is_declared_and_deterministic():
    assert fixture_logit("alpha beta gamma", "alpha beta gamma") == 5.0
    assert fixture_logit("alpha beta", "alpha beta delta") == 3.0  # 2 jetons communs
    assert fixture_logit("alpha beta", "alpha epsilon") == 1.0  # 1 jeton commun
    assert fixture_logit("alpha beta", "rien du tout") == -1.0  # aucun jeton commun
    # Les nombres seuls et jetons courts ne comptent pas.
    assert fixture_logit("alpha 10.10", "10 10.10 couloir") == -1.0
    # Déterministe : même paire → même score.
    assert fixture_logit("journal local", "journal local") == fixture_logit("journal local", "journal local")
    # Indépendant de tout autre passage.
    assert fixture_logit("journal local", "journal local") == 3.0


def test_production_refuses_fixture_reranker(monkeypatch):
    monkeypatch.setenv("WALLIA_ENV", "production")
    # On isole la garde RERANKER : l'embedding reste sur son backend réel.
    monkeypatch.setenv("WALLIA_EMBEDDING_BACKEND", "e5")
    monkeypatch.setenv("WALLIA_RERANKER_BACKEND", "fixture")
    with pytest.raises(ConfigError):
        load_settings()
    # Le backend réel reste accepté ; modèle/révision/SHA fixes vérifiés.
    monkeypatch.setenv("WALLIA_RERANKER_BACKEND", "transformers")
    settings = load_settings()
    assert settings.reranker_backend == "transformers"
    assert settings.reranker_model == "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    assert settings.reranker_revision == "1427fd652930e4ba29e8149678df786c240d8825"
    assert settings.reranker_weights_sha256 == "5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4"
    assert settings.reranker_model_dir.name == "reranker"


# ---------------------------------------------------------------------------
# Intégrité : modèle absent ou corrompu → erreur typée, jamais no_relevant_source.
# ---------------------------------------------------------------------------
def test_missing_model_is_typed_and_distinct_from_no_source(settings, migrated, tmp_path):
    from app.db import session_scope

    broken = RerankerService(
        dataclasses.replace(
            settings, reranker_backend="transformers", reranker_model_dir=tmp_path / "absent"
        )
    )
    with session_scope() as db:
        _document_and_chunk(db)
        result = hybrid_search(
            db, settings, query="saturation journal", query_vector=_one_hot(5), reranker=broken
        )
        assert result["status"] == "retrieval_unavailable"
        assert result["status"] != "no_relevant_source"
        assert result["sources"] == []
        assert result["diagnostics"]["error"]["type"] == "reranker_integrity"
        assert result["diagnostics"]["error"]["message"]
        # Message sûr : jamais le chemin complet ni un traceback.
        assert str(tmp_path) not in result["diagnostics"]["error"]["message"]
        assert result["diagnostics"]["reranker"]["state"] == "unavailable"
        assert result["diagnostics"]["reranker"]["error_type"] == "reranker_integrity"
        # Les candidats restent tracés (signaux de sélection bruts).
        assert result["diagnostics"]["candidates"]

    assert broken.info["state"] == "unavailable"


def test_corrupt_model_files_are_detected(settings, migrated, tmp_path):
    from app.db import session_scope

    # Fichiers présents mais tailles incorrectes.
    wrong = tmp_path / "wrong-sizes"
    wrong.mkdir()
    for name in MODEL_FILES:
        (wrong / name).write_bytes(b"x")
    service = RerankerService(
        dataclasses.replace(settings, reranker_backend="transformers", reranker_model_dir=wrong)
    )
    with pytest.raises(RerankerIntegrityError):
        service.verify_model_files()
    with session_scope() as db:
        _document_and_chunk(db)
        result = hybrid_search(db, settings, query="saturation journal", query_vector=_one_hot(5), reranker=service)
    assert result["status"] == "retrieval_unavailable"
    assert result["diagnostics"]["error"]["type"] == "reranker_integrity"

    # Tailles exactes mais empreinte des poids différente (fichiers creux) :
    # la vérification SHA le détecte SANS charger le modèle (aucun réseau).
    fake = tmp_path / "fake-weights"
    fake.mkdir()
    for name, size in MODEL_FILES.items():
        with (fake / name).open("wb") as handle:
            handle.truncate(size)
    service2 = RerankerService(
        dataclasses.replace(settings, reranker_backend="transformers", reranker_model_dir=fake)
    )
    with pytest.raises(RerankerIntegrityError) as excinfo:
        service2.verify_model_files()
    assert "empreinte" in str(excinfo.value)
    assert WEIGHTS_FILE in str(excinfo.value) or "poids" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Saturation : une seule inférence à la fois, attente bornée → refus typé.
# ---------------------------------------------------------------------------
def test_saturation_is_bounded_and_typed(settings, migrated):
    from app.db import session_scope

    service = RerankerService(settings)
    assert service.backend == "fixture"
    with session_scope() as db:
        _document_and_chunk(db)
        service.lock_timeout_s = 0.05
        service._inference_lock.acquire()
        try:
            blocked = hybrid_search(
                db, settings, query="saturation journal", query_vector=_one_hot(5), reranker=service
            )
        finally:
            service._inference_lock.release()
        assert blocked["status"] == "retrieval_unavailable"
        assert blocked["sources"] == []
        assert blocked["diagnostics"]["error"]["type"] == "reranker_busy"
        assert "saturé" in blocked["diagnostics"]["error"]["message"]

        # Le verrou libéré, la même recherche repasse réellement.
        again = hybrid_search(
            db, settings, query="saturation journal", query_vector=_one_hot(5), reranker=service
        )
        assert again["status"] == "ok"


def test_empty_pool_never_consults_the_model(settings, migrated, tmp_path):
    """Corpus sans candidat : `no_relevant_source` — le modèle n'est pas
    sollicité et une panne de modèle ne fabrique pas un état mensonger."""
    from app.db import session_scope

    broken = RerankerService(
        dataclasses.replace(
            settings, reranker_backend="transformers", reranker_model_dir=tmp_path / "absent"
        )
    )
    with session_scope() as db:
        result = hybrid_search(db, settings, query="saturation journal", query_vector=_one_hot(5), reranker=broken)
        assert result["status"] == "no_relevant_source"
        assert result["sources"] == []
        assert "error" not in result["diagnostics"]


def test_diagnostics_expose_identity_and_raw_logit(settings, migrated):
    from app.db import session_scope

    with session_scope() as db:
        _document_and_chunk(db)
        result = hybrid_search(db, settings, query="saturation journal", query_vector=_one_hot(5))
    assert result["status"] == "ok"
    diagnostics = result["diagnostics"]
    reranker = diagnostics["reranker"]
    assert reranker["backend"] == "fixture"
    assert reranker["model"] == settings.reranker_model
    assert reranker["revision"] == settings.reranker_revision
    assert reranker["threshold"] == RERANK_LOGIT_THRESHOLD == 1.1491
    assert reranker["state"] == "ready"
    source = result["sources"][0]
    assert source["score_kind"] == "rerank_logit"
    assert source["score"] == 3.0  # logit brut du fixture, jamais une probabilité
    candidate = diagnostics["candidates"][0]
    assert candidate["score_rerank"] == 3.0 and candidate["served"] is True
    assert candidate["score_rrf"] > 0


def test_unknown_backend_is_refused_at_score_time(settings, migrated):
    from app.db import session_scope

    weird = RerankerService(dataclasses.replace(settings, reranker_backend="magique"))
    with session_scope() as db:
        _document_and_chunk(db)
        result = hybrid_search(db, settings, query="saturation journal", query_vector=_one_hot(5), reranker=weird)
    assert result["status"] == "retrieval_unavailable"
    assert result["diagnostics"]["error"]["type"] == "reranker_unavailable"


# ---------------------------------------------------------------------------
# Compléments revue3d : identité fixe, diagnostics post-scoring, sorties
# non finies = panne typée (jamais un no-source fabriqué).
# ---------------------------------------------------------------------------
def test_divergent_reranker_identity_is_refused_at_load(monkeypatch):
    monkeypatch.delenv("WALLIA_RERANKER_MODEL", raising=False)
    monkeypatch.delenv("WALLIA_RERANKER_REVISION", raising=False)
    monkeypatch.delenv("WALLIA_RERANKER_WEIGHTS_SHA256", raising=False)
    # Sans override : identité fixe, chemin local seul configurable.
    settings = load_settings()
    assert settings.reranker_model == "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    assert settings.reranker_revision == "1427fd652930e4ba29e8149678df786c240d8825"

    monkeypatch.setenv("WALLIA_RERANKER_MODEL", "autre-modele/inconnu")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.delenv("WALLIA_RERANKER_MODEL")
    monkeypatch.setenv("WALLIA_RERANKER_REVISION", "0" * 40)
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.delenv("WALLIA_RERANKER_REVISION")
    monkeypatch.setenv("WALLIA_RERANKER_WEIGHTS_SHA256", "f" * 64)
    with pytest.raises(ConfigError):
        load_settings()
    # Après retrait des overrides divergents, le chargement repasse.
    monkeypatch.delenv("WALLIA_RERANKER_WEIGHTS_SHA256")
    assert load_settings().reranker_model == "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


def test_non_finite_logits_are_a_typed_failure_not_no_source(settings, migrated):
    """Des logits NaN/Inf sont un ÉCHEC TECHNIQUE typé : statut
    `retrieval_unavailable` (jamais `no_relevant_source`), état observable."""
    from app.db import session_scope

    service = RerankerService(dataclasses.replace(settings, reranker_backend="transformers"))
    service._load_model = lambda: None  # chargement neutralisé : seule la sortie compte
    service._score_with_model = lambda question, texts: [float("nan")] * len(texts)
    with session_scope() as db:
        _document_and_chunk(db)
        result = hybrid_search(db, settings, query="saturation journal", query_vector=_one_hot(5), reranker=service)
    assert result["status"] == "retrieval_unavailable"
    assert result["status"] != "no_relevant_source"
    assert result["diagnostics"]["error"]["type"] == "reranker_failure"
    assert result["diagnostics"]["reranker"]["state"] == "unavailable"
    assert result["diagnostics"]["reranker"]["error_type"] == "reranker_failure"
    assert service.info["state"] == "unavailable"


def test_lazy_load_state_is_refreshed_after_scoring(settings, migrated):
    """Le diagnostic reflète l'état APRÈS le scoring : un service encore
    `unloaded` qui vient de charger avec succès apparaît `ready`."""
    from app.db import session_scope

    service = RerankerService(dataclasses.replace(settings, reranker_backend="transformers"))
    assert service.info["state"] == "unloaded"
    service._load_model = lambda: None
    service._score_with_model = lambda question, texts: [3.5] * len(texts)
    with session_scope() as db:
        _document_and_chunk(db)
        result = hybrid_search(db, settings, query="saturation journal", query_vector=_one_hot(5), reranker=service)
    assert result["status"] == "ok"
    assert result["diagnostics"]["reranker"]["state"] == "ready"
    assert result["diagnostics"]["reranker"]["error_type"] is None
    assert result["diagnostics"]["reranker"]["model"] == "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
