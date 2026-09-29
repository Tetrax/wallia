"""Déclarations explicites produit/version d'un message utilisateur.

Extraction strictement conservatrice :
- formulations explicites (« version 10.10 », « v10.10 », « produit : Aster ») ;
- produits connus du corpus mentionnés dans le message ;
- jamais une version déduite, une négation (« pas la version 10.9 ») ou une
  alternative (« 10.9 ou 10.10 ») promue en fait.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from .schemas import validate_version

# Versions : formulations explicitement déclaratives, jamais une déduction.
_VERSION_PATTERNS = (
    re.compile(r"\bversion\s*(?:n[o°]\s*)?[:=]?\s*([0-9][0-9A-Za-z._+-]{0,31})", re.IGNORECASE),
    re.compile(r"\bv\.?\s*([0-9][0-9A-Za-z._+-]{0,31})"),
)

_PRODUCT_EXPLICIT = re.compile(r"\bproduit\s*(?:démo\s+)?[:=]\s*([^\n,;:!?()\[\]{}]{2,64})", re.IGNORECASE)

# Négation ou incertitude immédiatement avant la déclaration : « pas la version
# 10.9 », « ni en v10.9 », « peut-être la version 11 » — jamais promues en fait.
_NEGATION_RE = re.compile(
    r"(?:^|[^0-9A-Za-zÀ-ÿ])(pas|jamais|ni|sans|aucun\w*|non|peut[- ]être|probablement|"
    r"sans\s+doute|je\s+pense|j'imagine|il\s+se\s+peut|hypothèse)\s+"
    r"(?:que\s+|c'est\s+|cest\s+|la\s+|le\s+|en\s+|dans\s+la\s+|vers\s+la\s+|de\s+la\s+)?$",
    re.IGNORECASE,
)

# Marqueurs d'une correction EXPLICITE du champ courant par l'utilisateur.
_CORRECTION_MARKERS = (
    "en fait",
    "plutôt",
    "je me suis trompé",
    "me suis trompé",
    "erreur de ma part",
    "je rectifie",
    "en réalité",
    "plus exactement",
)

_TRAILING = " \t\r\n.,;:!?»\"'"

# Doute explicite portant sur la VERSION (« je ne sais pas si ... version X »)
# ou sur l'ASSERTION entière (« pas sûr/pas certain ... Aster 10.9 »).
# En cas de doute : aucune promotion en fait certain, aucun filtre.
_DOUBT_VERSION_RE = re.compile(
    r"je\s+ne\s+sais\s+pas\s+si|je\s+ne\s+sais\s+pas\s+quel(?:le)?", re.IGNORECASE
)
_DOUBT_ASSERTION_RE = re.compile(
    r"je\s+ne\s+suis\s+pas\s+s[ûu]r|je\s+ne\s+suis\s+pas\s+certain|pas\s+s[ûu]r|pas\s+certain"
    r"|je\s+doute|doute\s+que|incertain\w*",
    re.IGNORECASE,
)

# Alternative explicite entre deux valeurs SANS répétition du mot « version »
# (« version 10.9 ou 10.10 », « Aster 10.9 ou 10.10 ») : l'ambiguïté ne
# choisit jamais la première — aucune des deux n'est promue.
_ALTERNATIVE_CLUSTER_RE = re.compile(
    r"(?:version\s*[:=]?\s*|v\.?\s*)?[0-9][0-9A-Za-z._+-]{0,31}\s*(?:,|;)?\s*(?:ou|or)\s*"
    r"(?:la\s+|le\s+|une?\s+)?(?:version\s*[:=]?\s*|v\.?\s*)?[0-9][0-9A-Za-z._+-]{0,31}",
    re.IGNORECASE,
)

# Fragment de version accolé à une déclaration produit (« Aster version 10.10 »).
_PRODUCT_VERSION_FRAGMENT_RE = re.compile(
    r"\s+(?:version|v\.?|n[o°])\s*[:=]?\s*[0-9][0-9A-Za-z._+-]*\s*$", re.IGNORECASE
)


def _doubted(text: str, start: int, pattern: "re.Pattern[str]") -> bool:
    """Doute explicite dans la même phrase, immédiatement avant la déclaration.

    Conservateur : le marqueur doit être présent dans la fenêtre précédente,
    sans ponctuation de fin de phrase entre lui et la déclaration."""
    window = text[max(0, start - 64) : start]
    match = None
    for match in pattern.finditer(window):
        pass
    if match is None:
        return False
    return re.search(r"[.!?;:]", window[match.end() :]) is None


def _ambiguous_alternative_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _ALTERNATIVE_CLUSTER_RE.finditer(text)]


def _normalize_product_candidate(value: str, known_products: Iterable[str]) -> str:
    """« Aster version 10.10 » → produit CONNU exact « Aster », jamais un
    produit composite inexistant."""
    normalized = " ".join(value.split()).strip(_TRAILING)
    normalized = _PRODUCT_VERSION_FRAGMENT_RE.sub("", normalized).strip(_TRAILING)
    for known in known_products:
        cleaned = str(known).strip()
        if cleaned and normalized.lower() == cleaned.lower():
            return cleaned
    return normalized


def is_explicit_correction(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _CORRECTION_MARKERS)


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - 32) : start]
    return _NEGATION_RE.search(window) is not None


def _valid_version(candidate: str) -> str | None:
    value = candidate.strip(_TRAILING)
    if not value:
        return None
    try:
        return validate_version(value)
    except ValueError:
        return None


def _product_matches(text: str, known_products: Iterable[str]) -> list[tuple[str, int, int]]:
    """Occurrences des produits connus (position de fin incluse)."""
    found: list[tuple[str, int, int]] = []
    for product in known_products:
        cleaned = str(product).strip()
        if len(cleaned) < 2:
            continue
        pattern = re.compile(r"(?<![0-9A-Za-zÀ-ÿ])" + re.escape(cleaned) + r"(?![0-9A-Za-zÀ-ÿ])", re.IGNORECASE)
        for match in pattern.finditer(text):
            found.append((cleaned, match.start(), match.end()))
    found.sort(key=lambda item: item[1])
    return found


def _extract_versions(text: str, known_products: Iterable[str]) -> list[str]:
    candidates: list[tuple[str, bool]] = []  # (valeur, écartée)
    alternative_spans = _ambiguous_alternative_spans(text)

    def collect(value: str, start: int) -> None:
        in_alternative = any(span_start <= start < span_end for span_start, span_end in alternative_spans)
        doubted = _doubted(text, start, _DOUBT_VERSION_RE) or _doubted(text, start, _DOUBT_ASSERTION_RE)
        candidates.append((value, _negated(text, start) or doubted or in_alternative))

    for pattern in _VERSION_PATTERNS:
        for match in pattern.finditer(text):
            value = _valid_version(match.group(1))
            if value:
                # Fenêtre de négation arrêtée au DÉBUT de la déclaration
                # (« version » inclus dans le motif), pas au chiffre.
                collect(value, match.start())

    # Version collée à un produit CONNU (« Aster 10.10 ») : explicitement
    # déclarative, conservatrice car le produit doit être connu du corpus.
    for _product, _start, end in _product_matches(text, known_products):
        tail = text[end : end + 48]
        match = re.match(r"\s*(?:version\s*)?[:=]?\s*([0-9][0-9A-Za-z._+-]{0,31})", tail)
        if not match:
            continue
        value = _valid_version(match.group(1))
        if value:
            # Position du CHIFFRE (groupe 1), pas de l'espace de tête : c'est
            # elle qui doit tomber dans une éventuelle alternative « ou ».
            collect(value, end + match.start(1))

    # Une négation, un doute explicite ou une alternative ne sont jamais
    # promus en fait ; deux valeurs distinctes = ambigu → aucune conservée.
    declared = [value for value, discarded in candidates if not discarded]
    distinct: list[str] = []
    for value in declared:
        if value not in distinct:
            distinct.append(value)
    if len(distinct) != 1:
        return []
    return distinct


def _extract_products(text: str, known_products: Iterable[str]) -> list[str]:
    products: list[str] = []

    def add(value: str) -> None:
        if 2 <= len(value) <= 64 and value not in products:
            products.append(value)

    for match in _PRODUCT_EXPLICIT.finditer(text):
        candidate = _normalize_product_candidate(match.group(1), known_products)
        add(candidate)

    # Produits connus du corpus réellement mentionnés : plusieurs produits
    # distincts dans un même message = ambigu → aucun conservé ; une mention
    # sous incertitude explicite (« pas sûr ... Aster 10.9 ») n'est jamais
    # promue en fait certain.
    mentions = _product_matches(text, known_products)
    distinct_known: list[str] = []
    for product, start, _end in mentions:
        if _doubted(text, start, _DOUBT_ASSERTION_RE):
            continue
        if product not in distinct_known:
            distinct_known.append(product)
    if len(distinct_known) == 1:
        add(distinct_known[0])
    elif len(distinct_known) > 1:
        # Ambigu : on ne conserve que les déclarations explicites « produit : X ».
        pass
    return products


def extract_explicit_declarations(text: str, known_products: Iterable[str] = ()) -> dict[str, list[str]]:
    """Retourne {'product': [...], 'version': [...]} — valeurs explicites validées."""
    return {
        "product": _extract_products(text, known_products),
        "version": _extract_versions(text, known_products),
    }
