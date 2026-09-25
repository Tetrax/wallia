# Wallia

Assistant de support technique — prototype privé, **indépendant et non officiel WALLIX**.

- Interface de chat conversationnelle (français, thème sombre) : conversations persistantes, pièces jointes, citations sourcées.
- RAG réel : PostgreSQL 17 + pgvector, extraction Docling CPU, embeddings multilingues `intfloat/multilingual-e5-small`.
- Corpus de démonstration **fictif** clairement identifié (« Documents de démonstration — non officiels »).
- Fournisseur de chat OpenAI-compatible configurable (`deepseek-flash` par défaut), activé quand une clé API est fournie par l'opérateur.

## Démarrage rapide

```bash
scripts/init.sh          # secrets locaux + DB + migrations + admin (idempotent)
scripts/build.sh --test  # image Docker (mode test local)
scripts/dev_up.sh        # stack locale sur 127.0.0.1:13745
scripts/smoke.sh         # vérifications HTTP authentifiées
```

Identifiants initiaux : `runtime/secrets/initial-access.txt` (mode 0600, hors Git).
Documentation : `docs/architecture.md` (conception), `docs/operations.md` (exploitation), `docs/implementation.md` (détails d'implémentation).

## Structure

```
backend/    API FastAPI, worker d'ingestion, migrations SQL, tests
frontend/   React + TypeScript + Vite
deploy/     Dockerfile, Compose, Nginx/TLS préparés
scripts/    init, build, deploy, backup/restore, smoke, fixtures
resources/  méthodes métier versionnées (prompt système)
tests/      acceptance + e2e Playwright
fixtures/   corpus synthétique reproductible (non officiel)
docs/       architecture, état, implémentation, opérations
```

Statut : prototype en construction — voir `docs/state.md` pour l'état réel et les capacités actives.
