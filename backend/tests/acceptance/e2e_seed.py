"""Seed du runtime UI de recette isolé (lot4) — compte de recette + corpus démo.

Idempotent et rejouable. Double UI EXPLICITEMENT étiqueté : les vecteurs sont
produits par le backend d'embeddings `fixture` (mécanique d'interface, jamais
une preuve sémantique/RAG). Aucune donnée réelle, aucun secret de livraison.

    docker compose -p wallia-e2e -f docker-compose.e2e-isolated.yml run --rm seed
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.embeddings import get_embedding_service  # noqa: E402
from app.migrate import run_migrations  # noqa: E402
from app.models import Chunk, Document, User  # noqa: E402
from app.security import hash_password, utcnow  # noqa: E402

CREDENTIALS_PATH = Path("/run/e2e/credentials.json")

ASTER_TEXT_1 = (
    "Fiche de démonstration (non officielle). Sur l'Aster 10.10, le voyant ambre "
    "clignote lorsque la sonde détecte une tension instable. Contrôle proposé : "
    "relever le journal local et confirmer la version exacte au redémarrage."
)
ASTER_TEXT_2 = (
    "Aster 10.10 — procédure de démonstration : la remise à zéro de la sonde se "
    "fait hors tension, jamais à chaud. Vérifier la tension après redémarrage."
)
BOREAS_TEXT_1 = (
    "Fiche de démonstration (non officielle). Sur le Boreas 9.9, la sonde rouge "
    "signale un défaut de calibration. Contrôle proposé : vérifier le capteur et "
    "relever la mesure d'étalonnage."
)


def _pdf_bytes(title: str, body: str) -> bytes:
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 760, title)
    y = 730
    for line in body.split(" "):
        pdf.drawString(72, y, line)
        y -= 14
        if y < 72:
            pdf.showPage()
            y = 760
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _write_original(relpath: str, data: bytes) -> None:
    settings = get_settings()
    target = settings.documents_dir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _upsert_user(email: str, password: str) -> str:
    with session_scope() as db:
        existing = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()
        if existing is not None:
            return "exists"
        user = User(email=email, password_hash=hash_password(password), is_admin=True)
        db.add(user)
        db.flush()
        user.password_changed_at = utcnow()
        return "created"


def _upsert_document(
    *,
    title: str,
    product: str,
    versions: list[str],
    filename: str,
    relpath: str,
    page_count: int,
    texts: list[tuple[str, int]],
) -> str:
    settings = get_settings()
    with session_scope() as db:
        existing = db.execute(select(Document).where(Document.title == title)).scalar_one_or_none()
        if existing is not None:
            return "exists"
    data = _pdf_bytes(title, " ".join(t for t, _ in texts))
    _write_original(relpath, data)
    checksum = hashlib.sha256(data).hexdigest()
    service = get_embedding_service()
    vectors = service.encode([text for text, _ in texts], kind="passage")
    with session_scope() as db:
        document = Document(
            title=title,
            origin="demo",
            product=product,
            versions=versions,
            language="fr",
            checksum_sha256=checksum,
            demo=True,
            scope="demo",
            status="ready",
            current_generation=1,
            embedding_model=getattr(service, "model", None) or "fixture",
            embedding_revision="e2e-fixture",
            embedding_dim=len(vectors[0]),
            stored_relpath=relpath,
            original_filename=filename,
            content_type="application/pdf",
            size_bytes=len(data),
            page_count=page_count,
        )
        db.add(document)
        db.flush()
        for index, ((text, page), vector) in enumerate(zip(texts, vectors), start=1):
            db.add(
                Chunk(
                    document_id=document.id,
                    generation=1,
                    seq=index,
                    text=text,
                    page_start=page,
                    page_end=page,
                    section="Voyants",
                    kind="text",
                    token_count=len(text) // 4,
                    embedding=vector,
                )
            )
    return "created"


def main() -> int:
    report: dict = {"migrations": run_migrations()}
    credentials = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    report["user"] = _upsert_user(credentials["email"], credentials["password"])
    report["documents"] = {
        "aster": _upsert_document(
            title="Fiche démo Aster 10.10 — voyant ambre (non officielle)",
            product="Aster",
            versions=["10.10"],
            filename="aster-10.10-demo.pdf",
            relpath="demo/aster-10.10-demo.pdf",
            page_count=2,
            texts=[(ASTER_TEXT_1, 1), (ASTER_TEXT_2, 2)],
        ),
        "boreas": _upsert_document(
            title="Fiche démo Boreas 9.9 — sonde rouge (non officielle)",
            product="Boreas",
            versions=["9.9"],
            filename="boreas-9.9-demo.pdf",
            relpath="demo/boreas-9.9-demo.pdf",
            page_count=1,
            texts=[(BOREAS_TEXT_1, 1)],
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())