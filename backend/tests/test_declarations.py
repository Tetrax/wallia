"""Extraction des déclarations explicites produit/version (conservatrice).

Uniquement les formulations explicites ; jamais de déduction d'une version
inconnue, jamais de promotion d'hypothèse ou de contrôle proposé.
"""
from __future__ import annotations

from app.declarations import extract_explicit_declarations


def test_version_keyword_forms_are_extracted():
    assert extract_explicit_declarations("La version 10.10 est installée.")["version"] == ["10.10"]
    assert extract_explicit_declarations("firmware v10.10.2 déployé")["version"] == ["10.10.2"]
    assert extract_explicit_declarations("version: 10.9")["version"] == ["10.9"]


def test_product_explicit_form_is_extracted():
    assert extract_explicit_declarations("produit : Aster")["product"] == ["Aster"]
    assert extract_explicit_declarations("Produit= Boreal")["product"] == ["Boreal"]


def test_speculative_or_unknown_versions_are_not_extracted():
    found = extract_explicit_declarations("Je pense que c'est la 11 ou la 12, pas sûr.")
    assert found["version"] == []
    found = extract_explicit_declarations("peut-être une version récente, je ne sais pas")
    assert found["version"] == []
    # Forme invalide après mot-clé : rejetée par le validateur, jamais conservée.
    found = extract_explicit_declarations("version la plus récente !!")
    assert found["version"] == []


def test_no_false_positive_on_plain_words():
    found = extract_explicit_declarations("Veuillez vérifier le volume et la vitesse du ventilateur.")
    assert found == {"product": [], "version": []}


def test_known_product_with_adjacent_version_is_extracted():
    """Scénario réel : « Sur le produit démo Aster 10.10, ... »."""
    found = extract_explicit_declarations(
        "Sur le produit démo Aster 10.10, la rotation des journaux semble courte.",
        known_products=["Aster"],
    )
    assert found["product"] == ["Aster"]
    assert found["version"] == ["10.10"]


def test_capitalized_version_keyword_is_extracted():
    assert extract_explicit_declarations("Version 10.11 installée hier.")["version"] == ["10.11"]


def test_negation_uncertainty_and_alternatives_are_never_confirmed():
    assert extract_explicit_declarations("Je ne suis pas en version 10.9.")["version"] == []
    assert extract_explicit_declarations("Ce n'est pas la version 10.9 qui est installée.")["version"] == []
    assert extract_explicit_declarations("C'est peut-être la version 11.")["version"] == []
    assert extract_explicit_declarations("Probablement la version 12 ici.")["version"] == []
    # Alternative : deux versions distinctes → ambigu, aucune promue.
    assert extract_explicit_declarations("La version 10.9 ou la version 10.10 ?")["version"] == []
    # Une seule version non ambiguë reste extraite.
    assert extract_explicit_declarations("La version 10.10 est installée, pas la 10.9.")["version"] == ["10.10"]


def test_unknown_products_are_not_promoted():
    found = extract_explicit_declarations("Sur le produit démo Aster 10.10.", known_products=[])
    assert found["product"] == []
    assert found["version"] == []  # « Aster » inconnu : aucune version collée promue


def test_explicit_correction_markers():
    from app.declarations import is_explicit_correction

    assert is_explicit_correction("En fait c'est la version 10.11.")
    assert is_explicit_correction("Je me suis trompé de version.")
    assert not is_explicit_correction("La version 10.10 est installée.")


# ---------------------------------------------------------------------------
# Régressions reproduites sur le module réel (revue Astra) — jamais de
# promotion d'une incertitude, jamais de produit composite.
# ---------------------------------------------------------------------------
def test_bare_version_alternative_without_repeated_keyword_is_not_promoted():
    """« version 10.9 ou 10.10 » (mot « version » non répété) : l'ambiguïté
    ne choisit JAMAIS la première — le produit certain reste, la version non."""
    found = extract_explicit_declarations(
        "Je ne sais pas si mon Aster est en version 10.9 ou 10.10.",
        known_products=["Aster", "Boreas"],
    )
    assert found["version"] == [], f"version ambiguë promue : {found['version']!r}"
    assert found["product"] == ["Aster"]

    # Variantes de casse/ponctuation : même invariants.
    assert extract_explicit_declarations("Version 10.9 OU 10.10 ?", known_products=["Aster"])["version"] == []
    assert (
        extract_explicit_declarations("Aster version 10.9 ou 10.10 ?", known_products=["Aster"])["version"] == []
    )
    # La forme à mot répété (déjà correcte) reste vide.
    assert extract_explicit_declarations("Aster version 10.9 ou version 10.10 ?", known_products=["Aster"])["version"] == []


def test_uncertain_product_version_pair_is_never_promoted():
    """« je ne suis pas sur Aster 10.9 » : ni produit ni version en fait
    certain (négation/incertitude du produit ET de la version)."""
    found = extract_explicit_declarations("Je ne suis pas sur Aster 10.9.", known_products=["Aster"])
    assert found["product"] == [], f"produit incertain promu : {found['product']!r}"
    assert found["version"] == [], f"version incertaine promue : {found['version']!r}"

    # Variantes de casse/orthographe : mêmes invariants.
    assert extract_explicit_declarations("JE NE SUIS PAS SUR ASTER 10.9", known_products=["Aster"]) == {
        "product": [],
        "version": [],
    }
    assert extract_explicit_declarations(
        "Je ne suis pas sûr que ce soit la version 10.9.", known_products=["Aster"]
    )["version"] == []


def test_explicit_product_plus_version_is_not_a_composite_product():
    """« Produit : Aster version 10.10 » → produit CONNU exact « Aster » +
    version 10.10 — jamais un produit composite inexistant."""
    found = extract_explicit_declarations(
        "Produit : Aster version 10.10 ; rotation du journal ?", known_products=["Aster"]
    )
    assert found["product"] == ["Aster"], f"produit composite : {found['product']!r}"
    assert found["version"] == ["10.10"]

    # Produit inconnu : la déclaration explicite reste conservée, sans version.
    found = extract_explicit_declarations("Produit : Nebula version 3.1.", known_products=["Aster"])
    assert found["product"] == ["Nebula"]
    assert found["version"] == ["3.1"]


def test_positive_declarations_and_corrections_still_work():
    """Aucune régression des positifs : déclarations explicites, corrections,
    versions inconnues valides."""
    assert extract_explicit_declarations("La version 10.10 est installée.")["version"] == ["10.10"]
    assert extract_explicit_declarations("Sur l'Aster 10.10, le voyant clignote.", known_products=["Aster"]) == {
        "product": ["Aster"],
        "version": ["10.10"],
    }
    assert extract_explicit_declarations("En fait, c'est la version 10.11.")["version"] == ["10.11"]
    assert extract_explicit_declarations("La version 99.99 est installée.")["version"] == ["99.99"]
