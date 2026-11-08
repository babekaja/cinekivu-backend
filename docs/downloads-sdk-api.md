# Fonctions de téléchargement issues du SDK Moviebox 0.2.4

Préfixe : `/api/v1/downloads`. Les réponses JSON utilisent `code/data`.
Les fichiers passent par le relais MP4 déjà présent : seules les sources
MP4 enregistrées après vérification Range sont proposées. Les URLs CDN,
cookies et en-têtes fournisseur restent côté serveur.

## Contrat

- `GET /capabilities` : qualités, modes HTTP, formats de sous-titres, limite
  de lot et miroirs actuellement configurés (sans prétendre les avoir sondés).
- `POST /resolve` : préparer un film/épisode avec soit `subject_id`, soit
  `query`, plus `subject_type` (1 film, 2 série), `year` facultatif,
  `season`/`episode` (1 par défaut), `quality` (`BEST`, `WORST`, `360p`,
  `480p`, `720p`, `1080p`, `1440p`, `2160p`), `caption_languages` (0 à 5
  codes/langues), `caption_format` (`vtt` ou `srt`) et `captions_only`.
- `POST /batch` : lot d'épisodes d'une série via `subject_id`, `season` et
  soit `episodes` (1 à 20 numéros distincts), soit `start_episode`/`limit`.
  Les options de qualité et sous-titres sont identiques. Le lot conserve
  l'ordre demandé et renvoie chaque résultat ou erreur, avec `readyCount`
  et `partial`. Les métadonnées de la série et ses saisons sont chargées une fois.
- `GET/HEAD /assets/{token}` : télécharger un MP4 ou un sous-titre avec son
  nom généré. Le MP4 relaie `Range`, `If-Range`, ETag et Content-Length.

`resolve` renvoie le titre retenu, `availableQualities`, le fichier MP4
(`url`, `resumeUrl`, `filename`, `mediaId`, `quality`) et les sous-titres.
`caption_languages` vide évite tout appel fournisseur de sous-titres.
Une piste manquante figure dans `missingCaptionLanguages` ; aucune autre
langue n'est substituée. Le code exact est privilégié, puis une variante de
la même langue. `language` conserve le code effectivement déclaré ;
`requestedLanguage` indique la variante demandée, sans prétendre la vérifier.
`captions_only` exige une langue et au moins une
piste correspondante, sans résoudre la vidéo.

L'année demandée est un filtre strict, y compris quand le SDK d'origine
aurait repris le premier résultat. Sans année, le titre exact est priorisé,
puis la préférence linguistique existante. La recherche utilise les 20 résultats
de la première page native : le fournisseur actuel rejette `perPage=30`.
Les qualités numériques sont
également strictes : qualité absente = HTTP 409 avec qualités disponibles.
Les identifiants et paramètres invalides donnent 422 ; absence de titre,
saison ou jeton donne 404 ; panne fournisseur donne 502.

Les liens nommés `/assets/` expirent après 30 minutes. Le `resumeUrl` MP4
existant, fondé sur `mediaId`, permet de renouveler la source après expiration.
Les fichiers sont transférés au client ; ils ne sont pas stockés sur le disque
du backend. La pause consiste à fermer la requête ; la reprise utilise Range.

Exemple :

```json
{"query":"Avatar","year":2009,"quality":"720p","caption_languages":["fr","en"],"caption_format":"srt"}
```

## Écarts corrigés par rapport au ZIP

Le SDK contient un `case "_"` littéral au lieu du cas par défaut pour les
qualités numériques, une variable cible réassignée après le filtre d'année,
et une classe `TVSeriesAuto` vide. L'intégration n'installe pas ce paquet
homonyme par-dessus le module local `moviebox_api` et n'importe pas ces bugs.
