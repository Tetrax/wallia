# Tests de livraison/exploitation Wallia (lot corrections run209)

Aucun Docker réel, aucun réseau, aucun root : ces tests exécutent les scripts
contre des DOUBLES explicites et des répertoires temporaires. Ils couvrent les
défauts concrets relevés par la revue `docs/review-run209.md` et les corrections
du lot `docs/corrections-run209-infra-fix.md`.

## Exécution (par le principal / Astra, depuis la racine du dépôt)

```bash
python3 -m pytest tests/delivery -q
```

Prérequis : `python3` avec `pytest`, `bash`, `tar`/`gzip` (sauvegarde),
`openssl` (TLS, sinon ignoré), `cryptography` (test du certificat futur,
sinon ignoré), et les dépendances applicatives pour
`test_check_models_manifest.py` (import de `backend/app/cli.py`, sans DB ni
réseau ; test ignoré si l'import est impossible).

## Contenu

| Fichier | Couvre | Doubles / fixtures |
|---|---|---|
| `conftest.py` | socle : bac à sable jetable, doubles `docker`/`git` pilotés par un état JSON (rendu `config` respectant `WALLIA_IMAGE`, panne `ps` injectable), journal d'appels ordonné | `DeliverySandbox` (fixture `delivery`) |
| `test_restore_lib.py` | schéma strict (8 tables exactes, types), chemins canoniques sans traversée, SHA256 archives avant parsing, liens/absolus/membres non réguliers refusés, cible neuve, fichiers manquants/extra/altérés, comptes, références DB→fichiers | bundles tar/gzip construits en `tmp_path` |
| `test_delivery_helpers.py` | env Compose EFFECTIF strict ; état atomique 0600/0700, mapping `--compose/--env → compose_snapshot/env_snapshot`, références incohérentes refusées à l'écriture, `previous=null` exclusif (jamais mêlé à un état image), `validate-refs` | entrées JSON sur stdin / fichiers `tmp_path` |
| `test_deploy_rollback_mocked.py` | refus `--sha`, aucune mutation avant contrôle strict, projet forcé, `healthy` obligatoire, `--bootstrap-previous-env` RETIRÉE (refus sans mutation), previous repris de `current.json` (jamais fabriqué), snapshot JSON référençant l'ID produit AVANT up/migrate, mutations par CE snapshot, image effective vérifiée, rollback par snapshot uniquement (refus si snapshot incohérent/tag mutable, échec de santé sans consommation) | double `docker` + double `git` |
| `test_backup_flow_mocked.py` | snapshot courant validé utilisé ; runtime historique exige `--env-file` explicite ; découverte `ps` et vérification d'arrêt FATALES ; seuls services initialement actifs ; arrêt non confirmé ⇒ échec + reprise ; reprise en échec ⇒ rc non nul ; référence DB sans fichier ⇒ NON valide avec reprise ; `ON_ERROR_STOP` | double `docker` (stop/start/exec/ps/inspect simulés) |
| `test_tls_self_check.py` | paire valide acceptée (clé publique des DEUX côtés), clé étrangère / hôte non couvert / certificat FUTUR (notBefore dans le futur) refusés | paires auto-signées éphémères (`openssl`, `cryptography` pour le futur) |
| `test_tls_transaction.py` | transaction TLS : rollback UNIQUE sur échec nginx, échec `install`, probe ACME (retries bornés) ; restauration EXACTE (fichier+mode, lien relatif, fichier enabled) ou absence ; refus avant mutation si le modèle manque | COPIE du script aux racines réécrites vers `tmp_path` + doubles non privilégiés `id`/`install`/`nginx`/`systemctl`/`curl` |
| `test_check_models_manifest.py` | manifestes : essentiels listés ET présents, chemins sûrs (`../`, absolu, cache refusés), identité/révision/licence, SHA/taille ; alignement des listes avec `fetch_models.py` | import local de `backend/app/cli.py`, répertoires `tmp_path` |

## Règles du socle

- Le double `docker` journalise CHAQUE appel (`state["calls"]`) : les
  assertions portent sur l'ordre réel (contrôle avant mutation, arrêt → dump →
  reprise, snapshot avant up), jamais sur des suppositions ; il ne renvoie
  jamais un ID factice indépendant de l'état déclaré.
- Aucun `docker run`, `down`, volume, port ou lifecycle système réel ; aucun
  fichier hors `tmp_path` ; aucun secret réel (`runtime/secrets/` du bac à
  sable ne contient que des valeurs de test).
- `test_tls_transaction.py` réécrit des racines SYSTÈME dans une copie du
  script (le script livré garde ses racines fixes, sans override par
  variable d'environnement) ; le harnais vérifie ses remplacements et échoue
  si une constante du script change — rien n'est écrit dans `/etc`,
  `/var/lib` ou `/var/www` réels.
- `WALLIA_HEALTH_TIMEOUT`/`WALLIA_HEALTH_POLL` bornés par le bac à sable : les
  cas d'échec de santé durent ~2 s au lieu du défaut de production.
