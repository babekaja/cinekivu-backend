# Recherche de métadonnées anime et manga

`GET /api/v1/enrichment/search?provider=anilist&kind=anime&q=Naruto&limit=5`

- `provider` : `anilist` ou `mangadex` ; `kind` : `anime` ou `manga`.
- MangaDex prend uniquement `kind=manga` (422 pour anime).
- `q` : titre de 2 à 120 caractères non blancs ; `limit` : 1 à 5.
- Réponse : `code/data`, avec `provider`, `kind`, `query`, `items`,
  `streamAvailability: "unknown"`. Les identifiants sont ceux du fournisseur
  de métadonnées, jamais des identifiants de lecture MovieBox.
- Erreurs : 422 pour les paramètres, 504 pour un délai réseau dépassé,
  503 pour une limitation fournisseur, 502 pour une réponse externe invalide.
- Cache partagé de dix minutes, borné à 128 recherches, sans mémoriser les
  erreurs ; appels identiques simultanés regroupés.

Ces deux connecteurs proviennent de l'outil de recherche V28. Leurs sondes
réelles ont répondu HTTP 200 le 7 novembre 2026. Jikan n'est pas activé : la
sonde n'a pas établi de connexion dans le délai de douze secondes.

Aucun chapitre, vidéo ou piste audio n'est téléchargé par cette recherche.
Les résultats portent leurs liens et attributions d'origine. Références :
[AniList Media](https://docs.anilist.co/guide/graphql/queries/media),
[MangaDex API](https://api.mangadex.org/docs/).
