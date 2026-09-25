# Wallia — déploiement (fichiers préparés, application par le principal)

Le prototype ne publie rien sur Internet : l'API écoute uniquement sur
`127.0.0.1:13745`. Les fichiers de ce dossier sont des **modèles à adapter** aux
conventions constatées sur le VPS ; l'implémenteur n'a modifié ni `/etc/nginx`,
ni `/etc/letsencrypt`, ni systemd.

## 1. Vhost Nginx

1. Inspecter les vhosts existants pour reprendre les conventions (chemins de
   certificats, hooks, includes, logs) : `ls /etc/nginx/sites-enabled/`.
2. Copier `deployment/nginx-wallia.conf` en remplaçant domaine et chemins de
   certificats, puis `nginx -t` et `systemctl reload nginx` **après** backup du
   fichier modifié (procédure infra en vigueur).
3. Le vhost proxifie vers `127.0.0.1:13745`, désactive la mise en tampon (SSE),
   interdit `/internal/` depuis l'extérieur et limite la taille des requêtes.

## 2. TLS

- Réutiliser la paire gérée active du VPS pour `wallia.valdev.me` (ou en créer
  une selon le mécanisme existant).
- Adapter `deployment/tls-hook.sh` (nom du certificat, chemin, rechargement) et
  l'intégrer au hook de renouvellement déjà en place.
- Vérifier sans `-k` : `curl -sS -o /dev/null -w '%{http_code}\n' https://wallia.valdev.me/healthz`.

## 3. Bascule de l'application en production

Créer `runtime/secrets/app.production.env` (0600) à partir de `runtime/secrets/app.env` :

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

```
scripts/deploy.sh --env-file runtime/secrets/app.production.env
```

Le script refuse un arbre sale ou un HEAD non poussé (mode livraison) et
n'effectue aucune modification système.

## 4. Clé du fournisseur applicatif

- Endpoint natif attendu : `https://api.deepseek.com/v1`, modèle `deepseek-flash`.
- Déposer la clé **soit** dans `runtime/secrets/provider_api_key` (0600, uid 1002),
  **soit** via l'interface Administration → « Connexion modèle » (écriture
  write-only, jamais relue ni affichée).
- Sans clé : l'application reste en mode démonstration honnête (recherche réelle,
  pas de texte généré par un modèle).
- Tester avec le bouton « Tester la connexion » puis vérifier `/api/status`.

## 5. Retour arrière

1. `docker compose -p wallia down` puis relancer l'image précédente
   (`WALLIA_IMAGE=wallia:<sha>` dans le fichier d'environnement).
2. La base est conservée dans le volume `wallia_pgdata` ; sauvegardes :
   `scripts/backup.sh`, restauration testée : `scripts/restore.sh --isolated <dump>`.
3. Nginx : restaurer le vhost sauvegardé puis `nginx -t` + reload.
