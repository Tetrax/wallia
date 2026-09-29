#!/usr/bin/env python3
"""Outil hôte (lot3b) : analyse de calibration et choix DÉTERMINISTE des seuils.

Entrées : fixtures de calibration (textes étiquetés) + vecteurs E5 réels
(runtime/tests-isolated/calibration-vectors.json, obtenus via l'endpoint
interne de l'API vivante). Aucun accès DB, aucune écriture hors espace de tests.

Critères de sélection documentés (figés AVANT l'acceptance) :
  Barrière vectorielle :
    neg_max = maximum, sur les négatifs GATED (hors_sujet + piège métadonnées),
              du meilleur cosinus contre N'IMPORTE quel passage du corpus ;
    pos_min = minimum, sur les positifs, du cosinus contre le passage attendu ;
    si pos_min > neg_max : seuil = arrondi au centième SUPÉRIEUR du point milieu
        (maximise la marge minimale des deux côtés) ;
    sinon : non séparé -> seuil = arrondi au centième supérieur de neg_max + 0,01
        (priorité au rejet des négatifs) ; l'inversion est consignée honnêtement.
  Politique PAR PASSAGE (lot3b) :
    - éligibilité lexicale : ≥2 correspondances significatives exactes DANS le
      passage, ou 1 seule corroborée par un cosinus ≥ seuil de corroboration ;
    - question réduite à un unique lexème significatif : 1 correspondance dans
      le passage suffit (couverture 1/1) ;
    - seuil de corroboration = arrondi au centième supérieur du cosinus maximal
      des négatifs sur un passage à UNE correspondance, + 0,01 (rejeter d'abord
      TOUS les négatifs mesurés — gated critiques et proches maintenus rejetés —
      puis maximiser les positifs servis).

Les correspondances par passage sont calculées sur les textes de la fixture avec
les MÊMES règles d'exclusion que `backend/app/retrieval.py` (mots vides extraits
de la source, jetons produit/version) ; la vérification SQL réelle de
l'implémentation se fait au runner isolé (`test_retrieval_calibration.py`).

Sortie : runtime/evidence/lot3b-calibration.json (scores réels, étiquettes,
statistiques, choix, limites) — lisible par le runner isolé.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
from datetime import datetime, timezone

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_RETRIEVAL_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "backend" / "app" / "retrieval.py"


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def ceil2(value: float) -> float:
    return math.ceil(value * 100 - 1e-9) / 100


def _stopwords() -> frozenset[str]:
    """Mots vides lus depuis `retrieval.py` (source unique, jamais dupliquée)."""
    source = _RETRIEVAL_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'_STOPWORDS = frozenset\(\s*"""(.*?)"""', source, re.S)
    if not match:
        raise RuntimeError("_STOPWORDS introuvables dans backend/app/retrieval.py")
    return frozenset(match.group(1).split())


def _significant(question: str, stopwords: frozenset[str], product_words: set[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in (t.lower() for t in _TOKEN_RE.findall(question or "")):
        if len(token) < 2 or token in stopwords or token in product_words:
            continue
        if _VERSION_RE.match(token) or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _hits(lexemes: list[str], text_value: str) -> int:
    tokens = {t.lower() for t in _TOKEN_RE.findall(text_value or "")}
    return sum(1 for lexeme in lexemes if lexeme in tokens)


def _eligible(hits: int, cosine: float, lexeme_count: int, min_cosine: float, min_matches: int, corroboration: float) -> str | None:
    if cosine >= min_cosine:
        return "vector"
    if lexeme_count == 1:
        return "lexical_single_lexeme_question" if hits >= 1 else None
    if lexeme_count >= 2:
        if hits >= min_matches:
            return "lexical"
        if hits == 1 and cosine >= corroboration:
            return "lexical_corroborated"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibration", type=pathlib.Path, required=True)
    parser.add_argument("--vectors", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()

    calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
    vectors_doc = json.loads(args.vectors.read_text(encoding="utf-8"))
    vectors = {item["id"]: item["vector"] for item in vectors_doc["items"]}

    corpus_ids = [passage["id"] for passage in calibration["corpus"]]
    positives = []
    for question in calibration["positives"]:
        scores = {pid: round(dot(vectors[question["id"]], vectors[pid]), 4) for pid in corpus_ids}
        expected = [pid for pid in question["expected"]]
        best_expected = max(scores[pid] for pid in expected)
        best_any = max(scores.values())
        best_any_id = max(scores, key=lambda k: scores[k])
        positives.append(
            {
                "id": question["id"],
                "question": question["question"],
                "lang": question["lang"],
                "target_lang": question["target_lang"],
                "expected": expected,
                "cos_expected": best_expected,
                "cos_best_any": best_any,
                "best_any_id": best_any_id,
                "scores": scores,
            }
        )
    negatives = []
    for question in calibration["negatives"]:
        scores = {pid: round(dot(vectors[question["id"]], vectors[pid]), 4) for pid in corpus_ids}
        best_any = max(scores.values())
        best_any_id = max(scores, key=lambda k: scores[k])
        negatives.append(
            {
                "id": question["id"],
                "question": question["question"],
                "category": question["category"],
                "gated": bool(question["gated"]),
                "cos_best_any": best_any,
                "best_any_id": best_any_id,
                "scores": scores,
            }
        )

    gated_negs = [n for n in negatives if n["gated"]]
    neg_max = max(n["cos_best_any"] for n in gated_negs)
    pos_min = min(p["cos_expected"] for p in positives)
    separated = pos_min > neg_max
    if separated:
        threshold = ceil2((neg_max + pos_min) / 2)
        rule = "midpoint(pos_min, neg_max) arrondi au centième supérieur"
    else:
        threshold = ceil2(neg_max) + 0.01
        rule = "non séparé : ceil2(neg_max) + 0,01 (priorité au rejet des négatifs)"

    # --- politique PAR PASSAGE (lot3b) : seuil de corroboration mono-lexème ---
    # Les correspondances sont comptées sur les textes de la fixture avec les
    # MÊMES règles d'exclusion que app/retrieval.py (mots vides lus depuis la
    # source, jetons produit/version) ; l'exécution SQL réelle est vérifiée par
    # test_retrieval_calibration.py au runner isolé.
    stopwords = _stopwords()
    product_words = {t.lower() for t in _TOKEN_RE.findall("Nova Boreal")}
    texts = {passage["id"]: passage["text"] for passage in calibration["corpus"]}
    min_matches = 2

    neg_single_max: float | None = None
    neg_single_detail: list[dict] = []
    for question in calibration["negatives"]:
        lexemes = _significant(question["question"], stopwords, product_words)
        scores = {pid: dot(vectors[question["id"]], vectors[pid]) for pid in texts}
        singles = [(pid, round(scores[pid], 4)) for pid, text in texts.items() if _hits(lexemes, text) == 1]
        if singles:
            best_pid, best_cos = max(singles, key=lambda item: item[1])
            if neg_single_max is None or best_cos > neg_single_max:
                neg_single_max = best_cos
            neg_single_detail.append({"id": question["id"], "passage": best_pid, "cos": best_cos})
    if neg_single_max is not None:
        corroboration = ceil2(neg_single_max) + 0.01
        rule_corroboration = "ceil2(cos maximal des négatifs sur un passage à UNE correspondance) + 0,01"
    else:
        corroboration = 0.85
        rule_corroboration = "défaut 0,85 (aucun négatif mesuré à correspondance unique)"

    sim_positives_served: list[str] = []
    sim_positives_missing: list[str] = []
    for question in calibration["positives"]:
        lexemes = _significant(question["question"], stopwords, product_words)
        scores = {pid: dot(vectors[question["id"]], vectors[pid]) for pid in texts}
        reasons = {
            pid: _eligible(_hits(lexemes, texts[pid]), scores[pid], len(lexemes), threshold, min_matches, corroboration)
            for pid in texts
        }
        eligible_any = any(reason for reason in reasons.values())
        expected_served = any(reasons[pid] for pid in question["expected"])
        (sim_positives_served if (eligible_any and expected_served) else sim_positives_missing).append(question["id"])

    sim_negatives_rejected: list[str] = []
    sim_negatives_admitted: list[str] = []
    for question in calibration["negatives"]:
        lexemes = _significant(question["question"], stopwords, product_words)
        scores = {pid: dot(vectors[question["id"]], vectors[pid]) for pid in texts}
        admitted = any(
            _eligible(_hits(lexemes, texts[pid]), scores[pid], len(lexemes), threshold, min_matches, corroboration)
            for pid in texts
        )
        (sim_negatives_admitted if admitted else sim_negatives_rejected).append(question["id"])

    evidence = {
        "kind": "calibration",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calibration_name": calibration["name"],
        "vectors_texts_sha256": vectors_doc["texts_sha256"],
        "embedding": vectors_doc["info"],
        "counts": {"positives": len(positives), "negatives": len(negatives), "gated_negatives": len(gated_negs)},
        "positives": positives,
        "negatives": negatives,
        "separation": {
            "pos_min_cos_expected": round(pos_min, 4),
            "neg_max_cos_best_any_gated": round(neg_max, 4),
            "margin": round(pos_min - neg_max, 4),
            "separated": separated,
        },
        "selection": {
            "objective": "rejeter d'abord TOUS les négatifs mesurés (gated critiques + proches maintenus rejetés), puis maximiser les positifs servis (lexicographie : rejet > rappel)",
            "rule_threshold": rule,
            "threshold_min_cosine": round(threshold, 4),
            "rule_lexical": "PAR PASSAGE : ≥2 correspondances significatives exactes DANS le passage, ou 1 seule corroborée par un cosinus ≥ seuil de corroboration (porte translangue) ; 1 correspondance suffit quand la question se réduit à un unique lexème significatif",
            "lexical_min_matches": min_matches,
            "rule_corroboration": rule_corroboration,
            "corroboration_cosine": round(corroboration, 4),
            "negatives_single_hit_max": round(neg_single_max, 4) if neg_single_max is not None else None,
            "negatives_single_hit_detail": neg_single_detail,
            "note": "Paramétrage figé AVANT l'acceptance ; aucun texte du jeu d'acceptance n'entre dans cette sélection. Limites statistiques honnêtes : voir separation.separated=false — pas de garantie générale.",
        },
        "policy_simulation": {
            "kind": "simulation déterministe (correspondances sur textes de la fixture, mêmes règles d'exclusion ; l'exécution SQL réelle est vérifiée par test_retrieval_calibration.py au runner isolé)",
            "min_cosine": round(threshold, 4),
            "lexical_min_matches": min_matches,
            "corroboration_cosine": round(corroboration, 4),
            "positives_served_count": len(sim_positives_served),
            "positives_missing": sorted(sim_positives_missing),
            "negatives_rejected_count": len(sim_negatives_rejected),
            "negatives_total": len(calibration["negatives"]),
            "negatives_admitted": sorted(sim_negatives_admitted),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[calibration] positifs={len(positives)} négatifs={len(negatives)} (gated={len(gated_negs)})")
    print(f"[calibration] pos_min={pos_min:.4f} neg_max={neg_max:.4f} marge={pos_min - neg_max:+.4f} séparé={separated}")
    print(f"[calibration] seuil retenu = {threshold:.2f} (règle : {rule})")
    neg_single_txt = f"{neg_single_max:.4f}" if neg_single_max is not None else "aucun"
    print(f"[calibration] corroboration mono-lexème = {corroboration:.2f} (max négatif à 1 correspondance : {neg_single_txt})")
    print(f"[calibration] simulation par passage : positifs servis={len(sim_positives_served)}/{len(positives)} manquants={sorted(sim_positives_missing)}")
    print(f"[calibration] simulation par passage : négatifs rejetés={len(sim_negatives_rejected)}/{len(negatives)} admis={sorted(sim_negatives_admitted)}")
    for p in positives:
        print(f"  pos {p['id']:>6} cos_expected={p['cos_expected']:.4f} best_any={p['cos_best_any']:.4f} ({p['best_any_id']})")
    for n in negatives:
        gate = "gated" if n["gated"] else "proche"
        print(f"  neg {n['id']:>6} [{gate:6}] best_any={n['cos_best_any']:.4f} ({n['best_any_id']})")
    print(f"[calibration] evidence -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
