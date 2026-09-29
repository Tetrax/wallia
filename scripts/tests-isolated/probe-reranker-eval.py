#!/usr/bin/env python3
"""Wallia — LOT3C : probe EXPÉRIMENTAL de reranking sémantique CPU (cross-encoder).

S'exécute UNIQUEMENT dans le conteneur borné du script `probe-reranker.sh`
(inférence offline, `--network none`, uid non-root 1002, 2 CPU, 1600 Mio).

Séquence STRICTE :
  0. refus si l'environnement n'est pas celui du probe (socket Docker, /secrets, réseau) ;
  1. vérification d'intégrité des entrées (copies de texte, SHA enregistrés) ;
  2. chargement FROID du cross-encoder (local_files_only=True, pas de trust_remote_code),
     vérification des tailles de fichiers et du SHA256 des poids ;
  3. CALIBRATION sur les 14 positifs / 14 négatifs / 13 passages existants
     (textes intacts), tous les scores question×passage (logit BRUT, jamais une
     probabilité), barrière choisie par une règle FIGÉE, puis gel écrit AVANT
     l'acceptance ;
  4. ACCEPTANCE sur le corpus réel exporté (4 documents / 16 passages), question
     française exacte, négatif commercial, filtres produit/version de recette ;
  5. mesures de performance (chargement froid, paires/batch, top-30, mémoire,
     disque) et statut rc/OOM.

Aucun test d'acceptance en boucle : la barrière est gelée avant l'étape 4 et
n'est jamais ajustée. Aucune intégration applicative, aucun fichier applicatif
touché.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MODEL_ID = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
WEIGHTS_SHA256 = "5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4"
MODEL_FILES = {
    "README.md": 2278,
    "config.json": 891,
    "special_tokens_map.json": 239,
    "tokenizer.json": 17082660,
    "tokenizer_config.json": 435,
    "sentencepiece.bpe.model": 5069051,
    "model.safetensors": 470592698,
}
MAX_LENGTH = 512
BARRIER_ROUND = 4

QUESTION_POSITIVE_FR = "Combien de jours de journaux la rotation conserve-t-elle dans Aster 10.10 ?"
QUESTION_NEGATIVE_FR = "Quel est le tarif de la licence annuelle en euros et les conditions commerciales de revente ?"

EXCLUSION_NOTE = (
    "barrière = score minimal (logit brut) pour servir un passage ; règle figée : "
    "milieu (min_pos+max_neg)/2 si min(score des bonnes réponses) > max(score de TOUS "
    "les négatifs, proches compris) ; sinon meilleur couple (rappel + rejet) sur la "
    "grille des demi-sommes des scores agrégés observés, départage : rappel max, puis "
    "rejet max, puis seuil le plus haut. Aucun texte d'acceptance n'intervient."
)

_log_lines: list[str] = []


def log(message: str) -> None:
    line = f"[probe-eval] {message}"
    print(line, flush=True)
    _log_lines.append(line)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_bytes: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_bytes)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"écrit : {path} ({path.stat().st_size} octets)")


def read_cgroup_value(path: str) -> int | None:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def vm_hwm_kb() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Phase 0 — garde-fous d'environnement
# ---------------------------------------------------------------------------
def phase0_environment() -> dict:
    if os.environ.get("WALLIA_RERANKER_PROBE_CONTAINER") != "1":
        raise SystemExit("refus : ce probe doit s'exécuter dans le conteneur borné (WALLIA_RERANKER_PROBE_CONTAINER=1).")
    if Path("/var/run/docker.sock").exists():
        raise SystemExit("refus : socket Docker détecté dans le conteneur.")
    secrets_dir = Path("/secrets")
    secret_entries = sorted(p.name for p in secrets_dir.iterdir()) if secrets_dir.exists() else []
    if secret_entries:
        raise SystemExit(f"refus : contenu de secrets détecté dans le conteneur : {secret_entries}")
    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("TRANSFORMERS_OFFLINE") != "1":
        raise SystemExit("refus : mode hors-ligne HF/transformers non imposé par l'environnement.")
    interfaces = sorted(os.listdir("/sys/class/net"))
    if interfaces != ["lo"]:
        raise SystemExit(f"refus : interfaces réseau non attendues dans le conteneur : {interfaces}")
    return {
        "interfaces": interfaces,
        "docker_socket": False,
        "secrets_dir_present_in_image": secrets_dir.exists(),
        "secrets_entries": secret_entries,
        "offline_env": True,
    }


# ---------------------------------------------------------------------------
# Phase 1 — intégrité des entrées
# ---------------------------------------------------------------------------
def phase1_inputs(args) -> dict:
    calibration = json.loads(Path(args.calibration).read_text(encoding="utf-8"))
    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    queries_doc = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    queries = {item["id"]: item for item in queries_doc["items"]}

    if queries["q-positif-fr-en"]["text"] != QUESTION_POSITIVE_FR:
        raise SystemExit("refus : la question positive du jeu d'acceptance a changé — textes non modifiés exigés.")
    if queries["q-negatif-commercial"]["text"] != QUESTION_NEGATIVE_FR:
        raise SystemExit("refus : le négatif commercial du jeu d'acceptance a changé — textes non modifiés exigés.")

    passages = calibration["corpus"]
    positives = calibration["positives"]
    negatives = calibration["negatives"]
    expected_ids = {pid for pos in positives for pid in pos["expected"]}
    known = {p["id"] for p in passages}
    missing = sorted(expected_ids - known)
    if missing:
        raise SystemExit(f"refus : passages attendus absents de la calibration : {missing}")

    seven = [c for c in corpus["chunks"] if "seven days" in (c["text"] or "").lower()]
    if len(seven) != 1:
        raise SystemExit(f"refus : « seven days » doit matcher exactement 1 passage du corpus (trouvé {len(seven)}) — aucune réparation.")

    sha_cal = sha256_file(Path(args.calibration))
    return {
        "calibration": calibration,
        "corpus": corpus,
        "queries": queries,
        "passages": passages,
        "positives": positives,
        "negatives": negatives,
        "seven_chunk": seven[0],
        "sha256": {
            "calibration_staged": sha_cal,
            "live_corpus_chunks_sha256_declared": corpus.get("chunks_sha256"),
            "queries_texts_sha256_declared": queries_doc.get("texts_sha256"),
        },
    }


# ---------------------------------------------------------------------------
# Phase 2 — chargement froid + vérification modèle
# ---------------------------------------------------------------------------
def phase2_load_model(args) -> dict:
    model_dir = Path(args.model)
    problems = []
    sizes = {}
    for name, want in MODEL_FILES.items():
        path = model_dir / name
        if not path.exists():
            problems.append(f"absent: {name}")
            continue
        got = path.stat().st_size
        sizes[name] = got
        if got != want:
            problems.append(f"taille {name}: {got} != {want}")
    if problems:
        raise SystemExit("refus : fichiers modèle non conformes : " + "; ".join(problems))

    weights_got = sha256_file(model_dir / "model.safetensors")
    if weights_got != WEIGHTS_SHA256:
        raise SystemExit(f"refus : SHA256 des poids différent : {weights_got}")

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(2)
    t0 = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False)
    t1 = time.perf_counter()
    model = AutoModelForSequenceClassification.from_pretrained(
        str(model_dir), local_files_only=True, trust_remote_code=False
    )
    t2 = time.perf_counter()
    model.eval()
    config = model.config
    info = {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "weights_sha256_verified": weights_got,
        "file_sizes_verified": sizes,
        "cold_load": {"tokenizer_s": round(t1 - t0, 3), "model_s": round(t2 - t1, 3), "total_s": round(t2 - t0, 3)},
        "libs": {
            "torch": torch.__version__,
            "transformers": __import__("transformers").__version__,
            "python": sys.version.split()[0],
        },
        "threads": torch.get_num_threads(),
        "num_labels": int(config.num_labels) if hasattr(config, "num_labels") else None,
        "architectures": list(getattr(config, "architectures", []) or []),
        "max_length": MAX_LENGTH,
        "activation_identity_declared": getattr(config, "sbert_ce_default_activation_function", None),
        "output": "logit brut (sortie linéaire du classifieur) ; aucune probabilité, aucune sigmoïde appliquée",
        "page_cache_caveat": "fichiers fraîchement téléchargés sur l'hôte : le cache OS n'est pas contrôlé — borne basse du chargement disque.",
    }
    log(f"modèle chargé en {info['cold_load']['total_s']}s (torch {info['libs']['torch']}, transformers {info['libs']['transformers']})")
    return {"model": model, "tokenizer": tokenizer, "info": info}


# ---------------------------------------------------------------------------
# Scoring (batch borné, logits bruts)
# ---------------------------------------------------------------------------
def score_pairs(context: dict, pairs: list[tuple[str, str]], batch_size: int) -> tuple[list[float], list[float]]:
    import torch

    tokenizer = context["tokenizer"]
    model = context["model"]
    scores: list[float] = []
    batch_times: list[float] = []
    for start in range(0, len(pairs), batch_size):
        chunk = pairs[start : start + batch_size]
        questions = [q for q, _ in chunk]
        contents = [c for _, c in chunk]
        t0 = time.perf_counter()
        encoded = tokenizer(
            questions, contents, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt"
        )
        with torch.inference_mode():
            logits = model(**encoded).logits
        flat = logits.reshape(-1).tolist()
        dt = time.perf_counter() - t0
        batch_times.append(dt)
        scores.extend(float(value) for value in flat)
    return scores, batch_times


def r4(value: float) -> float:
    return round(float(value), BARRIER_ROUND)


def r6(value: float) -> float:
    return round(float(value), 6)


# ---------------------------------------------------------------------------
# Phase 3 — calibration + gel
# ---------------------------------------------------------------------------
def phase3_calibration(context: dict, phase1: dict, batch_size: int, out_dir: Path, model_info: dict) -> dict:
    passages = phase1["passages"]
    positives = phase1["positives"]
    negatives = phase1["negatives"]
    passage_ids = [p["id"] for p in passages]
    passage_text = {p["id"]: p["text"] for p in passages}
    questions = [(pos["id"], pos["question"]) for pos in positives] + [(neg["id"], neg["question"]) for neg in negatives]

    pairs = [(text, passage_text[pid]) for _, text in questions for pid in passage_ids]
    t0 = time.perf_counter()
    scores, batch_times = score_pairs(context, pairs, batch_size)
    elapsed = time.perf_counter() - t0
    matrix: dict[str, dict[str, float]] = {}
    cursor = 0
    for qid, _ in questions:
        matrix[qid] = {pid: r6(scores[cursor + i]) for i, pid in enumerate(passage_ids)}
        cursor += len(passage_ids)

    pos_detail = {}
    for pos in positives:
        expected = {pid: matrix[pos["id"]][pid] for pid in pos["expected"]}
        best_pid = max(expected, key=lambda pid: (expected[pid], pid))
        pos_detail[pos["id"]] = {
            "question": pos["question"],
            "expected": expected,
            "best_expected_passage": best_pid,
            "best_score": expected[best_pid],
            "target_lang": pos.get("target_lang"),
        }
    neg_detail = {}
    for neg in negatives:
        row = matrix[neg["id"]]
        ranked = sorted(row.items(), key=lambda kv: (-kv[1], kv[0]))
        top_pid, top_score = ranked[0]
        neg_detail[neg["id"]] = {
            "question": neg["question"],
            "category": neg.get("category"),
            "gated": neg.get("gated"),
            "top_passage": top_pid,
            "top_score": top_score,
            "top3": [{"passage": pid, "score": sc} for pid, sc in ranked[:3]],
        }

    pos_best = {qid: detail["best_score"] for qid, detail in pos_detail.items()}
    neg_worst = {qid: detail["top_score"] for qid, detail in neg_detail.items()}
    min_pos = min(pos_best.values())
    max_neg = max(neg_worst.values())
    min_pos_qid = min(pos_best, key=lambda qid: (pos_best[qid], qid))
    max_neg_qid = max(neg_worst, key=lambda qid: (neg_worst[qid], qid))

    separation = {
        "min_score_positifs": min_pos,
        "min_score_positifs_question": min_pos_qid,
        "max_score_negatifs": max_neg,
        "max_score_negatifs_question": max_neg_qid,
        "marge": r6(min_pos - max_neg),
        "separe": bool(min_pos > max_neg),
    }

    grid = []
    if separation["separe"]:
        barrier = r4((min_pos + max_neg) / 2.0)
        mode = "separe_milieu"
    else:
        values = sorted({r6(v) for v in pos_best.values()} | {r6(v) for v in neg_worst.values()})
        thresholds = [(a + b) / 2.0 for a, b in zip(values, values[1:])]
        if not thresholds:
            barrier = r4(values[0])
            mode = "chevauchement_valeur_unique"
        else:
            best_key = None
            best_threshold = None
            for threshold in thresholds:
                recall = sum(1 for v in pos_best.values() if v >= threshold) / len(pos_best)
                rejection = sum(1 for v in neg_worst.values() if v < threshold) / len(neg_worst)
                grid.append({"seuil": r6(threshold), "rappel": round(recall, 4), "rejet": round(rejection, 4)})
                key = (round(recall + rejection, 9), recall, rejection, threshold)
                if best_key is None or key > best_key:
                    best_key = key
                    best_threshold = threshold
            barrier = r4(best_threshold if best_threshold is not None else thresholds[-1])
            mode = "chevauchement_meilleur_couple_rappel_rejet"

    served_pos = [qid for qid, v in pos_best.items() if v >= barrier]
    missed_pos = [qid for qid, v in pos_best.items() if v < barrier]
    served_neg = [qid for qid, v in neg_worst.items() if v >= barrier]
    rejected_neg = [qid for qid, v in neg_worst.items() if v < barrier]

    payload = {
        "kind": "reranker-probe-calibration",
        "at": utcnow(),
        "model": model_info["model_id"],
        "revision": REVISION,
        "rule": EXCLUSION_NOTE,
        "dataset": {
            "name": "calibration-nova-boreal v1 (fixtures, textes intacts)",
            "passages": len(passages),
            "positives": len(positives),
            "negatives": len(negatives),
            "negatifs_proches": [neg["id"] for neg in negatives if neg.get("category") == "proche"],
            "negatifs_metadonnees": [neg["id"] for neg in negatives if neg.get("category") == "piege_metadonnees"],
            "pairs_scored": len(pairs),
        },
        "separation": separation,
        "scores_bruts": {
            "matrice_question_passage_logit": matrix,
            "positifs": pos_detail,
            "negatifs": neg_detail,
        },
        "grid": grid,
        "barrier": barrier,
        "barrier_mode": mode,
        "metrics_at_barrier": {
            "rappel_positifs": f"{len(served_pos)}/{len(positives)}",
            "rejet_negatifs": f"{len(rejected_neg)}/{len(negatives)}",
            "positifs_servis": served_pos,
            "positifs_manqués": missed_pos,
            "negatifs_rejetes": rejected_neg,
            "negatifs_servis": served_neg,
        },
        "ambiguity_note": (
            "Les négatifs proches (neg13, neg14 « firmware documentation ») et les pièges de "
            "métadonnées (neg11, neg12) sont exposés avec leurs scores bruts et leur passage "
            "le plus proche : aucune suppression, l'ambiguïté reste visible."
        ),
        "timing": {"pairs": len(pairs), "seconds": round(elapsed, 3), "batch_size": batch_size,
                   "batch_times_s": [round(t, 4) for t in batch_times],
                   "pairs_per_s": round(len(pairs) / elapsed, 1) if elapsed > 0 else None},
    }
    write_json(out_dir / "reranker-probe-calibration.json", payload)
    return {"payload": payload, "pos_best": pos_best, "neg_worst": neg_worst, "barrier": barrier,
            "calibration_sha256": sha256_file(out_dir / "reranker-probe-calibration.json")}


def phase3b_freeze(freeze: dict, out_dir: Path) -> dict:
    payload = {
        "kind": "reranker-probe-frozen-params",
        "at": utcnow(),
        "status": "frozen_before_acceptance",
        "acceptance_not_yet_evaluated": True,
        "barrier": freeze["barrier"],
        "barrier_mode": freeze["payload"]["barrier_mode"],
        "criterion": EXCLUSION_NOTE,
        "calibration_file": "reranker-probe-calibration.json",
        "calibration_sha256": freeze["calibration_sha256"],
        "no_acceptance_loop": "barrière gelée ici ; l'acceptance ne peut pas la modifier (aucun réglage en boucle).",
    }
    write_json(out_dir / "reranker-probe-frozen-params.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Phase 4 — acceptance (filtres recette en amont, puis barrière gelée)
# ---------------------------------------------------------------------------
def _chunks_for(corpus: dict, *, product: str | None, version: str | None) -> list[dict]:
    documents = {doc["id"]: doc for doc in corpus["documents"]}
    selected = []
    for chunk in corpus["chunks"]:
        doc = documents[chunk["document_id"]]
        if doc.get("status") != "ready" or chunk.get("generation") != doc.get("current_generation"):
            continue
        if product is not None and doc.get("product") != product:
            continue
        if version is not None and version not in (doc.get("versions") or []):
            continue
        selected.append({**chunk, "doc": doc})
    return selected


def phase4_acceptance(context: dict, phase1: dict, frozen: dict, batch_size: int, out_dir: Path) -> dict:
    barrier = frozen["barrier"]
    corpus = phase1["corpus"]
    seven_key = f"{phase1['seven_chunk']['document_id']}:{phase1['seven_chunk']['seq']}"
    q_pos = phase1["queries"]["q-positif-fr-en"]["text"]
    q_neg = phase1["queries"]["q-negatif-commercial"]["text"]

    scenarios = [
        {"id": "positif_sans_filtre", "question": q_pos, "product": None, "version": None},
        {"id": "positif_filtre_aster_10_10", "question": q_pos, "product": "Aster", "version": "10.10"},
        {"id": "version_10_9", "question": q_pos, "product": None, "version": "10.9"},
        {"id": "version_99_99", "question": q_pos, "product": None, "version": "99.99"},
        {"id": "negatif_commercial_sans_filtre", "question": q_neg, "product": None, "version": None},
    ]

    results = {}
    total_seconds = 0.0
    for scenario in scenarios:
        chunks = _chunks_for(corpus, product=scenario["product"], version=scenario["version"])
        pairs = [(scenario["question"], chunk["text"] or "") for chunk in chunks]
        t0 = time.perf_counter()
        scores, _ = score_pairs(context, pairs, batch_size)
        elapsed = time.perf_counter() - t0
        total_seconds += elapsed
        ranked = []
        for index, (chunk, score) in enumerate(zip(chunks, scores)):
            ranked.append(
                {
                    "chunk_id": f"{chunk['document_id']}:{chunk['seq']}",
                    "document_id": chunk["document_id"],
                    "title": chunk["doc"]["title"],
                    "versions": chunk["doc"]["versions"],
                    "page_start": chunk["page_start"],
                    "page_end": chunk["page_end"],
                    "seq": chunk["seq"],
                    "score": r6(score),
                    "served": bool(score >= barrier),
                    "has_seven_days": "seven days" in (chunk["text"] or "").lower(),
                }
            )
        ranked.sort(key=lambda item: (-item["score"], item["document_id"], item["seq"]))
        for position, item in enumerate(ranked, start=1):
            item["rang_tous_candidats"] = position
        served = [item for item in ranked if item["served"]]
        for position, item in enumerate(served, start=1):
            item["rang_servis"] = position
        seven_item = next((item for item in ranked if item["chunk_id"] == seven_key), None)
        results[scenario["id"]] = {
            "filters": {"product": scenario["product"], "version": scenario["version"]},
            "candidats": len(chunks),
            "servis": len(served),
            "seven_days": (
                {
                    "servi": bool(seven_item["served"]),
                    "rang_servis": seven_item.get("rang_servis"),
                    "rang_tous_candidats": seven_item["rang_tous_candidats"],
                    "score": seven_item["score"],
                    "page": f"{seven_item['page_start']}-{seven_item['page_end']}",
                    "titre": seven_item["title"],
                }
                if seven_item is not None
                else None
            ),
            "servis_detail": [
                {
                    "rang_servis": item.get("rang_servis"),
                    "chunk_id": item["chunk_id"],
                    "titre": item["title"],
                    "page": f"{item['page_start']}-{item['page_end']}",
                    "versions": item["versions"],
                    "score": item["score"],
                    "has_seven_days": item["has_seven_days"],
                }
                for item in served
            ],
            "classement_complet": [
                {
                    "rang": item["rang_tous_candidats"],
                    "chunk_id": item["chunk_id"],
                    "titre": item["title"],
                    "page": f"{item['page_start']}-{item['page_end']}",
                    "versions": item["versions"],
                    "score": item["score"],
                    "servi": item["served"],
                    "has_seven_days": item["has_seven_days"],
                }
                for item in ranked
            ],
            "seconds": round(elapsed, 3),
        }

    c_109 = results["version_10_9"]
    c_109_versions_ok = all(
        "10.9" in item["versions"] and "10.10" not in item["versions"]
        for item in c_109["classement_complet"]
    )
    seven_in_109_scope = any(item["has_seven_days"] for item in c_109["classement_complet"])
    c_109["reponse_seven_days_dans_perimetre"] = seven_in_109_scope

    checks = {
        "positif_sans_filtre_seven_days_servi": {
            "ok": bool(results["positif_sans_filtre"]["seven_days"] and results["positif_sans_filtre"]["seven_days"]["servi"] and results["positif_sans_filtre"]["seven_days"]["rang_servis"] <= 6),
            "detail": "sans filtre : passage Quick Start (EN) p.2 « seven days » servi avec rang ≤ 6",
        },
        "positif_filtre_aster_10_10_seven_days_servi": {
            "ok": bool(results["positif_filtre_aster_10_10"]["seven_days"] and results["positif_filtre_aster_10_10"]["seven_days"]["servi"] and results["positif_filtre_aster_10_10"]["seven_days"]["rang_servis"] <= 6),
            "detail": "périmètre product=Aster/version=10.10 : « seven days » servi avec rang ≤ 6",
        },
        "negatif_commercial_aucune_source": {
            "ok": results["negatif_commercial_sans_filtre"]["servis"] == 0,
            "detail": "aucun passage au-dessus de la barrière pour la question commerciale",
        },
        "version_99_99_rien": {
            "ok": results["version_99_99"]["candidats"] == 0 and results["version_99_99"]["servis"] == 0,
            "detail": "version inconnue : zéro candidat, aucune substitution",
        },
        "version_10_9_filtrage_sans_10_10": {
            "ok": bool(c_109["candidats"] > 0 and c_109_versions_ok),
            "detail": "restriction 10.9 : candidats uniquement 10.9 (aucune fuite 10.10) ; la réponse « seven days » est ABSENTE du périmètre 10.9 (absence connue, distincte du filtrage)",
        },
    }
    ok = all(check["ok"] for check in checks.values())
    payload = {
        "kind": "reranker-probe-acceptance",
        "at": utcnow(),
        "frozen_barrier": barrier,
        "frozen_params_file": "reranker-probe-frozen-params.json",
        "definition_six_passages": (
            "« les 6 passages éligibles classés » = capacité de service top_k=6 du retrieval de recette "
            "(WALLIA_RETRIEVAL_TOP_K=6) ; critère : passage servi (score ≥ barrière gelée) avec rang ≤ 6 "
            "dans la liste servie. Rang et score bruts exposés dans tous les cas."
        ),
        "no_loop": "aucun ajustement de barrière sur l'acceptance ; paramètres appliqués tels que gelés.",
        "chunk_id_note": "chunk_id = identifiant synthétique « document_id:seq » — l'export live-corpus.json ne contient pas les UUID de chunks (l'identité stable disponible est (document_id, generation, seq)).",
        "corpus_source": {
            "source": corpus.get("source"),
            "documents": corpus["counts"]["documents"],
            "chunks": corpus["counts"]["chunks"],
            "chunks_sha256": corpus.get("chunks_sha256"),
        },
        "question": q_pos,
        "negative": q_neg,
        "scenarios": results,
        "checks": checks,
        "ok": ok,
        "seconds_total": round(total_seconds, 3),
    }
    write_json(out_dir / "reranker-probe-acceptance.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Phase 5 — performances + mémoire + résumé
# ---------------------------------------------------------------------------
def phase5_perf(context: dict, phase1: dict, phase2: dict, calibration: dict, acceptance: dict, batch_size: int, out_dir: Path) -> dict:
    corpus = phase1["corpus"]
    q_pos = phase1["queries"]["q-positif-fr-en"]["text"]
    texts = [chunk["text"] or "" for chunk in corpus["chunks"]]
    top30_texts = (texts * 2)[:30]
    construction = f"{len(texts)} textes réels du corpus + {30 - len(texts)} duplications pour atteindre la taille candidate=30 (WALLIA_RETRIEVAL_CANDIDATES=30)"
    runs = []
    for _ in range(3):
        t0 = time.perf_counter()
        scores, batch_times = score_pairs(context, [(q_pos, text) for text in top30_texts], batch_size)
        elapsed = time.perf_counter() - t0
        runs.append({"seconds": round(elapsed, 4), "pairs": len(scores), "batch_times_s": [round(t, 4) for t in batch_times]})
    median = sorted(run["seconds"] for run in runs)[len(runs) // 2]

    model_bytes = sum(path.stat().st_size for path in Path("/model").rglob("*") if path.is_file())
    payload = {
        "kind": "reranker-probe-perf",
        "at": utcnow(),
        "cold_load": phase2["info"]["cold_load"],
        "calibration_scan": calibration["payload"]["timing"],
        "acceptance_scan_seconds": acceptance["seconds_total"],
        "top30": {
            "description": f"question FR vs 30 paires ({construction}) ; {len(runs)} exécutions",
            "runs": runs,
            "median_seconds": median,
            "per_pair_ms": round(1000 * median / 30, 2),
        },
        "memory": {
            "vm_hwm_kb": vm_hwm_kb(),
            "ru_maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "cgroup_memory_peak_bytes": read_cgroup_value("/sys/fs/cgroup/memory.peak"),
            "cgroup_memory_max_bytes": read_cgroup_value("/sys/fs/cgroup/memory.max"),
        },
        "disk": {"model_dir_bytes": model_bytes, "out_dir_bytes": sum(p.stat().st_size for p in out_dir.iterdir() if p.is_file())},
        "batch_size": batch_size,
        "max_length": MAX_LENGTH,
    }
    write_json(out_dir / "reranker-probe-perf.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--calibration", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    out_dir = Path(args.out)

    log(f"démarrage probe — modèle={MODEL_ID} révision={REVISION}")
    environment = phase0_environment()
    phase1 = phase1_inputs(args)
    log("entrées vérifiées : question/negatif exacts, 1 passage « seven days », SHA staging enregistrés")

    phase2 = phase2_load_model(args)

    calibration = phase3_calibration(phase2, phase1, args.batch_size, out_dir, phase2["info"])
    log(
        "calibration : min_pos={} max_neg={} marge={} barrière={} ({})".format(
            calibration["payload"]["separation"]["min_score_positifs"],
            calibration["payload"]["separation"]["max_score_negatifs"],
            calibration["payload"]["separation"]["marge"],
            calibration["barrier"],
            calibration["payload"]["barrier_mode"],
        )
    )
    frozen = phase3b_freeze(calibration, out_dir)
    log("paramètres GELÉS avant acceptance (aucune boucle d'acceptance)")

    acceptance = phase4_acceptance(phase2, phase1, frozen, args.batch_size, out_dir)
    for name, check in acceptance["checks"].items():
        log(f"  [{'OK' if check['ok'] else 'ÉCHEC'}] {name} — {check['detail']}")
    log(f"acceptance : {'OK' if acceptance['ok'] else 'ÉCHEC'}")

    perf = phase5_perf(phase2, phase1, phase2, calibration, acceptance, args.batch_size, out_dir)

    summary = {
        "kind": "reranker-probe-summary",
        "at": utcnow(),
        "model": phase2["info"],
        "environment": environment,
        "inputs": phase1["sha256"],
        "frozen_barrier": frozen["barrier"],
        "calibration_metrics": calibration["payload"]["metrics_at_barrier"],
        "calibration_separation": calibration["payload"]["separation"],
        "acceptance_checks": acceptance["checks"],
        "acceptance_ok": acceptance["ok"],
        "perf": {
            "cold_load_s": perf["cold_load"]["total_s"],
            "top30_median_s": perf["top30"]["median_seconds"],
            "top30_per_pair_ms": perf["top30"]["per_pair_ms"],
            "vm_hwm_kb": perf["memory"]["vm_hwm_kb"],
            "cgroup_memory_peak_bytes": perf["memory"]["cgroup_memory_peak_bytes"],
            "cgroup_memory_max_bytes": perf["memory"]["cgroup_memory_max_bytes"],
            "model_dir_bytes": perf["disk"]["model_dir_bytes"],
        },
        "notes": [
            "Cross-encoder CPU seul ; aucune E5 chargée ; aucune intégration applicative.",
            "Sortie = logit brut ; aucune probabilité.",
            "Aucun ajustement de seuil sur l'acceptance (gel préalable).",
        ],
    }
    write_json(out_dir / "reranker-probe-summary.json", summary)
    log(f"terminé — acceptance={acceptance['ok']} rc=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
