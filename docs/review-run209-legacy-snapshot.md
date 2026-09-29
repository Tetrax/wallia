# Run209 — rattachement contrôlé du runtime historique

Préparation par le principal, sans arrêt/redémarrage ni modification des conteneurs. Ce n'est ni un déploiement ni une recette de rollback exécutée.

Le Compose de `11265a5b8ac14ed001eda8c9c806006d03b80cdb` et `docker-compose.tests.yml`, rendus avec le répertoire de projet canonique et l'environnement existant, reproduisent EXACTEMENT les trois `com.docker.compose.config-hash` observés sur les conteneurs :

- API : `1a89081d2e38f66a990ebc2f67a6aa110054596228b816bf9b6281252228b949`
- worker : `a9f383622c63059c5374eb652ecd301bd8c4c4cadbc9c229c491468417b6d9ab`
- DB : `4afac7067497c595ed1199c53f74bac8109c9c627b0de17289878c930d5353e8`

Image effective API/worker : `sha256:73928914ea411f6c775022f6f6c3edd3dd538f021402e25898cb708401b82adc`. Son label de révision est `c11842fff201451e952a7cba94a7e0d517116233`, PAS le HEAD courant. Le backend est monté depuis le workspace : ce label ne décrit donc pas le code Python effectif, mutable. Cette limitation historique reste explicite.

L'ancien rendu a ensuite été conservé sous `runtime/deploy-state/run209-legacy.compose.json`, en remplaçant uniquement la référence d'image API/worker par cet ID immuable. Vérification JSON des deux IDs : `true` ; le hash DB reste identique. Les mounts/env de l'ancien rendu sont conservés, pas reconstruits depuis le nouveau Compose de livraison. L'environnement a sa copie dédiée `run209-legacy.env`, jamais affichée.

`current.json` a été créé seulement après contrôle de son absence, avec le writer atomique existant, puis relu et validé par `delivery_state.py validate-refs` (exit0). Répertoire0700, état/rendu/env0600 vérifiés. Aucun secret dans cette note. Un futur rollback conserve les binds historiques mutables : il ne constitue pas à lui seul une restauration du contenu du workspace. La livraison finale devra supprimer ces overrides de test et embarquer le code dans l'image publiée.
