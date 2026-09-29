# Corrections run209 — micro-correctif final infra

Contexte : test réel post-correctifs (`runtime/evidence/run209-infra-tests-after-fix.log`) :
**88 PASS / 1 FAIL / 1 SKIP en 44,85 s**. Échec unique :
`test_nginx_echec_rollback_restaure_le_fichier_enabled_exact` (attendu `OLD-VHOST`,
obtenu `ENABLED-OLD`).

Quatre correctifs minimaux, préparés par le codeur fichiers-only
(`opencode-go/deepseek-v4.1-flash`, effort max). **Tous les tests décrits ci-dessous
sont NON EXÉCUTÉS par le codeur** — revue puis exécution par le principal uniquement.

## 1. `deployment/wallia-tls-activate.sh` — sauvegardes de transaction

Cause (lue dans le log) : `capture_state` nommait la sauvegarde avec le seul
`basename` + horodatage + PID ; le vhost et l'entrée sites-enabled partagent le
même basename dans le même processus/seconde → la seconde capture écrasait la
première, et le rollback restaurait le mauvais contenu.

- le nom de sauvegarde inclut désormais le préfixe de rôle
  (`VHOST_STATE-`, `ENABLED_STATE-`, `ACTIVE_STATE-`) ;
- aucune sauvegarde existante n'est écrasée (suffixe `-N` incrémental) ;
- le test `test_nginx_echec_rollback_restaure_le_fichier_enabled_exact` conserve
  intégralement ses assertions de contenu/mode exacts ; ajout d'assertions de
  sauvegardes distinctes et préfixées par rôle (2 fichiers).

## 2. `deployment/wallia-tls-activate.sh` — échec de restauration cumulé

`rollback_all` pouvait dire « tout restauré » dès qu'AU MOINS une restauration
réussissait, même si une autre échouait. Désormais : tout échec de restauration
est cumulé ; aucune phrase de succès global ; l'intervention manuelle est
signalée ; nginx n'est PAS rechargé sur un état mixte ; le code d'échec initial
reste celui conservé par l'appelant et il n'y a jamais de second rollback.

Test ciblé ajouté (`test_rollback_partiel_restauration_en_echec_pas_de_succes_global`)
avec double non privilégié — jamais de vrai Nginx ni de root ; aucun override
root/path ajouté au script livré.

## 3. `backend/app/cli.py` (`_verify_manifest_dir`) — lien sortant

Les chemins étaient lexicalement bornés mais un lien symbolique pouvait sortir du
répertoire. La résolution de chaque fichier — `manifest.json` compris, fichiers
essentiels compris — doit rester sous `target.resolve()` ; un lien sortant est
refusé AVANT toute lecture de contenu ou calcul d'empreinte. Aucun changement
d'algorithme, de modèle, de révision ou de seuil.

Tests ajoutés dans `tests/delivery/test_check_models_manifest.py` : lien sortant
sur fichier essentiel, sur `manifest.json`, et sur une entrée du manifeste —
cible = fichier de test anodin, aucun secret réel.

## 4. `scripts/{build.sh,deploy.sh,rollback.sh,tests-isolated.sh}` — conteneurs conservés

Retrait du seul `--rm` (auto-suppression) des conteneurs de travail : sonde de
build, migrations deploy/rollback, runner des tests isolés. Aucun remplacement
par `docker rm`/cleanup/trap, aucune autre logique modifiée.

**Ces conteneurs restent après leur fin naturelle** (migration one-shot, sonde de
build, runner de tests) ; le principal récupère leurs identités (`docker ps -a`)
avant tout nettoyage ultérieur. Aucun conteneur live modifié. Les tests à doubles
n'invoquent jamais un vrai Docker.

## Limites

- Correctifs **non exécutés** ici (codeur fichiers-only) : les résultats réels
  seront ceux de la prochaine exécution par le principal.
- Aucun commit, aucun push, aucun déploiement.
- Périmètre strictement limité aux 8 fichiers ci-dessus + cette note.
