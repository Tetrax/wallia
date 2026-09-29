# AGENTS.md — Wallia

- Périmètre : dépôt unique Wallia (API FastAPI + worker + frontend React/TS).
- Contrats : docs/architecture.md est autoritatif ; docs/state.md porte l'état.
- Ne jamais committer runtime/, des secrets, des données d'exécution ou des exports.
- La CI « quality » doit rester verte ; aucun appel payant dans la CI.
- Le corpus de démonstration est fictif : ne jamais le présenter comme officiel.
- Toute modification de sécurité (auth, sessions, CSRF, permissions) exige tests + revue.
- Un seul auteur par fichier ; pas de secrets dans les logs, prompts ou tests.
