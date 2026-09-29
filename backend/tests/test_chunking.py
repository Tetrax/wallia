"""Découpage des passages (chunking) — unitaire, sans base de données."""
from __future__ import annotations

from app.chunking import approx_token_counter, chunk_items


def count(text: str) -> int:
    return approx_token_counter(text)


def test_paragraphs_are_merged_up_to_the_token_budget():
    items = [{"kind": "text", "label": "paragraph", "text": f"Paragraphe numéro {i} " + "mot " * 30, "page_no": 1} for i in range(6)]
    chunks = chunk_items(items, count, max_tokens=200, min_tokens=10)
    assert chunks
    assert all(chunk["token_count"] <= 200 for chunk in chunks)
    assert len(chunks) < len(items)  # fusion effective
    assert [chunk["seq"] for chunk in chunks] == list(range(1, len(chunks) + 1))


def test_long_text_is_split_without_loss_of_content():
    sentences = [f"Phrase {i} avec du contenu technique détaillé sur l'équipement." for i in range(60)]
    items = [{"kind": "text", "label": "paragraph", "text": " ".join(sentences), "page_no": 3}]
    chunks = chunk_items(items, count, max_tokens=100, min_tokens=10)
    assert len(chunks) > 1
    assert all(chunk["token_count"] <= 100 for chunk in chunks)
    joined = " ".join(chunk["text"] for chunk in chunks)
    assert "Phrase 0" in joined and "Phrase 59" in joined


def test_tables_are_kept_as_single_passages():
    table = "| Version | État |\n| --- | --- |\n" + "\n".join(f"| 10.{i} | OK |" for i in range(40))
    items = [
        {"kind": "text", "label": "paragraph", "text": "Avant tableau.", "page_no": 1},
        {"kind": "table", "label": "table", "text": table, "page_no": 2},
        {"kind": "text", "label": "paragraph", "text": "Après tableau.", "page_no": 2},
    ]
    chunks = chunk_items(items, count, max_tokens=500, min_tokens=5)
    tables = [chunk for chunk in chunks if chunk["kind"] == "table"]
    assert len(tables) == 1
    assert tables[0]["text"].startswith("| Version | État |")
    assert tables[0]["page_start"] == 2


def test_page_ranges_and_sections_are_preserved():
    items = [
        {"kind": "text", "label": "section_header", "text": "Section A", "page_no": 2, "section": None},
        {"kind": "text", "label": "paragraph", "text": "Contenu de la section A.", "page_no": 2, "section": "Section A"},
        {"kind": "text", "label": "paragraph", "text": "Suite page 3.", "page_no": 3, "section": "Section A"},
    ]
    chunks = chunk_items(items, count, max_tokens=400, min_tokens=5)
    assert chunks[0]["section"] == "Section A"
    merged = chunks[0]
    assert merged["page_start"] == 2 and merged["page_end"] == 3


def test_headers_footers_are_skipped():
    items = [
        {"kind": "text", "label": "page_header", "text": "En-tête confidentiel", "page_no": 1},
        {"kind": "text", "label": "paragraph", "text": "Contenu réel utile.", "page_no": 1},
        {"kind": "text", "label": "page_footer", "text": "Page 1/10", "page_no": 1},
        {"kind": "text", "label": "reference", "text": "[1] Référence technique", "page_no": 1},
    ]
    chunks = chunk_items(items, count, max_tokens=400, min_tokens=1)
    joined = " ".join(chunk["text"] for chunk in chunks)
    assert "Contenu réel utile" in joined
    assert "En-tête confidentiel" not in joined
    assert "Page 1/10" not in joined
    assert "Référence technique" not in joined


def test_empty_items_yield_no_chunks():
    assert chunk_items([], count) == []
    assert chunk_items([{"kind": "text", "label": "paragraph", "text": "   ", "page_no": 1}], count) == []


def test_oversized_table_header_is_bounded_and_nothing_is_lost():
    """En-tête de tableau plus long que la fenêtre : borné, répété seulement si
    possible, et AUCUN passage au-dessus de la fenêtre — sans perte de contenu."""
    header_cells = " ".join(f"entete{i}" for i in range(400))
    header = f"| {header_cells} |\n| --- |"
    rows = "\n".join(f"| ligne {index} valeur |" for index in range(4))
    chunks = chunk_items(
        [{"kind": "table", "label": "table", "text": header + "\n" + rows, "page_no": 3}],
        approx_token_counter,
        max_tokens=120,
    )
    assert chunks
    for chunk in chunks:
        assert approx_token_counter(chunk["text"]) <= 120
    joined = "".join(chunk["text"] for chunk in chunks)
    for token in header_cells.split():
        assert token in joined, f"contenu d'en-tête perdu: {token}"
    for index in range(4):
        assert f"ligne {index} valeur" in joined
    assert all(chunk["page_start"] == 3 for chunk in chunks)


def test_single_oversized_word_is_split_without_loss_and_bounded():
    word = "z" * 6000
    chunks = chunk_items([{"kind": "text", "label": "paragraph", "text": word, "page_no": 2}], approx_token_counter, max_tokens=100)
    assert chunks
    for chunk in chunks:
        assert approx_token_counter(chunk["text"]) <= 100
    assert "".join(chunk["text"] for chunk in chunks).replace("\n", "") == word
