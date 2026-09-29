# Conception de livraison TLS — relevé Astra

Inspection réelle avant livraison, 2026-09-25. Pas encore de mutation Nginx/TLS.

- Nginx partagé actif, syntaxe valide. Aucun vhost/certificat Wallia.
- Exemple installé `/etc/nginx/sites-available/hub.valdev.me` : HTTP ACME webroot dédié, redirection ; HTTPS allow 127.0.0.1/::1 avant include `/etc/nginx/conf.d/00-application-access.conf`, proxy X-Forwarded-For écrasé par `$remote_addr`, clé/cert hors lignée LE sous `/var/lib/hub/certificates/active/`.
- Hook Hub délègue à un helper applicatif doté d'un service root ; Wallia n'a pas besoin d'un daemon/admin de certificats. Préférer un petit script opérateur root dédié, versionné, installé root-owned, activant seulement le domaine Wallia avec validations/bascule atomique/test/reload/rollback. Pas de socket root exposée à l'app.
- `certbot.service` prend déjà le verrou `/home/tetrax/workspace/.locks/valdev-infra.lock`. Les hooks existants (Hub et JOX) précisent NE PAS reprendre ce verrou dans le hook. Un CLI manuel acquiert le verrou à l'extérieur ; hook appelé sous verrou préexistant. Éviter tout deadlock de renouvellement.
- Couvrir le nom par une comparaison explicite de sortie openssl checkhost (son code sortie ne prouve pas toujours le match). Vérifier validité, clé publique correspondante, empreinte ; copie root0600, génération immuable, symlink actif relatif ; nginx -t puis reload et vérification du certificat réellement servi avec SNI. Bounded retry pour laisser les vieux workers Nginx sortir ; rollback lien + test/reload en cas d'échec.
- Initialisation : vhost HTTP seulement -> probe ACME local -> Certbot webroot sous verrou -> paire LE temporairement servie valide -> paire gérée contenant même cert -> vhost final actif. Sauvegarde avant toute substitution de vhost ; aucune mutation des autres services/protections.
- Nginx SSE : proxy_buffering off, cache off, X-Accel-Buffering no via app, timeouts cohérents ; upload limité, schéma HTTPS transmis.
- Pas de `certbot renew --dry-run` global non ciblé pendant la mission : viser `--cert-name wallia.valdev.me` pour ne pas solliciter tous les domaines. Hook manual uniquement avec lineage Wallia.

Ce document transmet les observations au prochain lot DeepSeek chargé d'ajuster les scripts TLS ; Astra n'implémente pas ces scripts.
