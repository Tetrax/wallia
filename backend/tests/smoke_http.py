"""Smoke HTTP authentifié de la pile Wallia.

S'exécute dans un conteneur de l'image Wallia (ou sur l'hôte avec httpx) :
    cat runtime/secrets/initial-access.txt | docker compose -p wallia exec -T api \
        python tests/smoke_http.py

Les identifiants sont lus depuis un flux (stdin) ou un fichier, jamais affichés,
jamais passés en ligne de commande. Aucun secret n'est imprimé.
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {name}"
    if detail and not condition:
        line += f" — {detail}"
    print(line, flush=True)
    if not condition:
        FAILURES.append(name)


def read_credentials(argv: list[str]) -> tuple[str, str]:
    path = None
    if "--creds-file" in argv:
        path = argv[argv.index("--creds-file") + 1]
    if path:
        raw = open(path, encoding="utf-8").read()
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        raise SystemExit("identifiants manquants : fournir --creds-file ou un stdin non interactif")
    email = password = None
    for line in raw.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "email":
            email = value.strip()
        elif key == "password":
            password = value.strip()
    if not email or not password:
        raise SystemExit("fichier d'accès initial illisible (email/password absents)")
    return email, password


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("WALLIA_SMOKE_BASE", "http://api:8000"))
    parser.add_argument("--creds-file", default=None)
    args, _unknown = parser.parse_known_args()
    email, password = read_credentials(sys.argv)

    with httpx.Client(base_url=args.base, timeout=30.0) as client:
        health = client.get("/healthz")
        check("santé /healthz", health.status_code == 200 and health.json().get("status") == "ok", health.text[:200])

        login = client.post("/api/auth/login", json={"email": email, "password": password})
        check("connexion authentifiée", login.status_code == 200, f"HTTP {login.status_code}")
        if login.status_code != 200:
            return 1
        csrf = login.json()["csrf_token"]

        me = client.get("/api/auth/me")
        check("session active (/me)", me.status_code == 200 and me.json()["user"]["email"] == email)

        status = client.get("/api/status")
        check("état applicatif", status.status_code == 200 and "provider" in status.json())

        conversation = client.post("/api/conversations", json={"title": "smoke"}, headers={"X-CSRF-Token": csrf})
        check("création de conversation", conversation.status_code == 201, conversation.text[:200])
        conversation_id = conversation.json()["id"] if conversation.status_code == 201 else None

        if conversation_id:
            with client.stream(
                "POST",
                f"/api/conversations/{conversation_id}/chat",
                json={"text": "Test de fumée : décrire l'état du système."},
                headers={"X-CSRF-Token": csrf},
            ) as stream:
                first = ""
                for line in stream.iter_lines():
                    first = line
                    break
            check("flux SSE démarré", first.startswith("event: meta") or first.startswith("data: "), first[:120])

        documents = client.get("/api/documents")
        check("administration documentaire", documents.status_code == 200)

        search = client.post("/api/search", json={"query": "voyant ambre"}, headers={"X-CSRF-Token": csrf})
        check(
            "recherche documentaire",
            search.status_code == 200 and search.json().get("status") in {"ok", "no_relevant_source", "empty_corpus"},
            search.text[:200],
        )

        logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        check("déconnexion", logout.status_code == 200)
        check("session révoquée", client.get("/api/auth/me").status_code == 401)

    if FAILURES:
        print(f"\n{len(FAILURES)} vérification(s) en échec : {', '.join(FAILURES)}")
        return 1
    print("\nSmoke HTTP : toutes les vérifications sont passées.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
