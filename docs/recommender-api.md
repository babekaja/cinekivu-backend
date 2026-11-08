# Recommandations locales — contrat API

Ajout issu de `moviebox-master.zip` (Moviebox, Mario et Klaudio Sinani).
Le corpus contient 5 000 films et leurs synopsis anglais. Ses identifiants
numériques sont locaux : ils ne sont jamais des `subjectId` MovieBox.
Une recommandation ne garantit ni disponibilité vidéo, ni doublage français.

Toutes les réponses réussies utilisent `{"code":0,"data":…}` et indiquent
`source: "moviebox-corpus"`, `streamAvailability: "unknown"`.
Ces routes publiques sont sans session et ne contactent aucun fournisseur.

| Méthode | Route | Comportement |
|---|---|---|
| GET | `/api/v1/recommender/status` | Taille du corpus, langue, état de l'index |
| GET | `/api/v1/recommender/genres` | Genres et nombre de films |
| GET | `/api/v1/recommender/movies` | Liste/recherche par titre, filtre `genre`, `q` (120 caractères maximum), `page` ≥ 1, `page_size` entre 1 et 100, `include_plots` (false par défaut) |
| GET | `/api/v1/recommender/movies/{movie_id}` | Fiche et synopsis ; 404 si absent, 422 si identifiant négatif ou non numérique |
| GET | `/api/v1/recommender/movies/{movie_id}/recommendations` | De 1 à 30 résultats (`limit`, défaut 3), `include_plots` facultatif ; le film de départ est toujours exclu |
| POST | `/api/v1/recommender/recommendations` | JSON `{ "text": "space exploration on Mars", "limit": 3, "include_plots": false }` ; synopsis libre de 3 à 10 000 caractères non blancs |

Chaque film porte `movieId`, `catalogId: "moviebox-corpus:<id>"`, `title` et
`genres`. `plot` n'est inclus que sur demande ou dans la fiche. Les résultats
de recommandations ajoutent `similarity` entre 0 et 1, triée par ordre
décroissant. Les égalités sont triées par identifiant ; aucun résultat sans
similarité positive n'est inventé. Un texte sans mots connus renvoie une liste
vide, avec succès. Les pages au-delà de la fin sont également vides.

Le calcul emploie TF-IDF sur mots et paires de mots, puis la similarité cosinus.
L'index partagé est chargé à la première recommandation et réutilisé. Il ne
construit pas la matrice dense 5 000 × 5 000 de l'ancien programme et n'exige
pas ses dépendances de 2018. Une ressource manquante ou invalide produit 503.

La CLI équivalente : `python -m cinekivu_recommendations --help`.
La licence du logiciel original et la provenance des données sont conservées
dans `resources/moviebox/`.
