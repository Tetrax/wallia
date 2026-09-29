"""Recherche web complémentaire — barrière d'activation, validation stricte,
repli borné, endpoint et BORNE RÉELLE du processus de recherche.

Aucun réseau réel :
- le client HTTP visé est un double local (`FirecrawlDouble`) ;
- la résolution DNS des URL de résultat est remplacée par un double
  (`dns_public` / monkeypatch de `app.web._result_host_addresses`) ;
- le contexte du processus de recherche est forcé à `fork` (Linux) par la
  fixture autouse `web_mp_fork` : l'enfant hérite des doubles monkeypatchés.
  Aucune option de production ne permet ce changement (le module reste sur
  `spawn`) ; les tests du VRAI chemin spawn utilisent uniquement des tâches
  inoffensives SANS réseau (clé vide ⇒ refus immédiat dans l'enfant).

La borne est MESURÉE, jamais supposée : les tests de blocage (DNS bloqué,
corps qui stagne) vérifient la durée de retour réelle et l'absence de
sous-processus survivant — pas seulement la présence d'une exception.

NON EXÉCUTÉ à l'écriture de ce lot : le principal exécute la suite après revue.
"""
from __future__ import annotations

import dataclasses
import json
import multiprocessing
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import app.web as web
from tests.conftest import login

PUBLIC_IP = "93.184.216.34"
ALLOWED_SOURCE_URL = "https://www.wallix.com/fr/produits/wallix-bastion"


def _item(**overrides):
    item = {
        "url": ALLOWED_SOURCE_URL,
        "title": "WALLIX Bastion — documentation publique",
        "description": "Extrait public du constructeur (non qualifié).",
    }
    item.update(overrides)
    return item


def _payload_ok(items=None):
    # Le marqueur prouve qu'aucun champ fournisseur non validé n'est recopié.
    return {
        "success": True,
        "debug": "DOUBLE-BODY-MARKER",
        "data": {"web": items if items is not None else [_item()]},
    }


def _surviving_web_search_children() -> list:
    """Enfants de recherche encore vivants — doit TOUJOURS être vide après un
    appel (borne de terminaison incluse)."""
    return [child for child in multiprocessing.active_children() if child.name == "wallia-web-search"]


class FirecrawlDouble:
    """Double HTTP local de l'API Firecrawl v2 — aucun réseau réel.

    Modes : ok, redirect (302 avec corps valide), http_error (500 + en-tête et
    corps marqués), malformed (JSON illisible), success_false, not_list
    (data.web non-liste), huge (corps > 256 Kio), slow (corps au-delà de
    l'échéance), stall (corps qui ne repart JAMAIS : stagnation réelle).
    """

    def __init__(self) -> None:
        self.mode = "ok"
        self.items: list[dict] | None = None
        self.requests = 0
        self.path: str | None = None
        self.auth_header: str | None = None
        self.payload: dict | None = None
        self.base_url = ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        double = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence
                return

            def _send(self, status_code: int, body: bytes, headers: dict[str, str] | None = None) -> None:
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self) -> None:  # noqa: N802 - API BaseHTTPRequestHandler
                double.requests += 1
                double.path = self.path
                double.auth_header = self.headers.get("Authorization")
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    double.payload = json.loads(raw.decode("utf-8"))
                except ValueError:
                    double.payload = None
                mode = double.mode
                if mode == "redirect":
                    body = json.dumps(_payload_ok(double.items)).encode("utf-8")
                    self._send(302, body, {"Location": "https://evil.invalid/v2/search"})
                    return
                if mode == "http_error":
                    body = b'{"error": "CORPS-FOURNISSEUR-A-NE-PAS-FUITER", "token": "sk-double-secret"}'
                    self._send(500, body, {"X-Double-Debug": "ENTETE-DOUBLE-A-NE-PAS-FUITER"})
                    return
                if mode == "malformed":
                    self._send(200, b"<html>pas du json</html>")
                    return
                if mode == "success_false":
                    body = json.dumps(
                        {"success": False, "data": {"web": [_item()]}}
                    ).encode("utf-8")
                    self._send(200, body)
                    return
                if mode == "not_list":
                    body = json.dumps(
                        {"success": True, "data": {"web": {"url": ALLOWED_SOURCE_URL}}}
                    ).encode("utf-8")
                    self._send(200, body)
                    return
                if mode == "huge":
                    filler = "x" * (web.WEB_RESPONSE_MAX_BYTES + 1024)
                    body = json.dumps(
                        {"success": True, "data": {"web": [_item(description=filler)]}}
                    ).encode("utf-8")
                    self._send(200, body)
                    return
                if mode == "slow":
                    body = json.dumps(_payload_ok(double.items)).encode("utf-8")
                    half = len(body) // 2
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body[:half])
                        self.wfile.flush()
                        time.sleep(0.4)
                        self.wfile.write(body[half:])
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if mode == "stall":
                    # Le corps ne repart JAMAIS : la lecture du client reste
                    # bloquée au-delà de tout budget court — seule la borne du
                    # processus parent peut terminer l'opération.
                    body = json.dumps(_payload_ok(double.items)).encode("utf-8")
                    half = len(body) // 2
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body[:half])
                        self.wfile.flush()
                        time.sleep(15)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                self._send(200, json.dumps(_payload_ok(double.items)).encode("utf-8"))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="wallia-firecrawl-double"
        )
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture()
def firecrawl_double(monkeypatch):
    double = FirecrawlDouble()
    double.start()
    monkeypatch.setattr(web, "FIRECRAWL_ENDPOINT", f"{double.base_url}/v2/search")
    try:
        yield double
    finally:
        double.stop()


@pytest.fixture()
def dns_public(monkeypatch):
    """Double de résolution : toute adresse demandée est publique."""
    monkeypatch.setattr(web, "_result_host_addresses", lambda host: [PUBLIC_IP])


@pytest.fixture(autouse=True)
def web_mp_fork(monkeypatch):
    """Le contexte PRIVÉ du processus de recherche passe à `fork` (Linux) pour
    les tests à doubles : l'enfant hérite des monkeypatches (HTTP local + DNS)
    — JAMAIS du vrai réseau. Aucune option d'environnement ni d'API ne permet
    ce changement en production (le module reste sur `spawn`) ; les tests du
    vrai chemin spawn forcent explicitement `spawn` sur des tâches inoffensives.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("fork requis pour hériter les doubles de test (Linux uniquement)")
    monkeypatch.setattr(web, "_WEB_SEARCH_MP_METHOD", "fork")


@pytest.fixture()
def firecrawl_key(settings):
    path = settings.secret_path("firecrawl_api_key")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("cle-firecrawl-factice-pour-tests\n", encoding="utf-8")
    try:
        yield "cle-firecrawl-factice-pour-tests"
    finally:
        if path.exists():
            path.unlink()


@pytest.fixture()
def web_env_enabled(monkeypatch):
    """Active le drapeau d'environnement pour la durée du test (restauré après)."""
    from app.config import get_settings

    monkeypatch.setenv("WALLIA_WEB_ENABLED", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _activate_web_operator(*, activated: bool = True, rag_validated: bool = True) -> None:
    from app.app_settings import set_row
    from app.db import session_scope

    with session_scope() as db:
        set_row(db, "web", {"activated": activated, "rag_validated": rag_validated})


@pytest.fixture()
def db_session(migrated):
    from app.db import session_scope

    with session_scope() as session:
        yield session


# ---------------------------------------------------------------------------
# Construction de la requête : vocabulaire fermé, version stricte
# ---------------------------------------------------------------------------


def test_public_query_uses_closed_vocabulary_only():
    query = web.build_public_query(
        "Quel est le token AKIA-FACTICE-0000 et le nom interne projet-falcon ? "
        "voyant du Bastion sur journal de rotation",
        None,
        None,
    )
    assert query is not None
    assert query.startswith("site:wallix.com")
    for expected in ("voyant", "journal", "rotation"):
        assert expected in query
    lowered = query.lower()
    # Aucun jeton brut, nom interne ou secret factice ne sort dans la requête.
    for forbidden in ("token", "akia", "factice", "falcon", "interne"):
        assert forbidden not in lowered
    assert len(query) <= web.WEB_QUERY_MAX_CHARS


def test_public_query_bounds_theme_count():
    query = web.build_public_query(
        "voyant journal rotation certificat session mfa audit erreur", None, None
    )
    assert query is not None
    themes = query.split(" ")[1:]
    assert len(themes) == 4
    assert themes == ["voyant", "journal", "rotation", "certificat"]


def test_public_query_maps_closed_products_and_ignores_free_values():
    wallix = web.build_public_query("journal", "wallix", None)
    assert wallix is not None and "WALLIX" in wallix
    bastion = web.build_public_query("journal", "Bastion", None)
    assert bastion is not None and "Bastion" in bastion
    access = web.build_public_query("journal", "access manager", None)
    assert access is not None and "Access Manager" in access
    # Valeur produit libre (même un nom interne) : jamais transmise.
    free = web.build_public_query("journal", "Aster-interne-référence-42", None)
    assert free is not None
    assert "Aster" not in free
    assert "interne" not in free


def test_public_query_is_none_without_public_terms():
    assert web.build_public_query("bonjour, quel est le problème ?", None, None) is None
    assert web.build_public_query("", None, None) is None
    # Une version valide seule reste une demande publique explicite.
    version_only = web.build_public_query("bonjour", None, "10.10")
    assert version_only is not None and "10.10" in version_only


@pytest.mark.parametrize("version", ["10.10", "12.0.1", "2026.09.29.1", "1.2.3.4"])
def test_public_query_accepts_strict_numeric_versions(version):
    query = web.build_public_query("journal", None, version)
    assert query is not None
    assert version in query


@pytest.mark.parametrize(
    "version",
    [
        "10.10-secret",  # suffixe après le numéro
        "10.10_beta",  # underscore
        "10",  # un seul composant
        "v10.10",  # préfixe texte
        " 10.10",  # strip permissif interdit
        "10.10 ",  # idem
        "10.10e5",  # exposant
        "10..10",  # composant vide
        "10.10.10.10.10",  # trop de composants
        "١٠.١٠",  # chiffres Unicode
        "１０.１０",  # chiffres pleine largeur
        "10.12345",  # composant trop long
        "10,10",  # virgule
        "PROJET-10.10",  # jeton mêlé
    ],
)
def test_public_query_omits_invalid_versions(version):
    query = web.build_public_query("journal", None, version)
    assert query is not None  # le thème public reste utilisable
    assert version not in query
    assert "10" not in query  # aucun fragment du numéro invalide ne fuit


# ---------------------------------------------------------------------------
# Validation d'URL : HTTPS public uniquement, formes déceptives refusées
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://wallix.com/",
        "https://www.wallix.com/fr/produits/",
        "https://wallix.com:443/fr/",
        "https://WALLIX.com/fr/",
        "https://support.wallix.com/faq?lang=fr",
        "https://fr.support.wallix.com/faq?lang=fr",
    ],
)
def test_allowed_public_url_accepts_only_public_wallix_https(url, dns_public):
    assert web._allowed_public_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://wallix.com/fr/",  # pas HTTPS
        "https://user:motdepasse@wallix.com/",  # info utilisateur
        "https://user@wallix.com/",  # idem
        "https://@wallix.com/",  # userinfo VIDE : refusé aussi (autorité brute)
        "https://wallix.com:8443/",  # port hors 443
        "https://wallix.com/fr/#ancre",  # fragment
        "https://wallix.com\\@evil.invalid/",  # backslash déceptif
        "https://***@evil.invalid/",  # userinfo déceptif (autorité brute)
        "https://wallix.com/fr/\x01",  # caractère de contrôle
        "https://evilwallix.com/",  # suffixe trompeur
        "https://wallix.com.evil.invalid/",  # sous-domaine trompeur
        "https://wallix.com./fr/",  # point final
        "https://wallix..com/",  # label vide au milieu
        "https://wallix.cóm/",  # domaine non ASCII
        "https://wallix。com/",  # point idéographique (jamais normalisé)
        "https://wallix%2Ecom/",  # percent-encoding du host
        "https://wall ix.com/",  # espace brut dans l'autorité
        "https://wallix_com.fr/",  # souligné
        "https://-wallix.com/",  # label commençant par un tiret
        "https://wallix-.com/",  # label finissant par un tiret
        "https://" + "a" * 64 + ".wallix.com/",  # label > 63
        "https://" + ".".join(["a" * 60] * 4) + ".wallix.com/",  # host > 253
        "https://127.0.0.1/",  # host littéral
        "ftp://wallix.com/",  # schéma
        "https:///fr/",  # host vide
    ],
)
def test_allowed_public_url_refuses_deceptive_forms(url, dns_public):
    assert web._allowed_public_url(url) is False


@pytest.mark.parametrize(
    "addresses",
    [
        ["192.168.1.10"],  # privé
        ["10.0.0.1"],
        ["127.0.0.1"],  # loopback
        ["169.254.169.254"],  # link-local (métadonnées cloud)
        ["0.0.0.0"],  # non spécifiée
        ["224.0.0.1"],  # multicast (is_global l'accepte encore sur 3.12)
        ["255.255.255.255"],  # réservé
        ["::1"],  # loopback IPv6
        ["fe80::1"],  # link-local IPv6
        ["fd00::abcd"],  # unique local IPv6
        ["ff02::1"],  # multicast IPv6
        [PUBLIC_IP, "10.0.0.1"],  # une seule adresse privée suffit à refuser
        [PUBLIC_IP, "224.0.0.1"],  # mélange global/multicast : refusé aussi
    ],
)
def test_allowed_public_url_refuses_non_global_resolution(monkeypatch, addresses):
    monkeypatch.setattr(web, "_result_host_addresses", lambda host: list(addresses))
    assert web._allowed_public_url("https://www.wallix.com/") is False


def test_allowed_public_url_refuses_resolution_failure(monkeypatch):
    def _boom(host):
        raise socket.gaierror("échec DNS simulé")

    monkeypatch.setattr(web, "_result_host_addresses", _boom)
    assert web._allowed_public_url("https://www.wallix.com/") is False


# ---------------------------------------------------------------------------
# Appel moteur : 200/success/data.web stricts, bornes, erreurs sûres
# ---------------------------------------------------------------------------


def test_search_accepts_valid_v2_payload_and_bounds_results(
    settings, firecrawl_key, firecrawl_double, dns_public
):
    firecrawl_double.items = [
        _item(url=f"https://www.wallix.com/page-{index}") for index in range(10)
    ]
    result = web.search_web_public("site:wallix.com journal", settings)
    assert result["query"] == "site:wallix.com journal"
    assert len(result["results"]) == web.WEB_MAX_RESULTS == 3
    assert all(item["url"].startswith("https://www.wallix.com/") for item in result["results"])
    # Le jeton d'authentification est envoyé au service, jamais exposé ni logué.
    assert firecrawl_double.auth_header == f"Bearer {firecrawl_key}"
    assert firecrawl_double.path == "/v2/search"
    sent = json.dumps(firecrawl_double.payload, ensure_ascii=False)
    assert firecrawl_key not in sent
    assert "DOUBLE-BODY-MARKER" not in json.dumps(result)


def test_search_filters_unacceptable_urls_and_item_types(
    settings, firecrawl_key, firecrawl_double, dns_public
):
    firecrawl_double.items = [
        _item(url="http://www.wallix.com/"),  # non HTTPS
        _item(url="https://user@www.wallix.com/"),  # userinfo
        _item(url="https://wallix.com:8443/"),  # port
        _item(url="https://www.wallix.com.evil.invalid/"),  # trompeur
        _item(url=ALLOWED_SOURCE_URL, title=123),  # type titre
        _item(url=ALLOWED_SOURCE_URL, description={"x": 1}),  # type extrait
        _item(url=ALLOWED_SOURCE_URL, title="Valide", description="Extrait valide"),
    ]
    result = web.search_web_public("site:wallix.com journal", settings)
    assert [item["title"] for item in result["results"]] == ["Valide"]
    assert [item["snippet"] for item in result["results"]] == ["Extrait valide"]


def test_search_examines_at_most_twelve_candidates(
    settings, firecrawl_key, firecrawl_double, dns_public
):
    """Le traitement est borné : au plus 12 candidats EXAMINÉS (pas seulement
    3 résultats). Un item valide au-delà du 12e rang n'est jamais examiné."""
    rejected = [_item(url="https://wallix.com.evil.invalid/") for _ in range(12)]
    firecrawl_double.items = rejected + [
        _item(url=f"https://www.wallix.com/page-{index}") for index in range(5)
    ]
    assert web.search_web_public("site:wallix.com journal", settings)["results"] == []
    firecrawl_double.items = rejected[:11] + [_item(url="https://www.wallix.com/ok")]
    result = web.search_web_public("site:wallix.com journal", settings)
    assert [item["url"] for item in result["results"]] == ["https://www.wallix.com/ok"]
    firecrawl_double.items = rejected[:12] + [_item(url="https://www.wallix.com/trop-loin")]
    assert web.search_web_public("site:wallix.com journal", settings)["results"] == []


def test_search_bounds_titles_and_snippets(settings, firecrawl_key, firecrawl_double, dns_public):
    firecrawl_double.items = [_item(title="T" * 500, description="D" * 900)]
    result = web.search_web_public("site:wallix.com journal", settings)
    item = result["results"][0]
    assert len(item["title"]) == web.WEB_TITLE_MAX_CHARS == 200
    assert len(item["snippet"]) == web.WEB_SNIPPET_MAX_CHARS == 400


def test_search_refuses_redirect_even_with_valid_body(settings, firecrawl_key, firecrawl_double):
    firecrawl_double.mode = "redirect"
    with pytest.raises(web.WebUnavailable) as excinfo:
        web.search_web_public("site:wallix.com journal", settings)
    assert "302" in str(excinfo.value)


def test_search_http_error_is_safe_and_named(settings, firecrawl_key, firecrawl_double):
    firecrawl_double.mode = "http_error"
    with pytest.raises(web.WebUnavailable) as excinfo:
        web.search_web_public("site:wallix.com journal", settings)
    message = str(excinfo.value)
    assert message == "service HTTP 500"
    for marker in ("CORPS-FOURNISSEUR", "ENTETE-DOUBLE", "sk-double-secret", firecrawl_key):
        assert marker not in message


@pytest.mark.parametrize(
    ("mode", "fragment"),
    [
        ("malformed", "illisible"),
        ("success_false", "non conforme"),
        ("not_list", "non conforme"),
    ],
)
def test_search_refuses_invalid_json_shapes(
    settings, firecrawl_key, firecrawl_double, mode, fragment
):
    firecrawl_double.mode = mode
    with pytest.raises(web.WebUnavailable) as excinfo:
        web.search_web_public("site:wallix.com journal", settings)
    assert fragment in str(excinfo.value)
    # Jamais le corps fournisseur dans le message d'erreur.
    assert "DOUBLE-BODY-MARKER" not in str(excinfo.value)


def test_search_refuses_oversized_response(settings, firecrawl_key, firecrawl_double):
    firecrawl_double.mode = "huge"
    with pytest.raises(web.WebUnavailable) as excinfo:
        web.search_web_public("site:wallix.com journal", settings)
    assert "volumineuse" in str(excinfo.value)


def test_search_deadline_is_bounded_and_measured(
    settings, firecrawl_key, firecrawl_double, monkeypatch
):
    """La borne est RÉELLE : la durée de retour est mesurée, jamais une simple
    exception après une longue attente. Le corps arrive après l'échéance du
    chercheur : le retour doit être borné par le budget court du test."""
    monkeypatch.setattr(web, "WEB_TOTAL_DEADLINE_S", 1.2)
    monkeypatch.setattr(web, "WEB_CHILD_DEADLINE_S", 0.3)
    monkeypatch.setattr(web, "WEB_CHILD_WAIT_S", 0.6)
    monkeypatch.setattr(web, "WEB_CHILD_KILL_GRACE_S", 0.3)
    firecrawl_double.mode = "slow"
    started = time.monotonic()
    with pytest.raises(web.WebUnavailable) as excinfo:
        web.search_web_public("site:wallix.com journal", settings)
    elapsed = time.monotonic() - started
    assert "délai" in str(excinfo.value)
    assert elapsed < 2.0, f"retour non borné : {elapsed:.2f}s"
    assert _surviving_web_search_children() == []


def test_stalled_body_is_killed_within_budget(
    settings, firecrawl_key, firecrawl_double, monkeypatch
):
    """Un corps qui ne repart JAMAIS (stagnation réelle) : la lecture bloquée
    ne peut être arrêtée par la seule échéance interne — le parent termine
    l'enfant et le retour reste dans le budget, sans processus survivant."""
    monkeypatch.setattr(web, "WEB_TOTAL_DEADLINE_S", 1.2)
    monkeypatch.setattr(web, "WEB_CHILD_DEADLINE_S", 0.3)
    monkeypatch.setattr(web, "WEB_CHILD_WAIT_S", 0.6)
    monkeypatch.setattr(web, "WEB_CHILD_KILL_GRACE_S", 0.3)
    firecrawl_double.mode = "stall"
    started = time.monotonic()
    with pytest.raises(web.WebUnavailable) as excinfo:
        web.search_web_public("site:wallix.com journal", settings)
    elapsed = time.monotonic() - started
    assert "délai" in str(excinfo.value)
    assert elapsed < 2.0, f"retour non borné : {elapsed:.2f}s"
    assert _surviving_web_search_children() == []
    # Le slot est libéré : une opération inoffensive (clé vide, sans réseau)
    # repasse par le vrai chemin et échoue proprement.
    with pytest.raises(web.WebUnavailable) as second:
        web._search_web_public_bounded("site:wallix.com journal", "")
    assert "clé" in str(second.value)


def test_blocked_dns_resolution_is_bounded(
    settings, firecrawl_key, firecrawl_double, monkeypatch
):
    """Le DNS d'un résultat ne peut pas bloquer l'opération : résolution
    volontairement bloquée (double hérité par fork), le parent borne et
    termine l'enfant dans le budget."""
    def _blocked(host):
        time.sleep(60)
        return [PUBLIC_IP]

    monkeypatch.setattr(web, "_result_host_addresses", _blocked)
    monkeypatch.setattr(web, "WEB_TOTAL_DEADLINE_S", 1.2)
    monkeypatch.setattr(web, "WEB_CHILD_DEADLINE_S", 0.3)
    monkeypatch.setattr(web, "WEB_CHILD_WAIT_S", 0.6)
    monkeypatch.setattr(web, "WEB_CHILD_KILL_GRACE_S", 0.3)
    firecrawl_double.items = [_item()]
    started = time.monotonic()
    with pytest.raises(web.WebUnavailable) as excinfo:
        web.search_web_public("site:wallix.com journal", settings)
    elapsed = time.monotonic() - started
    assert "délai" in str(excinfo.value)
    assert elapsed < 2.0, f"retour non borné : {elapsed:.2f}s"
    assert _surviving_web_search_children() == []


def test_search_requires_key(settings, firecrawl_double):
    path = settings.secret_path("firecrawl_api_key")
    if path.exists():
        path.unlink()
    with pytest.raises(web.WebUnavailable) as excinfo:
        web.search_web_public("site:wallix.com journal", settings)
    assert "clé" in str(excinfo.value)
    assert firecrawl_double.requests == 0  # jamais d'appel sans clé


# ---------------------------------------------------------------------------
# Mécanique du processus de recherche : vrai spawn inoffensif, IPC, saturation
# ---------------------------------------------------------------------------


def test_real_spawn_path_on_harmless_offline_task(monkeypatch):
    """Le VRAI chemin spawn (contexte de production) est exercé sur une tâche
    inoffensive SANS réseau : clé vide ⇒ l'enfant refuse immédiatement sans
    aucune connexion, le pipe revient proprement, aucun processus ne survit."""
    monkeypatch.setattr(web, "_WEB_SEARCH_MP_METHOD", "spawn")
    started = time.monotonic()
    with pytest.raises(web.WebUnavailable) as excinfo:
        web._search_web_public_bounded("site:wallix.com journal", "")
    elapsed = time.monotonic() - started
    assert "clé" in str(excinfo.value)
    assert elapsed < 15.0, f"chemin spawn anormalement lent : {elapsed:.2f}s"
    assert _surviving_web_search_children() == []


def test_child_inputs_travel_as_process_args_not_argv(monkeypatch):
    """Les entrées du chercheur (requête publique + clé) sont transmises en
    ARGUMENTS du processus (pickle → IPC multiprocessing), jamais par la ligne
    de commande ni par stdout : le lancement est inspecté pendant un vrai
    spawn inoffensif (clé vide ⇒ aucun réseau)."""
    import multiprocessing as mp

    captured: list[tuple] = []
    real_get_context = mp.get_context

    def spy_get_context(method=None):
        context = real_get_context(method)
        real_process = context.Process

        def recording_process(*args, **kwargs):
            captured.append(tuple(kwargs.get("args") or ()))
            return real_process(*args, **kwargs)

        monkeypatch.setattr(context, "Process", recording_process)
        return context

    monkeypatch.setattr(mp, "get_context", spy_get_context)
    monkeypatch.setattr(web, "_WEB_SEARCH_MP_METHOD", "spawn")
    query = "site:wallix.com journal"
    with pytest.raises(web.WebUnavailable):
        web._search_web_public_bounded(query, "")
    assert captured, "aucun processus enfant enregistré"
    child_args = captured[0]
    assert child_args[0] == query and child_args[1] == ""
    # La ligne de commande du parent ne contient jamais la requête ni la clé.
    assert all(query not in part for part in sys.argv)
    assert _surviving_web_search_children() == []


def test_saturated_slots_refuse_immediately_and_recover(monkeypatch):
    """Deux opérations simultanées au plus : le troisième appel est refusé
    IMMÉDIATEMENT (pas de file d'attente) ; après libération, une opération
    inoffensive repasse par le vrai chemin."""
    monkeypatch.setattr(web, "_WEB_SEARCH_MP_METHOD", "spawn")
    assert web._WEB_SEARCH_SLOTS.acquire(blocking=False)
    assert web._WEB_SEARCH_SLOTS.acquire(blocking=False)
    try:
        started = time.monotonic()
        with pytest.raises(web.WebUnavailable) as excinfo:
            web._search_web_public_bounded("site:wallix.com journal", "")
        elapsed = time.monotonic() - started
        assert "saturé" in str(excinfo.value)
        assert elapsed < 1.0, "refus saturé non immédiat"
    finally:
        web._WEB_SEARCH_SLOTS.release()
        web._WEB_SEARCH_SLOTS.release()
    with pytest.raises(web.WebUnavailable) as excinfo:
        web._search_web_public_bounded("site:wallix.com journal", "")
    assert "clé" in str(excinfo.value)
    assert _surviving_web_search_children() == []


# ---------------------------------------------------------------------------
# Endpoint manuel : barrière effective AVANT recherche, auth/CSRF, bornes
# ---------------------------------------------------------------------------


def test_web_endpoints_require_authentication(client):
    assert client.get("/api/web/status").status_code == 401
    assert client.post("/api/web/search", json={"terms": ["voyant"]}).status_code == 401


def test_web_search_requires_csrf_even_when_active(
    client, admin, firecrawl_double, firecrawl_key, web_env_enabled
):
    _activate_web_operator()
    login(client, admin)
    refused = client.post("/api/web/search", json={"terms": ["voyant"]})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "jeton CSRF invalide"
    assert firecrawl_double.requests == 0


def test_web_search_refused_when_inactive_even_with_key(
    client, admin, firecrawl_double, firecrawl_key, dns_public
):
    """La clé seule ne suffit JAMAIS : sans activation effective, refus 403."""
    csrf = login(client, admin)
    response = client.post(
        "/api/web/search", json={"terms": ["voyant"]}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 403
    assert "désactivé" in response.json()["detail"]
    assert firecrawl_double.requests == 0


def test_web_search_refused_without_operator_activation(
    client, admin, firecrawl_double, firecrawl_key, web_env_enabled, dns_public
):
    csrf = login(client, admin)
    response = client.post(
        "/api/web/search", json={"terms": ["voyant"]}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 403
    assert "recette RAG" in response.json()["detail"]
    assert firecrawl_double.requests == 0


def test_web_search_refused_without_rag_validation(
    client, admin, firecrawl_double, firecrawl_key, web_env_enabled, dns_public
):
    _activate_web_operator(activated=True, rag_validated=False)
    csrf = login(client, admin)
    response = client.post(
        "/api/web/search", json={"terms": ["voyant"]}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 403
    assert "recette RAG" in response.json()["detail"]
    assert firecrawl_double.requests == 0


def test_web_search_refused_without_key(client, admin, firecrawl_double, web_env_enabled, settings, dns_public):
    _activate_web_operator()
    path = settings.secret_path("firecrawl_api_key")
    if path.exists():
        path.unlink()
    csrf = login(client, admin)
    response = client.post(
        "/api/web/search", json={"terms": ["voyant"]}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 403
    assert "clé" in response.json()["detail"]
    assert firecrawl_double.requests == 0


def test_web_search_returns_public_query_and_validated_sources_only(
    client, admin, firecrawl_double, firecrawl_key, web_env_enabled, dns_public
):
    _activate_web_operator()
    csrf = login(client, admin)
    firecrawl_double.items = [
        _item(url=ALLOWED_SOURCE_URL, title="Bastion public", description="Extrait public."),
        _item(url="https://wallix.com.evil.invalid/", title="Trompeur", description="Non retenu."),
        _item(url="https://user:pw@www.wallix.com/", title="Userinfo", description="Non retenu."),
    ]
    response = client.post(
        "/api/web/search",
        json={
            "product": "wallix",
            "version": "10.10-secret",  # invalide ⇒ omise
            "terms": ["AKIA-FACTICE-0000 nom interne", "voyant", "journal", "rotation"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["query"] == "site:wallix.com WALLIX voyant journal rotation"
    # Ni le texte brut, ni la version invalide, ni un champ fournisseur non validé.
    assert "AKIA" not in response.text
    assert "10.10-secret" not in response.text
    assert "DOUBLE-BODY-MARKER" not in response.text
    sent = json.dumps(firecrawl_double.payload, ensure_ascii=False)
    assert "AKIA" not in sent and "10.10-secret" not in sent and "nom interne" not in sent
    assert firecrawl_double.payload["query"] == payload["query"]
    assert firecrawl_double.auth_header == f"Bearer {firecrawl_key}"
    results = payload["results"]
    assert len(results) == 1
    source = results[0]
    assert source["source_type"] == "web"
    assert source["url"] == ALLOWED_SOURCE_URL
    assert source["domain"] == "www.wallix.com"
    assert source["version_state"] == "non_verifiee"
    for null_field in (
        "document_id",
        "chunk_id",
        "page_start",
        "page_end",
        "score",
        "score_vector",
        "score_text",
    ):
        assert source[null_field] is None


def test_web_search_accepts_valid_version(
    client, admin, firecrawl_double, firecrawl_key, web_env_enabled, dns_public
):
    _activate_web_operator()
    csrf = login(client, admin)
    response = client.post(
        "/api/web/search",
        json={"version": "12.0.1", "terms": ["voyant"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "12.0.1" in response.json()["query"]


def test_web_search_input_bounds_and_no_free_query(
    client, admin, firecrawl_double, firecrawl_key, web_env_enabled, dns_public
):
    _activate_web_operator()
    csrf = login(client, admin)
    too_many = client.post(
        "/api/web/search", json={"terms": ["voyant"] * 7}, headers={"X-CSRF-Token": csrf}
    )
    assert too_many.status_code == 422
    too_long = client.post(
        "/api/web/search", json={"terms": ["a" * 101, "voyant"]}, headers={"X-CSRF-Token": csrf}
    )
    assert too_long.status_code == 422
    # Aucune requête libre : un texte sans terme public ne génère aucun appel.
    no_public = client.post(
        "/api/web/search", json={"terms": ["bonjour le monde"]}, headers={"X-CSRF-Token": csrf}
    )
    assert no_public.status_code == 422
    assert firecrawl_double.requests == 0


def test_web_status_endpoint_reports_effective_state(
    client, admin, firecrawl_key, web_env_enabled, settings
):
    login(client, admin)
    pending = client.get("/api/web/status")
    assert pending.status_code == 200
    assert pending.json()["available"] is False
    assert "recette RAG" in pending.json()["reason"]
    _activate_web_operator()
    active = client.get("/api/web/status")
    assert active.status_code == 200
    assert active.json()["available"] is True
    assert active.json()["reason"] is None
    # Aucun secret dans l'état exposé.
    assert firecrawl_key not in json.dumps(active.json())


# ---------------------------------------------------------------------------
# Repli du chat (unitaire) : portes, statuts, indépendance du statut corpus
# ---------------------------------------------------------------------------


def test_fallback_disabled_when_env_flag_off(db_session, settings, firecrawl_double, firecrawl_key):
    patched = dataclasses.replace(settings, web_enabled=False)
    info = web.web_fallback_for_chat(
        db_session, patched, retrieval_status="empty_corpus", user_text="voyant journal", case_state={}
    )
    assert info["status"] == "disabled"
    assert info["sources"] == []
    assert firecrawl_double.requests == 0


def test_fallback_disabled_without_operator_activation(
    db_session, settings, firecrawl_double, firecrawl_key
):
    patched = dataclasses.replace(settings, web_enabled=True)
    info = web.web_fallback_for_chat(
        db_session, patched, retrieval_status="empty_corpus", user_text="voyant journal", case_state={}
    )
    assert info["status"] == "disabled"
    assert "recette RAG" in info["reason"]
    assert firecrawl_double.requests == 0


def test_fallback_disabled_without_rag_validation(
    db_session, settings, firecrawl_double, firecrawl_key
):
    _activate_web_operator(rag_validated=False)
    patched = dataclasses.replace(settings, web_enabled=True)
    info = web.web_fallback_for_chat(
        db_session, patched, retrieval_status="empty_corpus", user_text="voyant journal", case_state={}
    )
    assert info["status"] == "disabled"
    assert firecrawl_double.requests == 0


def test_fallback_disabled_without_key(db_session, settings, firecrawl_double):
    _activate_web_operator()
    path = settings.secret_path("firecrawl_api_key")
    if path.exists():
        path.unlink()
    patched = dataclasses.replace(settings, web_enabled=True)
    info = web.web_fallback_for_chat(
        db_session, patched, retrieval_status="empty_corpus", user_text="voyant journal", case_state={}
    )
    assert info["status"] == "disabled"
    assert "clé" in info["reason"]
    assert firecrawl_double.requests == 0


def test_fallback_never_replaces_corpus_ok_or_technical_failures(
    db_session, settings, firecrawl_double, firecrawl_key
):
    _activate_web_operator()
    patched = dataclasses.replace(settings, web_enabled=True)
    for status_value in ("ok", "retrieval_unavailable", "embeddings_unavailable"):
        info = web.web_fallback_for_chat(
            db_session, patched, retrieval_status=status_value, user_text="voyant journal", case_state={}
        )
        assert info["status"] == "not_needed", status_value
        assert info["sources"] == []
    assert firecrawl_double.requests == 0


def test_fallback_not_needed_without_public_terms(
    db_session, settings, firecrawl_double, firecrawl_key
):
    _activate_web_operator()
    patched = dataclasses.replace(settings, web_enabled=True)
    info = web.web_fallback_for_chat(
        db_session, patched, retrieval_status="empty_corpus", user_text="bonjour le monde", case_state={}
    )
    assert info["status"] == "not_needed"
    assert info["query"] is None
    assert firecrawl_double.requests == 0


def test_fallback_ok_sources_contract_and_validated_version(
    db_session, settings, firecrawl_double, firecrawl_key, dns_public
):
    _activate_web_operator()
    patched = dataclasses.replace(settings, web_enabled=True)
    firecrawl_double.items = [_item()]
    info = web.web_fallback_for_chat(
        db_session,
        patched,
        retrieval_status="empty_corpus",
        user_text="voyant journal",
        case_state={"version": "10.10", "product": "wallix"},
    )
    assert info["status"] == "ok"
    assert info["query"].startswith("site:wallix.com WALLIX 10.10")
    assert "voyant" in info["query"] and "journal" in info["query"]
    source = info["sources"][0]
    assert source["source_type"] == "web"
    assert source["url"] == ALLOWED_SOURCE_URL
    assert source["version_state"] == "non_verifiee"
    assert source["document_id"] is None and source["chunk_id"] is None
    assert source["score"] is None and source["page_start"] is None


def test_fallback_version_invalide_est_omise(
    db_session, settings, firecrawl_double, firecrawl_key, dns_public
):
    _activate_web_operator()
    patched = dataclasses.replace(settings, web_enabled=True)
    info = web.web_fallback_for_chat(
        db_session,
        patched,
        retrieval_status="empty_corpus",
        user_text="voyant",
        case_state={"version": "10.10-interne-token"},
    )
    assert info["status"] == "ok"
    assert "10.10-interne-token" not in info["query"]
    assert "interne" not in info["query"]


def test_fallback_no_results_when_nothing_acceptable(
    db_session, settings, firecrawl_double, firecrawl_key, dns_public
):
    _activate_web_operator()
    patched = dataclasses.replace(settings, web_enabled=True)
    firecrawl_double.items = [_item(url="https://wallix.com.evil.invalid/")]
    info = web.web_fallback_for_chat(
        db_session, patched, retrieval_status="empty_corpus", user_text="voyant", case_state={}
    )
    assert info["status"] == "no_results"
    assert info["sources"] == []


def test_fallback_unavailable_is_explicit_and_safe(
    db_session, settings, firecrawl_double, firecrawl_key
):
    _activate_web_operator()
    patched = dataclasses.replace(settings, web_enabled=True)
    firecrawl_double.mode = "http_error"
    info = web.web_fallback_for_chat(
        db_session, patched, retrieval_status="empty_corpus", user_text="voyant", case_state={}
    )
    assert info["status"] == "unavailable"
    assert info["reason"] == "service HTTP 500"
    assert "sk-double-secret" not in info["reason"]
    assert "CORPS-FOURNISSEUR" not in info["reason"]
    assert info["sources"] == []
