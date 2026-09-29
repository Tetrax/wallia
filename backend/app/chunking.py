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


def _split_oversized_piece(text: str, counter: TokenCounter, max_tokens: int) -> list[str]:
    """Découpe un fragment SANS ESPACE trop long en morceaux ≤ max_tokens.

    Aucune troncature silencieuse : la concaténation des morceaux restitue
    exactement le fragment d'origine. La borne est vérifiée par le tokenizer
    réel (recherche du plus grand préfixe accepté), jamais par une constante
    de caractères.
    """
    pieces: list[str] = []
    remaining = text
    while remaining:
        low, high = 1, len(remaining)
        best = 0
        while low <= high:
            mid = (low + high) // 2
            if counter(remaining[:mid]) <= max_tokens:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        if best <= 0:
            best = 1  # le tokenizer refuse même un caractère : progrès borné garanti
        pieces.append(remaining[:best])
        remaining = remaining[best:]
    return pieces


def _split_long_text(text: str, counter: TokenCounter, max_tokens: int) -> list[str]:
    if counter(text) <= max_tokens:
        return [text]
    # 1) paragraphes, puis 2) phrases, puis 3) mots, puis 4) coupe sans perte.
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
                buffer = ""
                for word in sentence.split(" "):
                    candidate = f"{buffer} {word}".strip() if buffer else word
                    if counter(candidate) <= max_tokens:
                        buffer = candidate
                    else:
                        if buffer:
                            parts.append(buffer)
                        word_pieces = _split_oversized_piece(word, counter, max_tokens)
                        parts.extend(word_pieces[:-1])
                        buffer = word_pieces[-1] if word_pieces else ""
        if buffer:
            parts.append(buffer)
    return parts or [text]


def _split_table(text: str, counter: TokenCounter, max_tokens: int) -> list[str]:
    if counter(text) <= max_tokens:
        return [text]
    lines = text.splitlines()
    if len(lines) <= 2:
        return _split_long_text(text, counter, max_tokens)
    header = lines[:2]
    header_text = "\n".join(header)
    header_tokens = counter(header_text)
    rows = lines[2:]
    # Un en-tête n'est répété que s'il laisse réellement de la place à une
    # ligne ; sinon il est préservé intégralement (découpé sans perte) et les
    # lignes suivent SANS répétition — jamais un passage hors fenêtre.
    repeat_header = header_tokens + 1 < max_tokens
    row_budget = max(1, max_tokens - header_tokens - 1) if repeat_header else max_tokens
    parts: list[str] = []
    if not repeat_header:
        parts.extend(_split_oversized_piece(header_text, counter, max_tokens))
    header_cells = list(header) if repeat_header else []
    buffer = list(header_cells)
    for row in rows:
        candidate = buffer + [row]
        if buffer and counter("\n".join(candidate)) <= max_tokens:
            buffer = candidate
            continue
        if len(buffer) > len(header_cells):
            parts.append("\n".join(buffer))
            buffer = list(header_cells)
        if repeat_header and counter(header_text + "\n" + row) <= max_tokens:
            buffer = list(header_cells) + [row]
            continue
        for sub in _split_oversized_piece(row, counter, row_budget):
            parts.append(f"{header_text}\n{sub}" if repeat_header else sub)
    if len(buffer) > len(header_cells):
        parts.append("\n".join(buffer))
    return _enforce_final_bounds(parts or [text], counter, max_tokens)


def _enforce_final_bounds(parts: list[str], counter: TokenCounter, max_tokens: int) -> list[str]:
    """Contrôle final : AUCUNE sortie au-dessus de la fenêtre, sans perte."""
    bounded: list[str] = []
    for piece in parts:
        if counter(piece) <= max_tokens:
            bounded.append(piece)
            continue
        for sub in _split_long_text(piece, counter, max_tokens):
            if counter(sub) <= max_tokens:
                bounded.append(sub)
            else:
                bounded.extend(_split_oversized_piece(sub, counter, max_tokens))
    return bounded


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
    # Contrôle final de TOUTE sortie : un passage ne dépasse jamais la fenêtre
    # du tokenizer, quel que soit le chemin qui l'a produit ; la provenance de
    # page est conservée pour chaque fragment issu d'un passage trop long.
    final: list[dict[str, Any]] = []
    for chunk in chunks:
        if counter(chunk["text"]) <= max_tokens:
            final.append(chunk)
            continue
        for piece in _enforce_final_bounds([chunk["text"]], counter, max_tokens):
            clone = dict(chunk)
            clone["text"] = piece
            clone["token_count"] = counter(piece)
            final.append(clone)
    for index, chunk in enumerate(final, start=1):
        chunk["seq"] = index
    return final
