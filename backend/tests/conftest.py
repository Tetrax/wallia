"""Socle de tests Wallia.

- PostgreSQL réel avec pgvector : aucune substitution SQLite. Une base
  `wallia_test` est créée automatiquement à côté de la base de travail.
- Serveur uvicorn réel (HTTP + SSE + déconnexions vraies).
- Embeddings : backend `fixture` (déterministe, interdit en production) pour la
  CI ; les tests réels d'embeddings/Docling sont exécutés en acceptance locale.
- ISOLATION STRICTE : secrets et données de test sont TOUJOURS des répertoires
  dédiés ; les répertoires du runtime de livraison (/secrets, /data) ne sont
  jamais utilisés ni modifiés. La suite REFUSE de s'exécuter dans le conteneur
  de livraison (détection /secrets réels) et exige le harnais isolé
  `scripts/tests-isolated.sh` ; la base `wallia_test` est la seule tronquée.
"""
from __future__ import annotations

import os
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

# --- garde-fou : jamais dans le conteneur de livraison ------------------------
# Le conteneur api/worker de la pile wallia monte /secrets et /data réels. La
# suite ne doit JAMAIS y tourner (risque de secrets/données vivants) : elle doit
# s'exécuter dans le harnais isolé (scripts/tests-isolated.sh, qui pose
# WALLIA_TEST_RUNNER=isolated et ne monte ni /secrets ni /data).
# Les contrôles production/secrets réels ci-dessous sont exécutés AVANT toute
# dérogation liée à WALLIA_TEST_RUNNER : ce dernier ne peut jamais les
# court-circuiter (isolated signifie « harnais dédié », pas « contrôles
# désactivés »).
_DELIVERY_MARKERS = ("/secrets/db_password", "/secrets/initial-access.txt", "/secrets/session_secret")


def _refuse_delivery_container() -> None:
    if os.environ.get("WALLIA_ENV") == "production":
        raise RuntimeError(
            "suite de tests refusée : environnement production détecté — "
            "utiliser le runner isolé (scripts/tests-isolated.sh)."
        )
    for marker in _DELIVERY_MARKERS:
        if Path(marker).exists():
            raise RuntimeError(
                f"suite de tests refusée : conteneur de livraison détecté ({marker}) — "
                "utiliser le runner isolé (scripts/tests-isolated.sh)."
            )


_refuse_delivery_container()

# --- environnement de test défini AVANT tout import applicatif ---------------
os.environ["WALLIA_ENV"] = "test"
os.environ["WALLIA_EMBEDDING_BACKEND"] = "fixture"
os.environ["WALLIA_API_RATE_PER_MINUTE"] = "1000000"
os.environ["WALLIA_LOGIN_RATE_PER_MINUTE"] = "1000000"
os.environ["WALLIA_UPLOAD_IMAGE_MAX_PIXELS"] = "1000000"
# Valeurs imposées (et non par défaut) : l'environnement d'exécution peut en
# définir d'autres, qui ne doivent jamais changer le sens des tests.
os.environ["WALLIA_PROVIDER_ALLOWED_DOMAINS"] = "api.deepseek.com,127.0.0.1,localhost,fake-upstream"
# Reclassement : backend `fixture` EXPLICITE (mécanique de test déclarée,
# jamais une preuve métier) ; le backend réel `transformers` n'est chargé que
# par les recettes isolées explicites, hors de cette suite. Les anciennes
# barrières d'éligibilité cosinus/lexicale (lot3/lot3b) sont remplacées par le
# reclassement (docs/reranker-probe.md) : leurs variables ne sont plus lues.
os.environ["WALLIA_RERANKER_BACKEND"] = "fixture"
# Bornes de temps réduites : les tests de silence/timeout restent rapides.
os.environ["WALLIA_CHAT_MAX_SECONDS"] = "12"
os.environ["WALLIA_CHAT_MAX_CONCURRENT"] = "2"
os.environ["WALLIA_WORKER_LEASE_SECONDS"] = "6"
os.environ["WALLIA_WORKER_JOB_TIMEOUT_SECONDS"] = "60"

# Répertoires DÉDIÉS (forcés, jamais hérités du conteneur vivant). Toute valeur
# héritée est écrasée : les tests ne doivent toucher ni /secrets ni /data.
_TEST_ROOT = Path(
    os.environ.get("WALLIA_TEST_TMP") or tempfile.mkdtemp(prefix="wallia-tests-")
).resolve()
os.environ["WALLIA_SECRETS_DIR"] = str(_TEST_ROOT / "secrets")
os.environ["WALLIA_DATA_DIR"] = str(_TEST_ROOT / "data")
_SECRETS = Path(os.environ["WALLIA_SECRETS_DIR"])
_DATA = Path(os.environ["WALLIA_DATA_DIR"])


def _assert_isolated(path: Path) -> None:
    """Garde-fou : refuse toute racine de test hors du répertoire dédié.

    Comparaison par chemins résolus (`Path.is_relative_to`), jamais par
    préfixe de chaîne : `/root/secrets-extra` ne doit pas passer pour
    `/secrets`. Les DESCENDANTS de /secrets et /data sont refusés eux aussi —
    y compris quand WALLIA_TEST_TMP est configuré à l'intérieur : le contenu
    réel resterait sinon directement atteignable.
    """
    resolved = Path(path).resolve()
    forbidden = (Path("/secrets").resolve(), Path("/data").resolve())
    if any(resolved.is_relative_to(root) for root in forbidden):
        raise RuntimeError(
            f"isolation des tests rompue : {resolved} est sous un répertoire interdit (/secrets ou /data) — arrêt."
        )
    if not resolved.is_relative_to(_TEST_ROOT):
        raise RuntimeError(
            f"isolation des tests rompue : {resolved} n'est pas sous {_TEST_ROOT} — arrêt."
        )


_assert_isolated(_SECRETS)
_assert_isolated(_DATA)
_SECRETS.mkdir(parents=True, exist_ok=True)
_DATA.mkdir(parents=True, exist_ok=True)
for _name in ("session_secret", "worker_token"):
    _path = _SECRETS / _name
    if not _path.exists():
        _path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")

TEST_DB_NAME = "wallia_test"


def _admin_url() -> str:
    """URL d'administration pour créer la base de test.

    Le mot de passe est lu silencieusement (fichier de secrets existant ou
    variable dédiée) et n'est jamais affiché ; seule la base `wallia_test`
    est créée/joignée, jamais la base de travail.
    """
    explicit = os.environ.get("WALLIA_TEST_DB_URL")
    if explicit:
        return explicit
    host = os.environ.get("WALLIA_DB_HOST", "127.0.0.1")
    port = os.environ.get("WALLIA_DB_PORT", "5432")
    user = os.environ.get("WALLIA_DB_USER", "wallia")
    name = os.environ.get("WALLIA_DB_NAME", "wallia")
    password = os.environ.get("WALLIA_TEST_DB_PASSWORD")
    if not password:
        password_file = os.environ.get("WALLIA_DB_PASSWORD_FILE")
        if password_file and Path(password_file).is_file():
            password = Path(password_file).read_text(encoding="utf-8").strip()
    if not password:
        password = "wallia"
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def _ensure_test_database() -> str:
    """Crée la base wallia_test si nécessaire et retourne l'URL de test."""
    import psycopg
    from sqlalchemy.engine import make_url

    admin = make_url(_admin_url())
    if admin.database and admin.database != TEST_DB_NAME:
        # on se connecte toujours à une base d'administration distincte pour
        # créer wallia_test ; la base de travail n'est jamais ouverte par les tests.
        pass
    plain = admin.set(drivername="postgresql")
    # str(URL) masque le mot de passe (***) : il faut le rendu explicite.
    with psycopg.connect(plain.render_as_string(hide_password=False), autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,)).fetchone()
        if row is None:
            conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    test_url = admin.set(database=TEST_DB_NAME)
    return test_url.render_as_string(hide_password=False)


def _configure_db() -> str:
    test_url = _ensure_test_database()
    os.environ["WALLIA_DB_URL"] = test_url
    return test_url


def _assert_test_database() -> None:
    """Refuse toute opération destructive hors de `wallia_test`.

    La base vivante (`wallia`, base admin quelconque) n'est jamais tronquée par
    les tests : seuls les TRUNCATE de la suite ciblent la base de test dédiée.
    """
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["WALLIA_DB_URL"])
    if url.database != TEST_DB_NAME:
        raise RuntimeError(
            f"refus : WALLIA_DB_URL pointe sur {url.database!r}, pas sur {TEST_DB_NAME!r} — arrêt."
        )


@pytest.fixture(scope="session")
def settings():
    _configure_db()
    from app.config import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
def migrated(settings):
    from app.db import reset_engine
    from app.migrate import run_migrations

    reset_engine()
    run_migrations(verbose=False)
    return settings


@pytest.fixture(scope="session")
def live_server(migrated):
    import uvicorn

    from app.config import get_settings

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Origines autorisées : le serveur de test écoute sur un port éphémère.
    previous_origins = os.environ.get("WALLIA_ALLOWED_ORIGINS")
    os.environ["WALLIA_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{port},http://localhost:{port}"
    get_settings.cache_clear()

    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="wallia-test-server")
    thread.start()
    deadline = time.time() + 30
    while time.time() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("serveur de test non démarré")
    base_url = f"http://127.0.0.1:{port}"
    # Fidélité production : le worker passe par l'endpoint interne d'embeddings.
    previous = os.environ.get("WALLIA_API_BASE_URL")
    os.environ["WALLIA_API_BASE_URL"] = base_url
    yield base_url
    if previous is None:
        os.environ.pop("WALLIA_API_BASE_URL", None)
    else:
        os.environ["WALLIA_API_BASE_URL"] = previous
    if previous_origins is None:
        os.environ.pop("WALLIA_ALLOWED_ORIGINS", None)
    else:
        os.environ["WALLIA_ALLOWED_ORIGINS"] = previous_origins
    get_settings.cache_clear()
    server.should_exit = True
    thread.join(timeout=15)


@pytest.fixture(autouse=True)
def clean_state(migrated, live_server):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.db import session_scope

    _assert_test_database()

    def _truncate() -> None:
        with session_scope() as db:
            db.execute(
                text(
                    "TRUNCATE users, sessions, login_attempts, conversations, messages, attachments,"
                    " documents, chunks, ingestion_jobs, settings CASCADE"
                )
            )

    # Un stream SSE résiduel peut tenir une transaction courte : un deadlock
    # transitoire sur le TRUNCATE est retenté (borné), jamais ignoré.
    for attempt in range(3):
        try:
            _truncate()
            break
        except OperationalError as exc:
            if "deadlock" not in str(exc).lower() or attempt == 2:
                raise
            time.sleep(0.3 * (attempt + 1))
    # La clé fournisseur est supprimée UNIQUEMENT dans le répertoire de test
    # dédié (garde-fou ci-dessous) : jamais dans /secrets réel.
    key = migrated.secret_path("provider_api_key")
    _assert_isolated(key)
    if key.exists():
        key.unlink()
    from app.routers.auth import reset_login_bucket
    from app.routers.chat import reset_generation_state
    from app.reranking import reset_reranker_service

    reset_login_bucket()
    reset_generation_state()
    reset_reranker_service()
    yield


@pytest.fixture()
def client(live_server):
    import httpx

    with httpx.Client(base_url=live_server, timeout=60.0, follow_redirects=False) as http:
        yield http


@pytest.fixture(scope="session")
def fake_upstream():
    """Faux fournisseur OpenAI-compatible, partagé par les tests de streaming."""
    import socket
    import threading as _threading
    import time as _time

    import uvicorn

    from tests.fake_upstream import app as fake_app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(fake_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = _threading.Thread(target=server.run, daemon=True, name="wallia-fake-upstream")
    thread.start()
    deadline = _time.time() + 20
    while _time.time() < deadline and not getattr(server, "started", False):
        _time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("faux fournisseur non démarré")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "motdepasse-test-123456"


@pytest.fixture()
def admin(migrated):
    from app.db import session_scope
    from app.models import User
    from app.security import hash_password

    with session_scope() as db:
        user = User(email=ADMIN_EMAIL, password_hash=hash_password(ADMIN_PASSWORD), is_admin=True)
        db.add(user)
        db.flush()
        user_id = str(user.id)
    return {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "id": user_id}


@pytest.fixture()
def other_user(migrated):
    from app.db import session_scope
    from app.models import User
    from app.security import hash_password

    email = "second@test.local"
    password = "motdepasse-second-123456"
    with session_scope() as db:
        user = User(email=email, password_hash=hash_password(password), is_admin=False)
        db.add(user)
        db.flush()
        user_id = str(user.id)
    return {"email": email, "password": password, "id": user_id}


def login(client, credentials) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": credentials["email"], "password": credentials["password"]},
        headers={"Origin": str(client.base_url).rstrip("/")},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]
