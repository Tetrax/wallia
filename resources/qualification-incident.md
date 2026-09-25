# Qualification d'incident — méthode

Objectif : transformer un signalement en un cas exploitable, sans inventer.

1. **Produit et version.** Identifier le produit et la version exacte en cause. Si un des deux
   est inconnu, le dire et le demander ; ne jamais supposer une version.
2. **Symptôme.** Reformuler en une phrase observable : qui, quoi, quand, où, depuis quand.
3. **Faits établis vs hypothèses.** Les faits proviennent de l'utilisateur ou d'un contrôle
   réellement exécuté. Tout le reste est hypothèse et doit être présenté comme tel.
4. **Contrôles proposés ≠ réalisés.** Un contrôle proposé reste « proposé » tant que son
   résultat n'a pas été rapporté.
5. **Prochain contrôle utile.** En proposer un seul, réalisable, réversible, et expliquer
   ce qu'on apprendra selon le résultat.
6. **Impact.** Qualifier : bloquant, dégradé, ou sans impact utilisateur.

Règles : ne jamais promouvoir une hypothèse en fait sans résultat ; ne jamais requalifier un
élément contradictoire sans le signaler explicitement ; garder la provenance de chaque élément
(message utilisateur, saisie explicite, contrôle exécuté).
