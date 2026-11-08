# Investigation de MobLand sur Render — 8 novembre 2026

Comparaison effectuée sur le même identifiant `4335165664987072680`, saison 1,
épisode 1, chemin `mobland-wWHa6Cu6fa5` fourni par le catalogue local.

| Vérification | Local | Render |
| --- | --- | --- |
| Recherche MobLand | HTTP 200 | HTTP 502, sources de recherche indisponibles |
| Résolution S1E1 | HTTP 200, 2 MP4 | HTTP 200, 0 source, diagnostics vides avant correctif |
| Lecture de 32 octets du MP4 1080p | HTTP 206, signature `ftyp`, plage valide | Aucun flux à relayer |

Le contrôle des octets est borné : il valide le début du fichier et le transport
HTTP, pas le décodage intégral du film. Le correctif précédent des sessions
publiques est bien déployé : la santé CineKivu sur Render n'émet plus de cookie
`session_id`.

La différence apparaît avant le lecteur : Render ne récupère pas les mêmes
sources auprès des fournisseurs. Ces seuls résultats ne permettent pas de
conclure si l'origine refuse l'adresse de sortie Render, renvoie une erreur
métier ou ne répond pas. La recherche renvoyant aussi 502, le CDN n'est pas
encore identifié comme l'unique cause.

## Diagnostics de résolution

`GET /api/v1/cinekivu/titles/{id}/streams` renvoie maintenant
`data.lookup_diagnostics` : fournisseur, nom d'hôte Web, statut HTTP, code métier
numérique, nombre de flux ou classe d'erreur. Les cookies, jetons, corps bruts
et adresses CDN signées ne sont jamais inclus dans ce relevé.

Si aucune source n'a été obtenue, `data.issues` explique les échecs recensés au
lieu de les masquer derrière une absence générique de vidéo. Le lecteur partagé
existant affiche ces messages. Le repli `play-info/v2` vers `play-info` reste
possible lorsqu'une route renvoie un refus métier ; un refus n'est plus traité
comme une réponse vide réussie.

Exemple public à refaire après déploiement :

```sh
curl --get 'https://cinekivu-backend.onrender.com/api/v1/cinekivu/titles/4335165664987072680/streams' \
  --data-urlencode 'subject_type=2' --data-urlencode 'season=1' \
  --data-urlencode 'episode=1' --data-urlencode 'detail_path=mobland-wWHa6Cu6fa5'
```

Render documente ses [adresses de sortie](https://render.com/docs/outbound-ip-addresses)
et les [interruptions de connexions sortantes](https://render.com/docs/outbound-connection-resets).
Ces documents expliquent des différences possibles avec le réseau local ; ils
ne prouvent pas un blocage du fournisseur pour cet épisode.

## Validation du correctif

326 tests Python et 222 assertions Postman (107 requêtes) réussis sur la version finale. Les fournisseurs sont simulés dans les tests unitaires ; la collection mélange lectures réelles et transport SDK synthétique isolé. La vérification Render après déploiement reste distincte.
