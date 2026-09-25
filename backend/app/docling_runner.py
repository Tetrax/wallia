"""Extraction Docling exécutée dans un sous-processus borné.

Usage :
    python -m app.docling_runner --input <fichier.pdf> --output <dossier> [--models <dossier>]

Produit dans <dossier> :
    - extracted.json : métadonnées + items (texte/tableaux) avec page réelle
    - document.md    : export Markdown Docling complet (preuve lisible)
Usage strictement CPU, do_ocr=False, do_table_structure=True.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
from pathlib import Path
from typing import Any


def _apply_limits() -> None:
    # Bornes défensives du processus d'extraction (CPU, taille de fichier).
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (1200, 1200))
        resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024 * 1024, 200 * 1024 * 1024))
    except (ValueError, OSError):  # pragma: no cover - plateforme
        pass


def build_converter(models_dir: Path | None):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        AcceleratorDevice,
        AcceleratorOptions,
        PdfPipelineOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = True
    options.accelerator_options = AcceleratorOptions(num_threads=2, device=AcceleratorDevice.CPU)
    if models_dir is not None and models_dir.is_dir() and any(models_dir.iterdir()):
        options.artifacts_path = models_dir
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def extract(input_path: Path, output_dir: Path, models_dir: Path | None) -> dict[str, Any]:
    import docling

    converter = build_converter(models_dir)
    result = converter.convert(str(input_path))
    doc = result.document

    page_count = 0
    try:
        page_count = int(doc.num_pages())
    except Exception:
        page_count = 0

    items: list[dict[str, Any]] = []
    current_section: str | None = None
    for item, _level in doc.iterate_items():
        label = getattr(item, "label", None)
        label_value = getattr(label, "value", None) or (str(label) if label is not None else "text")
        text = getattr(item, "text", None)
        page_no: int | None = None
        prov = getattr(item, "prov", None) or []
        if prov:
            page_no = getattr(prov[0], "page_no", None)
        if label_value == "section_header" and text:
            current_section = str(text).strip()
        if label_value == "table":
            try:
                table_md = item.export_to_markdown(doc)
            except TypeError:  # signatures docling variables selon versions
                table_md = item.export_to_markdown()
            if table_md and str(table_md).strip():
                items.append(
                    {
                        "kind": "table",
                        "label": "table",
                        "text": str(table_md).strip(),
                        "page_no": page_no,
                        "section": current_section,
                    }
                )
            continue
        if text and str(text).strip():
            items.append(
                {
                    "kind": "text",
                    "label": label_value,
                    "text": str(text).strip(),
                    "page_no": page_no,
                    "section": current_section,
                }
            )

    if page_count == 0:
        pages = [i["page_no"] for i in items if i.get("page_no")]
        page_count = max(pages) if pages else 0

    markdown = ""
    try:
        markdown = doc.export_to_markdown()
    except Exception as exc:  # pragma: no cover - défensif
        markdown = f"<!-- export markdown indisponible: {exc} -->"

    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "engine": {
            "name": "docling",
            "version": getattr(docling, "__version__", "unknown"),
            "options": {"do_ocr": False, "do_table_structure": True, "device": "cpu"},
            "models_dir": str(models_dir) if models_dir else None,
        },
        "page_count": page_count,
        "items": items,
    }
    (output_dir / "extracted.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (output_dir / "document.md").write_text(markdown, encoding="utf-8")
    return {"pages": page_count, "items": len(items), "markdown_chars": len(markdown)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extraction Docling Wallia")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--models", default=os.environ.get("WALLIA_DOCLING_MODELS", ""))
    args = parser.parse_args(argv)

    _apply_limits()
    try:
        import torch

        torch.set_num_threads(int(os.environ.get("WALLIA_TORCH_THREADS", "2")))
    except Exception:  # pragma: no cover
        pass

    models_dir = Path(args.models) if args.models else None
    summary = extract(Path(args.input), Path(args.output), models_dir)
    print(json.dumps({"ok": True, **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
