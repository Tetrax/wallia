"""Découpage en passages exploitables.

Les passages sont construits depuis les items Docling (texte, tableaux) en
respectant la fenêtre effective du tokenizer E5 (512 tokens, marge de sécurité).
Aucune page ni aucun vecteur ne sont simulés : chaque passage garde sa
provenance de page réelle.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

TokenCounter = Callable[[str], int]

MAX_TOKENS_DEFAULT = 450
MIN_TOKENS_DEFAULT = 8
SKIP_LABELS = {"page_header", "page_footer", "reference"}
HEADER_LABELS = {"title", "section_header"}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;:])\s+")


def approx_token_counter(text: str) -> int:
    """Approximation explicite (tests uniquement, jamais en production)."""
    return max(1, len(text) // 4)


def make_token_counter(model_dir: Path, allow_approx: bool) -> TokenCounter:
    """Tokenizer réel du modèle E5 (local). L'approximation est interdite hors test."""
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)

        def count(text: str) -> int:
            return len(tokenizer.encode(text, add_special_tokens=False))

        return count
    except Exception as exc:
        if not allow_approx:
            raise RuntimeError(
                f"tokenizer indisponible dans {model_dir}; découpage refusé (pas d'approximation)"
            ) from exc
        return approx_token_counter


def _split_long_text(text: str, counter: TokenCounter, max_tokens: int) -> list[str]:
    if counter(text) <= max_tokens:
        return [text]
    # 1) paragraphes, puis 2) phrases, puis 3) coupe brute bornée.
    parts: list[str] = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if counter(paragraph) <= max_tokens:
            parts.append(paragraph)
            continue
        buffer = ""
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            candidate = f"{buffer} {sentence}".strip() if buffer else sentence
            if counter(candidate) <= max_tokens:
                buffer = candidate
                continue
            if buffer:
                parts.append(buffer)
            if counter(sentence) <= max_tokens:
                buffer = sentence
            else:
                words = sentence.split(" ")
                buffer = ""
                for word in words:
                    candidate = f"{buffer} {word}".strip() if buffer else word
                    if counter(candidate) <= max_tokens:
                        buffer = candidate
                    else:
                        if buffer:
                            parts.append(buffer)
                        buffer = word[: max_tokens * 8]
        if buffer:
            parts.append(buffer)
    return parts or [text[: max_tokens * 8]]


def _split_table(text: str, counter: TokenCounter, max_tokens: int) -> list[str]:
    if counter(text) <= max_tokens:
        return [text]
    lines = text.splitlines()
    if len(lines) <= 2:
        return _split_long_text(text, counter, max_tokens)
    header = lines[:2]
    rows = lines[2:]
    parts: list[str] = []
    buffer = list(header)
    for row in rows:
        candidate = buffer + [row]
        if counter("\n".join(candidate)) <= max_tokens or len(buffer) <= len(header):
            buffer = candidate
            continue
        parts.append("\n".join(buffer))
        buffer = list(header) + [row]
    if len(buffer) > len(header):
        parts.append("\n".join(buffer))
    return parts


def chunk_items(
    items: list[dict[str, Any]],
    counter: TokenCounter,
    max_tokens: int = MAX_TOKENS_DEFAULT,
    min_tokens: int = MIN_TOKENS_DEFAULT,
) -> list[dict[str, Any]]:
    """Retourne une liste de passages : {seq, text, page_start, page_end, section, kind, token_count}."""
    chunks: list[dict[str, Any]] = []
    buffer_texts: list[str] = []
    buffer_pages: list[int] = []
    buffer_section: str | None = None

    def flush() -> None:
        nonlocal buffer_texts, buffer_pages
        if not buffer_texts:
            return
        text = "\n\n".join(buffer_texts).strip()
        tokens = counter(text)
        if tokens < min_tokens and chunks:
            # rattache les fragments minuscules au passage précédent si possible
            prev = chunks[-1]
            merged = f"{prev['text']}\n\n{text}"
            if counter(merged) <= max_tokens:
                prev["text"] = merged
                prev["token_count"] = counter(merged)
                if buffer_pages:
                    prev["page_start"] = min(
                        p for p in [prev["page_start"], *buffer_pages] if p is not None
                    )
                    prev["page_end"] = max(p for p in [prev["page_end"], *buffer_pages] if p is not None)
                buffer_texts, buffer_pages = [], []
                return
        if tokens == 0:
            buffer_texts, buffer_pages = [], []
            return
        pages = [p for p in buffer_pages if p is not None]
        chunks.append(
            {
                "seq": len(chunks) + 1,
                "text": text,
                "page_start": min(pages) if pages else None,
                "page_end": max(pages) if pages else None,
                "section": buffer_section,
                "kind": "table" if text.lstrip().startswith("|") else "text",
                "token_count": tokens,
            }
        )
        buffer_texts, buffer_pages = [], []

    for item in items:
        label = str(item.get("label") or "")
        kind = str(item.get("kind") or "text")
        text = str(item.get("text") or "").strip()
        page = item.get("page_no")
        section = item.get("section")
        if not text or label in SKIP_LABELS and kind != "table":
            continue
        # Un titre ou un en-tête de section ouvre une nouvelle section : il la
        # nomme pour lui-même et pour les passages suivants.
        is_header = kind != "table" and label in HEADER_LABELS
        if is_header:
            flush()
            buffer_section = text
        effective_section = section if section else buffer_section
        pieces = (
            _split_table(text, counter, max_tokens)
            if kind == "table"
            else _split_long_text(text, counter, max_tokens)
        )
        for piece in pieces:
            piece_tokens = counter(piece)
            is_table = kind == "table" or piece.lstrip().startswith("|")
            if is_table:
                flush()
                buffer_section = effective_section
                buffer_texts = [piece]
                buffer_pages = [page] if page is not None else []
                flush()
                continue
            candidate_tokens = counter("\n\n".join(buffer_texts + [piece])) if buffer_texts else piece_tokens
            if buffer_texts and candidate_tokens > max_tokens:
                flush()
            if not buffer_texts:
                buffer_section = effective_section
            buffer_texts.append(piece)
            if page is not None:
                buffer_pages.append(page)
    flush()
    for index, chunk in enumerate(chunks, start=1):
        chunk["seq"] = index
    return chunks
