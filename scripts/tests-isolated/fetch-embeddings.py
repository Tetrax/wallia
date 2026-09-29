#!/usr/bin/env python3
"""Outil hôte (lot3) : vecteurs E5 réels via l'endpoint interne de l'API vivante.

Le token interne est lu silencieusement dans runtime/secrets/worker_token et
n'est jamais affiché. Seuls des textes bornés sont envoyés ; seules des données
non secrètes (textes fictifs + vecteurs) sont écrites dans l'espace de tests.
Aucune écriture DB, aucun modèle chargé côté hôte.

Modes :
  fetch-embeddings.py --calibration fixtures/calibration/calibration.json --out <vectors.json>
  fetch-embeddings.py --acceptance --out <vectors.json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path("/home/tetrax/workspace/wallia")
ENDPOINT = "http://127.0.0.1:13745/internal/embeddings"
MAX_TEXTS = 64
MAX_CHARS = 8000

ACCEPTANCE_QUERIES = [
    ("q-positif-fr-en", "Combien de jours de journaux la rotation conserve-t-elle dans Aster 10.10 ?", "query"),
    ("q-negatif-commercial", "Quel est le tarif de la licence annuelle en euros et les conditions commerciales de revente ?", "query"),
]


def _token() -> str:
    return (ROOT / "runtime/secrets/worker_token").read_text(encoding="utf-8").strip()


def _embed(texts: list[str], kind: str) -> tuple[list[list[float]], dict]:
    body = json.dumps({"kind": kind, "texts": texts}).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json", "X-Internal-Token": _token()},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.loads(response.read().decode("utf-8"))
    info = {k: v for k, v in payload.items() if k != "vectors"}
    return payload["vectors"], info


def _batches(items: list[dict]) -> list[list[dict]]:
    return [items[i : i + MAX_TEXTS] for i in range(0, len(items), MAX_TEXTS)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibration", type=pathlib.Path)
    parser.add_argument("--acceptance", action="store_true")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()

    items: list[dict] = []
    if args.calibration:
        calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
        for passage in calibration["corpus"]:
            items.append({"id": passage["id"], "kind": "passage", "text": passage["text"]})
        for question in calibration["positives"] + calibration["negatives"]:
            items.append({"id": question["id"], "kind": "query", "text": question["question"]})
    elif args.acceptance:
        for item_id, text, kind in ACCEPTANCE_QUERIES:
            items.append({"id": item_id, "kind": kind, "text": text})
    else:
        raise SystemExit("mode requis : --calibration <fichier> ou --acceptance")

    for item in items:
        if len(item["text"]) > MAX_CHARS:
            raise SystemExit(f"texte trop long pour {item['id']}")

    results: list[dict] = []
    info: dict = {}
    for kind in ("passage", "query"):
        batch_items = [item for item in items if item["kind"] == kind]
        for batch in _batches(batch_items):
            vectors, info = _embed([item["text"] for item in batch], kind)
            if len(vectors) != len(batch):
                raise SystemExit("réponse d'embeddings incomplète")
            for item, vector in zip(batch, vectors):
                results.append({**item, "vector": vector})

    digest = hashlib.sha256(
        json.dumps([{"id": r["id"], "text": r["text"]} for r in results], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    document = {
        "endpoint": ENDPOINT,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "info": info,
        "texts_sha256": digest,
        "items": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[fetch] {len(results)} vecteurs dim={len(results[0]['vector'])} backend={info.get('backend')} model={info.get('model')} revision={str(info.get('revision'))[:12]}…")
    print(f"[fetch] texts_sha256={digest[:16]}… -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
