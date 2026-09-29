"""Preuve mémoire bornée (lot4) — batchs MAX réels E5 (16) / CE (8), textes longs.

Complète la preuve de coexistence E5+CE (lot3d) en exerçant les batchs MAXIMUM
réels des deux services dans le même runner borné 2000 Mio :
  - E5 : 16 textes LONGS en UN lot (batch réel par défaut = 16) ;
  - CE : 8 paires (question, texte long) en UN lot (lots de 8 du service réel,
    512 tokens, logits bruts) ;
mesures : durées par charge, mémoire cgroup (memory.peak avant/après,
memory.events : oom / oom_kill), VmHWM, absence d'OOM.

Réutilise les services réels de l'app tels quels (aucun nouveau framework,
aucune modification applicative). Une charge lourde à la fois : E5 puis CE,
jamais en parallèle. Si un OOM était mesuré, la sérialisation petite existante
serait conservée plutôt qu'un service.

Sortie : lot4-memory-batches.json dans WALLIA_EVIDENCE_DIR.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

MEMORY_BOUND_BYTES = 2000 * 1024 * 1024
E5_BATCH_MAX = 16
CE_BATCH_MAX = 8
EXPECTED_DIM = 384
ROUNDS = 2


def _require_isolated() -> None:
    if os.environ.get("WALLIA_TEST_RUNNER") != "isolated":
        raise SystemExit("refus : cette preuve doit tourner dans le harnais isolé (WALLIA_TEST_RUNNER=isolated)")
    if "/secrets/db_password" in os.environ.get("WALLIA_TEST_DB_URL", "") or Path("/secrets/db_password").exists():
        raise SystemExit("refus : conteneur de livraison détecté")


def _configure_models() -> dict:
    """Reranker RÉEL + E5 RÉEL : variables posées AVANT tout import applicatif."""
    model_dir = Path(os.environ.get("WALLIA_ACCEPTANCE_RERANKER_MODEL", "/run/isolation/reranker-probe-model"))
    if not model_dir.is_dir():
        raise SystemExit(f"refus : modèle reranker absent du runner ({model_dir})")
    os.environ["WALLIA_RERANKER_BACKEND"] = "transformers"
    os.environ["WALLIA_RERANKER_MODEL_DIR"] = str(model_dir)
    os.environ["WALLIA_EMBEDDING_BACKEND"] = "e5"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    return {"reranker_model_dir": str(model_dir)}


def _read_int(path: str) -> int | None:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else None


def _read_events(path: str) -> dict[str, int]:
    events: dict[str, int] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                events[parts[0]] = int(parts[1])
    except OSError:
        pass
    return events


def _vm_hwm_kb() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def _long_texts(count: int) -> list[str]:
    """Textes LONGS déterministes (> 512 tokens, aucune donnée réelle) : phrases
    techniques répétées, pour exercer la troncature du CE et la charge mémoire."""
    base = (
        "La rotation des journaux conserve les archives pendant sept jours sur le "
        "disque local, puis les transferts chiffrés sont purgés automatiquement. "
        "Le plafond du journal local est limité par la configuration de version. "
        "Les journaux sont indexés par date, par sévérité et par identifiant de "
        "processus afin de faciliter la recherche lors d'un incident de production. "
    )
    return [f"[long-{index}] " + (base * 30) for index in range(count)]


def main() -> int:
    _require_isolated()
    models = _configure_models()

    from app.embeddings import get_embedding_service
    from app.reranking import get_reranker_service

    report: dict = {
        "kind": "lot4-memory-batches",
        "bound_bytes": MEMORY_BOUND_BYTES,
        "models": models,
        "e5_batch_max": E5_BATCH_MAX,
        "ce_batch_max": CE_BATCH_MAX,
        "rounds_requested": ROUNDS,
    }

    before_peak = _read_int("/sys/fs/cgroup/memory.peak")
    before_events = _read_events("/sys/fs/cgroup/memory.events")

    t0 = time.perf_counter()
    e5 = get_embedding_service()
    e5.warmup()
    report["e5_cold_load_s"] = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    reranker = get_reranker_service()
    reranker.warmup()
    report["ce_cold_load_s"] = round(time.perf_counter() - t0, 3)

    texts = _long_texts(E5_BATCH_MAX)
    question = "Combien de jours de journaux la rotation conserve-t-elle dans Aster 10.10 ?"

    rounds: list[dict] = []
    for round_index in range(ROUNDS):
        # UNE charge lourde à la fois : E5 d'abord, CE ensuite (jamais en parallèle).
        t0 = time.perf_counter()
        vectors = e5.encode(texts, kind="passage")
        e5_seconds = round(time.perf_counter() - t0, 4)
        t0 = time.perf_counter()
        logits = reranker.score(question, texts[:CE_BATCH_MAX])
        ce_seconds = round(time.perf_counter() - t0, 4)
        events = _read_events("/sys/fs/cgroup/memory.events")
        rounds.append(
            {
                "round": round_index + 1,
                "e5_texts": len(vectors),
                "e5_dims": len(vectors[0]) if vectors else 0,
                "e5_batch_seconds": e5_seconds,
                "ce_pairs": len(logits),
                "ce_batch_seconds": ce_seconds,
                "cgroup_peak_bytes": _read_int("/sys/fs/cgroup/memory.peak"),
                "events_oom": events.get("oom", 0),
                "events_oom_kill": events.get("oom_kill", 0),
            }
        )

    max_bytes = _read_int("/sys/fs/cgroup/memory.max")
    after_events = _read_events("/sys/fs/cgroup/memory.events")
    report.update(
        {
            "cgroup_memory_max_bytes": max_bytes,
            "cgroup_peak_before_bytes": before_peak,
            "cgroup_peak_after_bytes": _read_int("/sys/fs/cgroup/memory.peak"),
            "cgroup_events_before": before_events,
            "cgroup_events_after": after_events,
            "vm_hwm_kb": _vm_hwm_kb(),
            "rounds": rounds,
        }
    )
    try:
        import torch  # noqa: PLC0415

        report["torch"] = torch.__version__
    except Exception:  # noqa: BLE001
        report["torch"] = None

    checks = {
        "e5_batch_max_16_textes_longs": all(
            item["e5_texts"] == E5_BATCH_MAX and item["e5_dims"] == EXPECTED_DIM for item in rounds
        ),
        "ce_batch_max_8_textes_longs": all(item["ce_pairs"] == CE_BATCH_MAX for item in rounds),
        "sous_borne_2000_mio_sans_oom": (max_bytes is not None)
        and max_bytes <= MEMORY_BOUND_BYTES
        and after_events.get("oom_kill", 0) == 0
        and after_events.get("oom", 0) == 0,
        "durees_mesurees": all(
            item["e5_batch_seconds"] > 0 and item["ce_batch_seconds"] > 0 for item in rounds
        ),
    }
    report["checks"] = checks
    ok = all(checks.values())

    evidence_dir = Path(os.environ.get("WALLIA_EVIDENCE_DIR", "/run/isolation/evidence"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out = evidence_dir / "lot4-memory-batches.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print("Résultat preuve mémoire batchs max : " + ("OK" if ok else "ÉCHEC"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
