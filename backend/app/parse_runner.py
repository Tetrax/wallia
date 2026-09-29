"""Parseur isolé exécuté en SOUS-PROCESSUS borné (jamais dans la boucle web).

Entrée : spécification JSON sur stdin `{"kind": ..., "path": ...}`.
Sortie : résultat JSON sur stdout (dernière ligne).
Types : `pdf_pages`, `pdf_text`, `image_meta`.

Le processus peut être tué à tout moment (dépassement de délai) : aucun état
partagé, aucune dépendance à la boucle d'événements. Sa MÉMOIRE est bornée par
`RLIMIT_AS` (plus RLIMIT_CPU/CORE) : un fichier pathologique échoue par
MemoryError contrôlée dans CE processus, jamais en consommant la mémoire du
conteneur API qui l'héberge.
"""
from __future__ import annotations

import io
import json
import os
import resource
import sys

MAX_TEXT_CHARS = 200_000
# Borne par défaut de l'espace d'adressage du parseur (surchargée par
# l'appelant via WALLIA_PARSE_MAX_MEMORY_BYTES, jamais par le fichier analysé).
MAX_MEMORY_DEFAULT_BYTES = 768 * 1024 * 1024
CPU_LIMIT_SECONDS = 60


def apply_parse_limits() -> None:
    """Applique les bornes défensives du processus d'analyse.

    Appelée au démarrage réel du parseur (et par les tests de régression :
    la limite est un invariant VÉRIFIABLE, pas une intention)."""
    try:
        limit = int(os.environ.get("WALLIA_PARSE_MAX_MEMORY_BYTES", str(MAX_MEMORY_DEFAULT_BYTES)))
    except ValueError:
        limit = MAX_MEMORY_DEFAULT_BYTES
    limit = max(64 * 1024 * 1024, limit)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):  # pragma: no cover - plateforme
        pass


def _pdf_reader(payload: bytes):
    import pypdf

    return pypdf.PdfReader(io.BytesIO(payload))


def _pdf_pages(data: bytes) -> dict:
    return {"pages": len(_pdf_reader(data).pages)}


def _pdf_text(data: bytes) -> dict:
    reader = _pdf_reader(data)
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - PDF exotique : texte partiel acceptable
            text = ""
        if text:
            parts.append(text)
            total += len(text)
        if total >= MAX_TEXT_CHARS:
            break
    return {"pages": len(reader.pages), "text": ("\n\n".join(parts))[:MAX_TEXT_CHARS]}


def _image_meta(data: bytes) -> dict:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    return {"width": width, "height": height}


def main() -> int:
    apply_parse_limits()
    spec = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    kind = str(spec.get("kind") or "")
    path = str(spec.get("path") or "")
    with open(path, "rb") as handle:
        data = handle.read()
    if kind == "pdf_pages":
        result = _pdf_pages(data)
    elif kind == "pdf_text":
        result = _pdf_text(data)
    elif kind == "image_meta":
        result = _image_meta(data)
    else:
        raise SystemExit(f"type d'analyse inconnu: {kind}")
    result["kind"] = kind
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
