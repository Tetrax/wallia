# Wallia — déploiement (fichiers préparés, application par le principal)

Le prototype ne publie rien sur Internet : l'API écoute uniquement sur
`127.0.0.1:13745`. Les fichiers de ce dossier sont des **modèles à installer**
par l'opérateur root ; l'implémenteur n'a modifié ni `/etc/nginx`, ni
`/etc/letsencrypt`, ni systemd.

## 0. Installation système (opérateur root, une fois)

Le script d'activation et les modèles de vhost sont root-owned, à des chemins
FIXES — un script exécuté par root ne lit jamais un fichier du dépôt (mutable
par l'utilisateur) :

```bash
sudo install -o root -g root -m 0755 deployment/wallia-tls-activate.sh /usr/local/sbin/wallia-tls-activate
sudo install -d -o root -g root -m 0755 /etc/wallia/tls-templates
sudo install -o root -g root -m 0644 \
  deployment/nginx-wallia-http-initial.conf \
  deployment/nginx-wallia-le-bootstrap.conf \
  deployment/nginx-wallia-final.conf \
  /etc/wallia/tls-templates/
sudo install -o root -g root -m 0755 deployment/tls-hook.sh /etc/letsencrypt/renewal-hooks/deploy/wallia-tls-hook.sh
```

Le script d'activation ne s'auto-installe pas et refuse d'écrire ailleurs que
dans `/etc/nginx/sites-{available,enabled}/wallia.valdev.me`,
`/var/lib/wallia/` et le webroot ACME. Toutes les invocations se font SOUS le
verrou infra existant (`/home/tetrax/workspace/.locks/valdev-infra.lock`) ;
le hook de renouvellement, lui, est déjà appelé sous le verrou de certbot et
ne le reprend jamais.

## 1. TLS — trois phases explicites

```bash
# Phase 1 — vhost HTTP (ACME) + probe local dans le vrai .well-known/acme-challenge
flock /home/tetrax/workspace/.locks/valdev-infra.lock /usr/local/sbin/wallia-tls-activate --install-http

# Acquisition LE (webroot dédié), sous le même verrou
flock /home/tetrax/workspace/.locks/valdev-infra.lock certbot certonly --webroot \
  -w /var/www/wallia-acme -d wallia.valdev.me --cert-name wallia.valdev.me

# Phase 2 — paire LE VALIDÉE puis RÉELLEMENT SERVIE (vhost intermédiaire),
# empreinte SNI vérifiée avec retries bornés (anciens workers Nginx)
flock /home/tetrax/workspace/.locks/valdev-infra.lock /usr/local/sbin/wallia-tls-activate --stage-le

# Phase 3 — paire gérée (génération immuable + lien relatif `active`, bascule
# atomique) puis vhost final ; rollback si nginx -t/reload ou empreinte servie diffèrent
flock /home/tetrax/workspace/.locks/valdev-infra.lock /usr/local/sbin/wallia-tls-activate --activate
```

Validation sans privilège d'une paire (aucune mutation) :

```bash
deployment/wallia-tls-activate.sh --self-check --cert <cert> --key <key> --host wallia.valdev.me
```

Vérifier HTTPS sans `-k` : `curl -sS -o /dev/null -w '%{http_code}\n' https://wallia.valdev.me/healthz`.

Renouvellement : viser uniquement la lignée Wallia —
`certbot renew --cert-name wallia.valdev.me` ; le hook est NO-OP pour toute
autre lignée. Ne jamais lancer de `--dry-run` global non ciblé.

## 2. Bascule de l'application en production

Créer `runtime/secrets/app.production.env` (0600) avec l'environnement STRICT
(l'environnement Compose **effectif** est contrôlé par `scripts/delivery_env_check.py`
via `docker compose config` — fichier ET variables héritées — rien n'est affiché) :

```
WALLIA_ENV=production
WALLIA_COOKIE_SECURE=1
WALLIA_ALLOWED_ORIGINS=https://wallia.valdev.me
WALLIA_TRUSTED_PROXY_CIDRS=<IP/CIDR de Nginx, ex. 172.31.245.0/24>
WALLIA_PROVIDER_ALLOWED_DOMAINS=api.deepseek.com
WALLIA_PROVIDER_ENDPOINT=https://api.deepseek.com/v1
WALLIA_PROVIDER_MODEL=deepseek-flash
WALLIA_VISION_ENABLED=0
WALLIA_WEB_ENABLED=0
WALLIA_API_PORT=13745
```

Puis :

```bash
scripts/deploy.sh --env-file runtime/secrets/app.production.env
```

- le script refuse un arbre sale ou un HEAD non poussé (vérification en ligne)
  et déploie TOUJOURS le HEAD ; il n'existe plus de `--sha` arbitraire ;
- image `wallia:<sha-complet>`, label OCI concordant, image ID immuable résolu
  UNE fois ; snapshot Compose RENDU (JSON) référençant cet ID, produit AVANT
  up/migrate et utilisé pour les mutations ; santé `healthy` exigée de
  db/api/worker, image EFFECTIVE d'api/worker vérifiée après démarrage ;
- l'état précédent (snapshot Compose RENDU + env + image ID) est enregistré
  sous `runtime/deploy-state/` (0700/0600) pour `scripts/rollback.sh`.
- transition depuis un runtime Wallia EXISTANT sans état suivi : le
  déploiement est REFUSÉ avant toute mutation (`--bootstrap-previous-env` est
  retirée : une capture automatique depuis un conteneur ne serait pas un
  rollback fidèle). Préparer et VÉRIFIER manuellement un snapshot fidèle de la
  configuration réellement active (image ID effectif, Compose rendu, env) et
  l'installer comme `runtime/deploy-state/current.json` avant ce premier
  déploiement (docs/operations.md §12). Première installation réellement vide :
  `previous=null` (rollback impossible, c'est explicite).

## 3. Clé du fournisseur applicatif

- Endpoint natif attendu : `https://api.deepseek.com/v1`, modèle `deepseek-flash`.
- Déposer la clé **soit** dans `runtime/secrets/provider_api_key` (0600, uid 1002),
  **soit** via l'interface Administration → « Connexion modèle » (écriture
  write-only, jamais relue ni affichée).
- Sans clé : l'application reste en mode démonstration honnête (recherche réelle,
  pas de texte généré par un modèle).
- Tester avec le bouton « Tester la connexion » puis vérifier `/api/status`.

## 4. Retour arrière

1. `scripts/rollback.sh` : rejoue le snapshot précédent (Compose rendu + env +
   image ID immuable). Il échoue AVANT toute mutation si aucun état précédent
   valide n'existe ; après succès `runtime/deploy-state/current.json` est
   actualisé. Les migrations restent AVANT-only : si des migrations
   incompatibles ont été appliquées, restaurer d'abord la sauvegarde
   (`docs/operations.md` §10).
2. La base est conservée dans le volume `wallia_pgdata` ; sauvegardes :
   `scripts/backup.sh`, restauration isolée testée :
   `scripts/restore.sh --isolated <bundle>` (la restauration en place est une
   procédure opérateur contrôlée, `docs/operations.md` §10).
3. Nginx : les vhosts sauvegardés sont sous `/var/lib/wallia/vhost-backups/`;
   installation/rollback gérés par `wallia-tls-activate` (nginx -t + reload).
