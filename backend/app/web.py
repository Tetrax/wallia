"""Recherche web complémentaire (Firecrawl v2) — intégration bornée au chat.

Règles (docs/delivery-contracts.md) :
- activation EFFECTIVE vérifiée par `web_status` AVANT tout appel : drapeau
  d'environnement + activation opérateur (après recette RAG) + clé réellement
  présente — une clé seule ne suffit JAMAIS ; l'endpoint manuel et le repli du
  chat passent par le même contrôle ;
- requête construite UNIQUEMENT à partir d'un vocabulaire fermé public
  (WALLIX / Bastion / Access Manager + thèmes techniques prédéfinis) et d'une
  version strictement numérique validée : jamais de texte brut de question, de
  log, de nom interne, de jeton ou de valeur produit libre ; aucune requête
  libre n'est exposée ;
- endpoint moteur FIXE (API v2 officielle `POST /v2/search`, réponse
  `data.web`), TLS vérifié, redirections désactivées, proxy d'environnement
  ignoré, limites de résultats, d'octets et délai total borné ;
- l'opération entière s'exécute dans un PROCESSUS enfant isolé (contexte
  `spawn` en production — jamais fork dans un serveur multithreadé) : le
  parent tient une borne wallclock DURE (attente bornée puis terminate/kill
  avec join borné), sans processus orphelin ni file d'attente illimitée ; la
  requête publique et la clé ne transitent que par l'IPC multiprocessing
  (jamais argv, stdout ni log) ; l'enfant n'accède ni à la base, ni à une
  session, ni à la configuration globale ;
- seules les URL HTTPS du domaine public validé sont conservées : autorité
  BRUTE contrôlée AVANT toute normalisation URL/IDNA (ASCII pur, sans
  userinfo même vide, sans percent-encoding ni espace brut), labels DNS
  complets, puis résolution bornée du host (aucune adresse non globale —
  privée, loopback, link-local, multicast, réservée ou non spécifiée) ;
  aucune URL résultat n'est jamais appelée ;
- les résultats sont des données non fiables comme les documents, avec un état
  de version explicitement « non vérifiée » ;
- service indisponible ⇒ état explicite (`unavailable`), jamais présenté actif,
  et jamais un message d'erreur contenant en-têtes, clé ou corps du fournisseur.
"""
from __future__ import annotations

import ipaddress
import json
import multiprocessing
import re
import socket
import threading
import time
from multiprocessing.process import BaseProcess
from typing import Any

import httpx
from sqlalchemy.orm import Session

from .config import Settings

# API officielle v2 (https://docs.firecrawl.dev/api-reference/endpoint/search) :
# réponse `data.web` — le tableau v1 n'est PAS accepté. Endpoint FIXE HTTPS :
# aucune configuration ne permet de le substituer, aucune URL libre n'y entre.
FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/search"

WEB_MAX_RESULTS = 3
# Candidats EXAMINÉS au maximum (bornes de traitement, pas seulement de sortie).
WEB_MAX_CANDIDATES = 12
WEB_QUERY_MAX_CHARS = 160
WEB_RESPONSE_MAX_BYTES = 256 * 1024
WEB_TITLE_MAX_CHARS = 200
WEB_SNIPPET_MAX_CHARS = 400

# Budgets de l'opération isolée. Le parent tient la borne DURE du budget total :
# il n'attend le processus enfant que jusqu'à WEB_CHILD_WAIT_S puis
# terminate/kill avec join borné — le budget wallclock total de l'opération
# reste ≤ WEB_TOTAL_DEADLINE_S (30 s), marge de terminaison comprise. Le
# chercheur enfant reçoit un budget plus court (WEB_CHILD_DEADLINE_S) pour
# terminer proprement AVANT la borne parent ; l'échéance y est vérifiée entre
# les étapes et entre les candidats examinés.
WEB_TOTAL_DEADLINE_S = 30.0
WEB_CHILD_DEADLINE_S = 24.0
WEB_CHILD_WAIT_S = 27.0
WEB_CHILD_KILL_GRACE_S = 1.0
# Bornes réseau de l'appel amont (en plus de l'échéance globale).
WEB_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=10.0)

# Domaine public validé : seules les URL HTTPS de ce domaine (ou sous-domaines)
# sont conservées. Aucun autre domaine n'est accepté, même renvoyé par l'API.
WEB_ALLOWED_DOMAINS = ("wallix.com",)

# Vocabulaire FERMÉ : seuls ces termes peuvent sortir dans une requête.
PRODUCT_TERMS = {
    "wallix": "WALLIX",
    "bastion": "Bastion",
    "access manager": "Access Manager",
}

THEME_TERMS = {
    "voyant": "voyant",
    "led": "voyant",
    "journal": "journal",
    "journaux": "journal",
    "log": "journal",
    "logs": "journal",
    "rotation": "rotation",
    "certificat": "certificat",
    "certificate": "certificat",
    "tls": "TLS",
    "ssl": "TLS",
    "session": "session",
    "sessions": "session",
    "authentification": "authentification",
    "mfa": "MFA",
    "sso": "SSO",
    "ldap": "LDAP",
    "kerberos": "Kerberos",
    "sauvegarde": "sauvegarde",
    "backup": "sauvegarde",
    "restauration": "restauration",
    "restore": "restauration",
    "disponibilité": "haute disponibilité",
    "supervision": "supervision",
    "audit": "audit",
    "trace": "trace",
    "traces": "trace",
    "erreur": "erreur",
    "error": "erreur",
    "redémarrage": "redémarrage",
    "reboot": "redémarrage",
    "configuration": "configuration",
    "procédure": "procédure",
    "port": "port",
    "réseau": "réseau",
    "network": "réseau",
    "compte": "compte",
    "utilisateur": "utilisateur",
    "mise à jour": "mise à jour",
    "upgrade": "mise à jour",
    "version": "version",
}

# Version autorisée : numérique pointée, ASCII strict — jamais un suffixe, un
# jeton, un underscore, un chiffre Unicode ni un exposant. Les valeurs
# invalides sont OMISES de la requête (aucune normalisation permissive, aucun
# strip). Ex. acceptés : 10.10, 12.0.1, 2026.09.29.1.
WEB_VERSION_RE = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}")

# Nom DNS strict : labels ASCII [a-z0-9-], pas de souligné, pas de label vide,
# pas de label commençant ou finissant par un tiret, host ≤ 253 caractères.
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

_TOKEN_RE = re.compile(r"[a-zà-ÿ0-9]+", re.IGNORECASE)

# Statuts du repli web exposés au chat (vocabulaire fermé, jamais inventé).
WEB_STATUSES = ("not_needed", "disabled", "unavailable", "no_results", "ok")

# Contexte multiprocessing PRIVÉ du module : « spawn » — jamais fork dans un
# serveur de production multithreadé. Les tests peuvent le remplacer par
# « fork » (Linux) pour hériter des doubles monkeypatchés ; AUCUNE option
# d'environnement ni d'API ne permet ce changement en production.
_WEB_SEARCH_MP_METHOD = "spawn"

# Deux opérations simultanées AU PLUS ; au-delà, refus immédiat — jamais de
# file d'attente illimitée.
_WEB_SEARCH_SLOTS = threading.BoundedSemaphore(2)


class WebUnavailable(RuntimeError):
    """Service web indisponible (clé absente, réseau, HTTP, réponse illisible).

    Le message est TOUJOURS construit par ce module : jamais les en-têtes, la
    clé, le corps ou une URL du fournisseur.
    """


def web_activation(db: Session, settings: Settings) -> dict:
    from .app_settings import get_row

    row = get_row(db, "web") or {}
    return {
        "env_enabled": settings.web_enabled,
        "operator_activated": bool(row.get("activated")),
        "rag_validated": bool(row.get("rag_validated")),
        "key_present": settings.read_secret("firecrawl_api_key") is not None,
    }


def web_status(db: Session, settings: Settings) -> dict:
    """État EFFECTIF : le drapeau seul ne déclare jamais la fonction active.

    C'est LA barrière commune : l'endpoint manuel la consulte avant toute
    recherche et le repli du chat passe par la même fonction — une clé
    présente ne suffit jamais.
    """
    from .config import web_availability

    effective, reason = web_availability(settings)
    if not effective:
        return {"available": False, "reason": reason}
    state = web_activation(db, settings)
    if not state["operator_activated"] or not state["rag_validated"]:
        return {"available": False, "reason": "en attente d'activation opérateur après recette RAG"}
    if not state["key_present"]:
        return {"available": False, "reason": "clé de service absente"}
    return {"available": True, "reason": None}


def build_public_query(user_text: str, product: str | None, version: str | None) -> str | None:
    """Requête à partir du vocabulaire fermé UNIQUEMENT (jamais de texte brut).

    - produit : mappé sur PRODUCT_TERMS (sinon ignoré) — jamais la valeur brute ;
    - version : uniquement une valeur numérique strictement validée
      (`WEB_VERSION_RE`, ASCII complet) — toute autre forme est OMISE ;
    - thèmes : les jetons de la question qui appartiennent au vocabulaire fermé
      sont remplacés par leur forme publique normalisée (dédupliquée, bornée
      à 4).
    Retourne None si rien de public n'est disponible — aucun appel web dans ce cas.
    """
    parts: list[str] = ["site:wallix.com"]
    product_term = PRODUCT_TERMS.get((product or "").strip().lower())
    if product_term:
        parts.append(product_term)
    if isinstance(version, str) and WEB_VERSION_RE.fullmatch(version):
        parts.append(version)
    seen: list[str] = []
    lowered = (user_text or "").lower()
    for token in _TOKEN_RE.findall(lowered):
        mapped = THEME_TERMS.get(token)
        if mapped and mapped not in seen:
            seen.append(mapped)
        if len(seen) >= 4:
            break
    # Expressions fermées à deux mots (« mise à jour ») : correspondance explicite.
    for phrase, mapped in (("mise à jour", "mise à jour"), ("access manager", "Access Manager")):
        if phrase in lowered and mapped not in seen:
            seen.append(mapped)
    parts.extend(seen[:4])
    if len(parts) <= 1:
        return None
    return " ".join(parts)[:WEB_QUERY_MAX_CHARS]


def _is_strict_dns_name(host: str) -> bool:
    """Nom DNS ASCII strict : au moins deux labels, aucun label vide ou invalide."""
    if not host or len(host) > 253:
        return False
    try:
        host.encode("ascii")
    except UnicodeError:
        return False
    labels = host.split(".")
    if len(labels) < 2:
        return False
    return all(_DNS_LABEL_RE.fullmatch(label) for label in labels)


def _raw_authority(url: str) -> str | None:
    """Autorité BRUTE de l'URL — extraite AVANT toute normalisation URL/IDNA.

    Le texte est pris tel quel entre « :// » et le premier « / », « ? » ou
    « # » : c'est cette forme brute qui est contrôlée, jamais une forme
    réécrite par le parseur. Aucune soumission Unicode, percent-encodée ou
    déguisée ne doit passer par une normalisation silencieuse.
    """
    if not isinstance(url, str):
        return None
    marker = url.find("://")
    if marker < 0:
        return None
    rest = url[marker + 3 :]
    end = len(rest)
    for separator in ("/", "?", "#"):
        index = rest.find(separator)
        if index >= 0:
            end = min(end, index)
    return rest[:end]


def _strict_raw_authority(authority: str) -> bool:
    """Contrôle de l'autorité BRUTE : ASCII uniquement (jamais d'Unicode),
    sans « @ » (userinfo même vide), sans percent-encoding du host, sans
    espace brut ni caractère de contrôle ; un seul « : » au plus (port
    explicite). Les règles DNS complètes sont vérifiées ensuite sur le host
    normalisé — jamais l'inverse."""
    if not authority or len(authority) > 261:  # 253 (host max) + marge « :port »
        return False
    try:
        authority.encode("ascii")
    except UnicodeError:
        return False
    if authority.count(":") > 1:
        return False
    for ch in authority:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or ch in " @%\\/#?":
            return False
    return True


def _result_host_addresses(host: str) -> list[str]:
    """Adresses résolues du host d'un RÉSULTAT (résolution bornée, jamais un fetch).

    Fonction isolée : les tests la remplacent par un double — aucun réseau
    réel dans la suite de tests. Elle s'exécute dans le processus de recherche
    borné : une résolution bloquée ne peut jamais dépasser le budget total.
    """
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            addresses.append(str(sockaddr[0]))
    return addresses


def _host_is_public(host: str) -> bool:
    """Accepte uniquement un host dont TOUTES les adresses sont GLOBALEMENT routables.

    Un seul IPv4/IPv6 non conforme — ou un échec de résolution — fait refuser
    l'URL. Les catégories sont refusées EXPLICITEMENT : sur Python 3.12,
    `is_global` seul accepte encore le multicast (224.0.0.1), donc
    multicast, réservée, non spécifiée, loopback, link-local et privée sont
    toutes contrôlées en plus. Aucune connexion n'est jamais établie vers ce
    host : la résolution sert exclusivement à refuser.
    """
    try:
        addresses = _result_host_addresses(host)
    except (socket.gaierror, OSError):
        return False
    if not addresses:
        return False
    for text in addresses:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return False
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_private
        ):
            return False
    return True


def _allowed_public_url(url: Any) -> bool:
    """URL HTTPS publique acceptée.

    Exigences : schéma HTTPS, autorité BRUTE ASCII contrôlée avant toute
    normalisation (pas d'Unicode, pas de « @ » — userinfo même vide —, pas de
    percent-encoding, pas d'espace brut), aucune info utilisateur, aucun port
    hors 443, aucun fragment, backslash ou caractère de contrôle, host DNS
    ASCII strict du domaine public validé (ou sous-domaine) — labels sans
    tiret en début/fin, ≤ 63 caractères, host ≤ 253 — et résolution
    entièrement globale. Une forme déceptive (suffixe trompeur, userinfo,
    port, souligné, label vide, trailing dot) est refusée sans exception.
    """
    if not isinstance(url, str) or not url:
        return False
    if "\\" in url or "#" in url:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    authority = _raw_authority(url)
    if authority is None or not _strict_raw_authority(authority):
        return False
    try:
        parsed = httpx.URL(url)
        scheme = parsed.scheme
        userinfo = parsed.userinfo
        port = parsed.port
        host = (parsed.host or "").lower()
    except Exception:  # noqa: BLE001 - URL invalide (port, caractères) ⇒ refus
        return False
    if scheme != "https" or userinfo:
        return False
    if port is not None and port != 443:
        return False
    if not _is_strict_dns_name(host):
        return False
    if not any(host == domain or host.endswith("." + domain) for domain in WEB_ALLOWED_DOMAINS):
        return False
    return _host_is_public(host)


def _result_text(value: Any, limit: int) -> str | None:
    """Texte de résultat en CHAÎNE uniquement (aucune conversion objet→str).

    - champ absent/None ⇒ chaîne vide ;
    - type inattendu ⇒ None : l'élément est refusé, jamais converti ;
    - caractères de contrôle retirés, longueur bornée.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch == "\n" or ch == "\t" or ch.isprintable())
    return cleaned[:limit]


def _read_bounded(response: httpx.Response, deadline: float) -> bytes:
    """Lit le corps en refusant tout dépassement de volume ou d'échéance.

    L'échéance est vérifiée au fil de l'eau : une réponse qui continue
    d'arriver après la deadline est refusée, jamais concaténée en entier.
    Un corps qui stagne est arrêté par la borne du processus parent (la
    lecture bloquée ne peut pas être interrompue par ce seul contrôle).
    """
    body = bytearray()
    if time.monotonic() > deadline:
        raise WebUnavailable("délai de recherche dépassé")
    for chunk in response.iter_bytes():
        if time.monotonic() > deadline:
            raise WebUnavailable("délai de recherche dépassé")
        body.extend(chunk)
        if len(body) > WEB_RESPONSE_MAX_BYTES:
            raise WebUnavailable("réponse trop volumineuse")
    if time.monotonic() > deadline:
        raise WebUnavailable("délai de recherche dépassé")
    return bytes(body)


def _search_web_public_inner(query_public: str, key: str, deadline: float) -> dict[str, Any]:
    """Appel borné de l'API v2 — exécuté DANS le processus de recherche isolé.

    Toute anomalie lève `WebUnavailable` (état explicite). Acceptés : HTTP 200
    uniquement, `success` booléen exact `true`, `data.web` liste, éléments aux
    champs URL/titre/description de type chaîne (aucune conversion arbitraire
    objet→str). Les redirections ne sont jamais suivies (un 3xx n'est pas un
    résultat), le proxy d'environnement est ignoré et le message d'erreur ne
    contient jamais en-têtes, clé ou corps du fournisseur. L'échéance est
    vérifiée entre les étapes et entre les candidats examinés (≤ 12) ; aucun
    appel réseau ne peut survenir après le retour du parent (processus tué).
    """
    if not key:
        raise WebUnavailable("clé de service absente")
    if time.monotonic() > deadline:
        raise WebUnavailable("délai de recherche dépassé")
    payload = {
        "query": query_public,
        "limit": WEB_MAX_RESULTS,
        "sources": ["web"],
        "timeout": 20000,
    }
    try:
        with httpx.Client(
            timeout=WEB_TIMEOUT,
            follow_redirects=False,
            verify=True,
            trust_env=False,  # jamais un proxy d'environnement vers un tiers
        ) as client:
            with client.stream(
                "POST",
                FIRECRAWL_ENDPOINT,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            ) as response:
                # Seul HTTP 200 est accepté : une redirection (jamais suivie)
                # ou une erreur n'est PAS un résultat, même avec un corps qui
                # ressemble à une réponse valide.
                if response.status_code != 200:
                    raise WebUnavailable(f"service HTTP {response.status_code}")
                body = _read_bounded(response, deadline)
    except WebUnavailable:
        raise
    except httpx.HTTPError as exc:
        raise WebUnavailable(f"service indisponible ({exc.__class__.__name__})") from exc
    if time.monotonic() > deadline:
        raise WebUnavailable("délai de recherche dépassé")

    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebUnavailable("réponse illisible") from exc
    # Le JSON est VALIDÉ, jamais supposé conforme (v2 : success booléen + data.web).
    if not isinstance(data, dict) or data.get("success") is not True:
        raise WebUnavailable("réponse non conforme (échec du service)")
    payload_data = data.get("data")
    if not isinstance(payload_data, dict):
        raise WebUnavailable("réponse non conforme (data absente)")
    web_items = payload_data.get("web")
    if not isinstance(web_items, list):
        raise WebUnavailable("réponse non conforme (data.web absente)")

    results: list[dict[str, Any]] = []
    examined = 0
    for item in web_items:
        if len(results) >= WEB_MAX_RESULTS or examined >= WEB_MAX_CANDIDATES:
            break
        examined += 1
        if time.monotonic() > deadline:
            raise WebUnavailable("délai de recherche dépassé")
        if not isinstance(item, dict):
            continue
        raw_url = item.get("url")
        if not isinstance(raw_url, str) or not _allowed_public_url(raw_url):
            continue
        title = _result_text(item.get("title"), WEB_TITLE_MAX_CHARS)
        snippet = _result_text(item.get("description"), WEB_SNIPPET_MAX_CHARS)
        if title is None or snippet is None:
            # Type inattendu : l'élément est refusé, jamais converti en chaîne.
            continue
        results.append({"url": raw_url, "title": title, "snippet": snippet})
    return {"query": query_public, "results": results}


def _search_child_main(query_public: str, key: str, conn: Any) -> None:
    """Cible TOP-LEVEL du processus de recherche (spawn) : la recherche
    s'exécute entièrement dans cet enfant isolé et le résultat/erreur revient
    par le pipe privé — jamais par stdout, argv ou un log. Toute erreur est
    convertie en message SÛR construit par ce module. L'enfant n'accède ni à
    la base, ni à une session, ni à la configuration globale : ses seules
    entrées sont la requête publique et la clé (IPC multiprocessing)."""
    try:
        deadline = time.monotonic() + WEB_CHILD_DEADLINE_S
        payload: tuple[str, Any] = ("ok", _search_web_public_inner(query_public, key, deadline))
    except WebUnavailable as exc:
        payload = ("error", str(exc))
    except BaseException:  # noqa: BLE001 - l'enfant ne doit jamais mourir sans réponse sûre
        payload = ("error", "service indisponible")
    try:
        conn.send(payload)
    except (BrokenPipeError, OSError):
        pass
    finally:
        conn.close()


def _terminate_child_bounded(process: BaseProcess) -> None:
    """Terminaison bornée : terminate, join borné, kill si nécessaire, join
    borné — puis fermeture du handle. Aucun enfant ne survit au-delà."""
    if process.is_alive():
        process.terminate()
        process.join(WEB_CHILD_KILL_GRACE_S)
    if process.is_alive():
        process.kill()
        process.join(WEB_CHILD_KILL_GRACE_S)
    try:
        process.close()
    except ValueError:  # encore vivant après les joins : ne doit pas arriver
        pass


def _search_web_public_bounded(query_public: str, key: str) -> dict[str, Any]:
    """Opération de recherche isolée dans un processus enfant (contexte privé).

    - sémaphore : 2 opérations simultanées au plus, acquisition NON bloquante
      (saturé ⇒ `WebUnavailable` immédiat, jamais de file d'attente) ;
    - le parent n'attend le résultat que jusqu'à `WEB_CHILD_WAIT_S` : au-delà,
      l'enfant est terminate/kill avec join borné — le budget wallclock total
      reste ≤ `WEB_TOTAL_DEADLINE_S`, marge de terminaison comprise ; après un
      timeout, aucun appel réseau tardif de cet enfant (il est mort) ;
    - résultat/erreur uniquement par pipe privé ; entrées minimales (requête
      publique + clé) transmises par IPC multiprocessing — jamais argv, stdout
      ni log ; aucune DB/session/config dans l'enfant ; endpoint fixe inchangé.
    """
    if not _WEB_SEARCH_SLOTS.acquire(blocking=False):
        raise WebUnavailable("service saturé (recherches simultanées)")
    try:
        context = multiprocessing.get_context(_WEB_SEARCH_MP_METHOD)
        parent_conn, child_conn = context.Pipe(duplex=False)
        process = context.Process(
            target=_search_child_main,
            args=(query_public, key, child_conn),
            name="wallia-web-search",
            daemon=True,
        )
        try:
            process.start()
        except Exception as exc:  # noqa: BLE001 - démarrage impossible ⇒ erreur sûre
            parent_conn.close()
            child_conn.close()
            raise WebUnavailable("service indisponible (démarrage du chercheur)") from exc
        child_conn.close()
        try:
            if not parent_conn.poll(WEB_CHILD_WAIT_S):
                _terminate_child_bounded(process)
                raise WebUnavailable("délai de recherche dépassé")
            try:
                received = parent_conn.recv()
            except (EOFError, OSError, ValueError) as exc:
                _terminate_child_bounded(process)
                raise WebUnavailable("service indisponible (réponse perdue)") from exc
        finally:
            parent_conn.close()
        if not (isinstance(received, tuple) and len(received) == 2 and received[0] in ("ok", "error")):
            _terminate_child_bounded(process)
            raise WebUnavailable("service indisponible (réponse illisible)")
        # L'enfant a répondu : fin propre attendue de façon bornée.
        process.join(WEB_CHILD_KILL_GRACE_S)
        if process.is_alive():
            _terminate_child_bounded(process)
        else:
            try:
                process.close()
            except ValueError:
                pass
        kind, payload = received
        if kind == "ok":
            return payload
        raise WebUnavailable(str(payload) if isinstance(payload, str) else "service indisponible")
    finally:
        _WEB_SEARCH_SLOTS.release()


def search_web_public(query_public: str, settings: Settings) -> dict[str, Any]:
    """Point d'entrée : clé lue ici, opération isolée bornée ensuite.

    La clé n'est jamais journalisée ni exposée : elle est transmise au
    chercheur uniquement via l'IPC du processus (jamais argv/stdout). Sans
    clé, aucun processus n'est lancé.
    """
    key = settings.read_secret("firecrawl_api_key")
    if not key:
        raise WebUnavailable("clé de service absente")
    return _search_web_public_bounded(query_public, key)


def web_source_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Source web au schéma contractuel : discriminant, URL, extrait, domaine,
    état de version « non vérifiée » ; identifiants/scores/pages NULLS (jamais
    d'identifiants ou de scores factices)."""
    url = result.get("url") if isinstance(result.get("url"), str) else ""
    domain = ""
    if url:
        try:
            domain = (httpx.URL(url).host or "").lower()
        except Exception:  # noqa: BLE001
            domain = ""
    return {
        "source_type": "web",
        "url": url,
        "domain": domain,
        "title": result.get("title") or "",
        "text": result.get("snippet") or "",
        "version_state": "non_verifiee",
        "document_id": None,
        "chunk_id": None,
        "page_start": None,
        "page_end": None,
        "section": None,
        "kind": "web",
        "product": None,
        "versions": [],
        "demo": False,
        "scope": "web",
        "language": None,
        "score": None,
        "score_kind": None,
        "score_vector": None,
        "score_text": None,
    }


def web_fallback_for_chat(
    db: Session,
    settings: Settings,
    *,
    retrieval_status: str,
    user_text: str,
    case_state: dict[str, Any],
) -> dict[str, Any]:
    """Repli web borné APRÈS la recherche corpus. Ne transforme jamais le statut
    du corpus ; ne s'active jamais pour masquer une indisponibilité technique.

    Le même contrôle EFFECTIF (`web_status`) garde l'endpoint manuel et le
    chat : drapeau d'environnement, activation opérateur (recette RAG) et clé ;
    une clé présente ne suffit jamais.

    Retourne {"status": not_needed|disabled|unavailable|no_results|ok,
              "reason": str|None, "query": str|None, "sources": [sources web]}.
    """
    state = web_status(db, settings)
    if not state.get("available"):
        return {"status": "disabled", "reason": state.get("reason"), "query": None, "sources": []}
    if retrieval_status not in ("no_relevant_source", "empty_corpus"):
        # Corpus ok, ou indisponibilité technique : jamais un repli qui
        # masquerait `retrieval_unavailable`/`embeddings_unavailable`.
        return {"status": "not_needed", "reason": None, "query": None, "sources": []}

    query = build_public_query(user_text, case_state.get("product"), case_state.get("version"))
    if query is None:
        return {
            "status": "not_needed",
            "reason": "aucun terme public du vocabulaire fermé pour cette question",
            "query": None,
            "sources": [],
        }
    try:
        result = search_web_public(query, settings)
    except WebUnavailable as exc:
        return {"status": "unavailable", "reason": str(exc), "query": query, "sources": []}
    if not result["results"]:
        return {"status": "no_results", "reason": None, "query": query, "sources": []}
    sources = [web_source_from_result(item) for item in result["results"]]
    return {"status": "ok", "reason": None, "query": query, "sources": sources}
