# Audit complet des API — 8 novembre 2026

## Résultat

- **134/134 opérations OpenAPI exercées** : 131 avec au moins une réponse positive ; 3 routes volontairement désactivées répondent 410.
- **300 tests Python réussis**, fournisseurs et comptes simulés, bases SQLite isolées.
- **105 requêtes Postman et 214 assertions réussies**, aucun échec, en 24.8 s.
- **Audit HTTP réel : 136 contrôles, 130 réussis, 6 indisponibilités fournisseur, 0 échec local.**
- Dernière sonde réelle commencée à `2026-11-08T11:29:57.993201+00:00`. Backend actif sur `http://127.0.0.1:8000`.

Les indisponibilités externes ci-dessous restent des indisponibilités, même si les
tests de contrat vérifient correctement leurs réponses d'erreur. Le code de sortie
de l'audit est 2 lorsque seules ces indisponibilités subsistent.

## Corrections appliquées

- Les 10 genres stables de l'application utilisent maintenant les identifiants du schéma natif actuel, pour les films et les séries. Le genre Action rend de nouveau des titres. Si une catégorie échoue, les résultats disponibles sont conservés avec `partial: true` et `unavailableCategories` ; une panne des deux catégories reste une erreur explicite.
- Les suggestions historiques utilisent l'autocomplétion active et conservent leur liste de chaînes.
- Les commentaires de titre utilisent `GET /post/list/subject` avec `id`, une page fournisseur commençant à zéro et `perPage`.
- Les communautés utilisent la sélection native `group/list/trending-entrance` avec un corps vide et `GET /group/get`. La sélection n'est pas paginée ; les pages suivantes sont vides. La provenance `native-trending-communities` est explicite.
- Les sous-titres de film utilisent saison/épisode zéro. Si la fiche de type manque, une recherche vide par épisode est suivie d'un essai en coordonnées de film. Un fichier inexistant chez le CDN renvoie un 404 explicite.
- L'ancienne route `/stream` peut utiliser le moteur CineKivu et ses relais vérifiés. Elle conserve les refus 401/403/429. Une panne de sous-titres ne bloque pas une source vidéo vérifiée.
- L'ancien `/download` réutilise les MP4 vérifiés, les noms de fichier sûrs et le relais HTTP commun, avec Range et fermeture du transport.
- Les erreurs métier du fournisseur ne deviennent plus de faux succès sur les communautés, les commentaires ou les positions cloud contrôlées. Les formulaires de compte vides et les progressions négatives sont rejetés avant tout envoi.

## Ce qui a réellement été exécuté

La collection existante `41970206-39202388-c5d9-4d8f-8e8e-f0c042a2e169` a été
maintenue puis relue avec le **plugin Postman**. Sa copie complète est enregistrée
dans `postman/collections/cinekivu-regression.postman_collection.json`.
L'exécution localhost utilise **Newman 6.2.3** ; le plugin hébergé ne peut pas joindre
le Mac. Aucun run cloud sur localhost n'est revendiqué.

Les requêtes `baseUrl` visent le backend réel. Le catalogue, les recommandations,
les commentaires, les communautés, les filtres et les connecteurs AniList/MangaDex
sont sondés sans remplacer leurs fournisseurs. Les favoris, la progression et les
préférences sont écrits puis restaurés dans une session HTTP anonyme isolée.

Le dossier SDK utilise `sdkBaseUrl` sur le port 8001, avec la vraie application et
un fournisseur/CDN HTTP synthétique. Il vérifie les transferts intégraux, HEAD,
Range, If-Range, les sous-titres, la reprise stable et la nouvelle résolution après
suppression des jetons. Le script arrête uniquement la fixture qu'il a démarrée.

Les envois OTP, inscriptions, connexions et publications positives sont **simulés**
en Python. Les seuls appels réels de ces formulaires sont des données invalides
rejetées en 422. Aucun message ni compte réel n'est créé pour ces tests.

La sonde média réelle reçoit 32 octets par lecture vidéo et contrôle la signature
MP4 ; les fichiers de sous-titres sont bornés à 2 Mio et validés en WebVTT. Cette
sonde ne prouve pas le décodage complet sur iPhone, la disponibilité de tous les
titres ni la langue audio. Les chemins de médias restent opaques et les rapports
d'audit ne contiennent pas les URL signées des CDN.

## Indisponibilités observées

- `GET /groups/interactive` : HTTP 503, Flux communautaire interactif momentanément indisponible chez le fournisseur.
- `GET /api/v1/cinekivu/captions/{token}.vtt` — langue `fr` : HTTP 404, Fichier de sous-titres absent chez le fournisseur.
- `GET /api/v1/cinekivu/captions/{token}.vtt` — langue `ar` : HTTP 404, Fichier de sous-titres absent chez le fournisseur.
- `GET /api/v1/cinekivu/captions/{token}.vtt` — langue `inid` : HTTP 404, Fichier de sous-titres absent chez le fournisseur.
- `GET /api/v1/cinekivu/captions/{token}.vtt` — langue `pt` : HTTP 404, Fichier de sous-titres absent chez le fournisseur.
- `GET /api/v1/cinekivu/captions/{token}.vtt` — langue `zh` : HTTP 404, Fichier de sous-titres absent chez le fournisseur.

Le filtre natif Fantastique répond 200 mais ne fournit aucun résultat au moment de la sonde. Aucun titre de remplacement n’a été inventé.

Les routes `POST /launch-player`, `GET /debug_history` et `GET /sub-proxy` restent
désactivées volontairement (410). Les collections sans identifiant public
disponible et les actions authentifiées sont couvertes par les contrats simulés,
sans prétendre à une validation positive avec un compte réel.

## Reproduire

Backend actif sur 8000 :

```sh
bash scripts/test_api.sh --reporters cli,json,junit --reporter-json-export postman/reports/all-api-newman.json --reporter-junit-export postman/reports/all-api-newman.xml --timeout-request 60000
.venv/bin/python scripts/audit_all_api.py --workers 2 --media --output postman/reports/all-api-live-final.json
```

Rapports locaux ignorés par Git : `postman/reports/all-api-test-run.log`,
`all-api-newman.json`, `all-api-newman.xml`, `all-api-live-final.json`,
`api-route-coverage.json` et `sdk-fixture.log` dans ce même dossier.
Un avertissement Starlette/TestClient préexistant reste affiché. Il n'est associé
à aucun test en échec.

## Matrice des 134 opérations

« Tests isolés » désigne les fournisseurs simulés. « Backend réel » rassemble
les statuts observés dans l'audit et Postman sur le port 8000 : un 422 sur une
route de compte confirme uniquement sa validation d'entrée. Un tiret signifie
qu'aucun appel HTTP réel positif n'est revendiqué pour cette opération.

| Opération | Tests isolés : HTTP | Backend réel : HTTP |
|---|---|---|
| `GET /` | 200 | 200 |
| `GET /health` | 200 | 200 |
| `GET /api/health` | 200 | 200 |
| `POST /request-otp` | 200, 422 | 422 |
| `POST /login` | 200, 422 | 422 |
| `POST /register` | 200, 422 | 422 |
| `POST /logout` | 200 | — |
| `GET /user-info` | 200 | 200 |
| `GET /home` | 200 | 200 |
| `GET /anime` | 200 | 200 |
| `GET /rankings` | 200 | 200 |
| `GET /discovery` | 200 | 200 |
| `GET /trending` | 200 | 200 |
| `GET /movies` | 200 | 200 |
| `GET /short-tv` | 200 | 200 |
| `GET /kids` | 200 | 200 |
| `GET /education` | 200 | 200 |
| `GET /music` | 200 | 200 |
| `GET /asian` | 200 | 200 |
| `GET /western` | 200 | 200 |
| `GET /nollywood` | 200 | 200 |
| `GET /tab/{tab_id}` | 200 | 200 |
| `GET /game` | 200 | 200 |
| `GET /search-suggestions` | 200 | 200 |
| `GET /search` | 200 | 200 |
| `GET /rooms/recommend` | 200, 503 | 200 |
| `GET /rooms/{room_id}` | 200, 503 | 200 |
| `GET /detail/{subject_id}` | 200 | 200 |
| `GET /episodes/{series_id}` | 200 | 200 |
| `GET /stream/{subject_id}` | 200, 401, 403, 429 | 200 |
| `GET /download/{subject_id}` | 206 | 206 |
| `GET /subtitles/search` | 200 | 200 |
| `GET /subtitles/{subject_id}` | 200 | 200 |
| `GET /history/position` | 200, 502 | — |
| `POST /history/position` | 200, 422 | 422 |
| `POST /history/seen` | 200, 422 | 422 |
| `POST /analytics/operation` | 200 | — |
| `GET /history` | 200 | 200 |
| `GET /watchlist` | 200 | 200 |
| `POST /history/delete/{subject_id}` | 200 | — |
| `POST /watchlist/toggle` | 200 | — |
| `POST /history/progress` | 200 | — |
| `POST /launch-player` | 410 | 410 |
| `GET /post/count/{subject_id}` | 200, 502 | 200 |
| `GET /groups/trending` | 200, 503 | 200 |
| `POST /post/like` | 200 | — |
| `POST /post/create` | 200 | — |
| `GET /groups/interactive` | 200, 503 | 503 |
| `GET /post/list/{subject_id}` | 200 | 200 |
| `GET /debug_history` | 410 | 410 |
| `GET /sub-proxy` | 410 | 410 |
| `GET /api/v1/cinekivu/health` | 200 | 200 |
| `GET /api/v1/cinekivu/titles/{subject_id}` | 200 | 200 |
| `GET /api/v1/cinekivu/titles/{subject_id}/editions` | 200 | 200 |
| `GET /api/v1/cinekivu/titles/{subject_id}/episodes` | 200 | 200 |
| `GET /api/v1/cinekivu/titles/{subject_id}/subtitles` | 200 | 200 |
| `GET /api/v1/cinekivu/captions/{token}.vtt` | 200, 404, 413, 502 | 200, 404 |
| `GET /api/v1/cinekivu/titles/{subject_id}/streams` | 200, 422 | 200, 422 |
| `GET /api/download/by-id/{media_id}` | 200, 206, 422 | 206 |
| `GET /api/download/{token}/{media_id}` | 200, 206, 403, 404, 416 | 206 |
| `GET /api/play/{token}` | 206 | 206, 404 |
| `GET /api/adaptive/{token}/{resource}` | 200 | 404 |
| `GET /api/v1/config` | 200 | 200 |
| `GET /api/v1/home` | 200 | 200 |
| `GET /api/v1/catalog/{category}` | 200, 404 | 200 |
| `GET /api/v1/titles/{subject_id}` | 200 | 200 |
| `GET /api/v1/titles/{subject_id}/seasons` | 200 | 200 |
| `GET /api/v1/genres` | 200 | 200 |
| `GET /api/v1/genres/{genre_id}` | 200, 404, 502 | 200 |
| `GET /api/v1/search` | 200, 422 | 200 |
| `GET /api/v1/titles/{subject_id}/editions` | 200 | 200 |
| `GET /api/v1/suggestions` | 200 | 200 |
| `GET /api/v1/titles/{subject_id}/similar` | 200 | 200 |
| `GET /api/v1/collections/{playlist_id}` | 200 | — |
| `GET /api/v1/library` | 200 | 200 |
| `PUT /api/v1/library/{subject_id}` | 200, 422 | 200 |
| `DELETE /api/v1/library/{subject_id}` | 200 | 200 |
| `GET /api/v1/progress/{subject_id}` | 200 | 200 |
| `PUT /api/v1/progress/{subject_id}` | 200 | 200 |
| `DELETE /api/v1/progress/{subject_id}` | 200 | 200 |
| `GET /api/v1/continue-watching` | 200 | 200 |
| `GET /api/v1/history` | 200 | 200 |
| `GET /api/v1/preferences` | 200 | 200 |
| `PATCH /api/v1/preferences` | 200, 422 | 200 |
| `GET /api/v1/search/autocomplete` | 200, 422 | 200 |
| `GET /api/v1/titles/{subject_id}/recommendations` | 200 | 200 |
| `GET /api/v1/people/{person_id}/titles` | 200, 422 | 200 |
| `GET /api/v1/recommender/status` | 200, 503 | 200 |
| `GET /api/v1/recommender/genres` | 200 | 200 |
| `GET /api/v1/recommender/movies` | 200, 422 | 200, 422 |
| `GET /api/v1/recommender/movies/{movie_id}` | 200, 404, 422 | 200, 404 |
| `GET /api/v1/recommender/movies/{movie_id}/recommendations` | 200, 404, 422 | 200, 422 |
| `POST /api/v1/recommender/recommendations` | 200, 422 | 200, 422 |
| `GET /api/v1/reference/search` | 200, 422 | 200, 422 |
| `GET /api/v1/reference/taxonomy` | 200 | 200 |
| `GET /api/v1/reference/stats` | 200, 503 | 200 |
| `GET /api/v1/reference/categories` | 200 | 200 |
| `GET /api/v1/reference/genres` | 200 | 200 |
| `GET /api/v1/reference/formats` | 200 | 200 |
| `GET /api/v1/reference/collections` | 200 | 200 |
| `GET /api/v1/reference/languages` | 200 | 200 |
| `GET /api/v1/reference/locales/{locale}` | 200, 404 | 200 |
| `GET /api/v1/reference/sources` | 200 | 200 |
| `GET /api/v1/reference/works/{work_id}` | 200, 404 | 200, 404 |
| `GET /api/v1/reference/works/{work_id}/editions` | 200 | 200 |
| `GET /api/v1/reference/works/{work_id}/recommendations` | 200 | 200 |
| `GET /api/v1/reference/editions/{edition_id}` | 200, 404 | 200 |
| `GET /api/v1/reference/editions/{edition_id}/{section}` | 200, 422 | 200 |
| `GET /api/v1/enrichment/search` | 200, 422, 502, 503, 504 | 200, 422 |
| `GET /api/v1/downloads/capabilities` | 200 | 200 |
| `POST /api/v1/downloads/resolve` | 200, 404, 409, 422 | 200 |
| `POST /api/v1/downloads/batch` | 200, 404, 422 | — |
| `GET /api/v1/downloads/assets/{token}` | 200, 206, 404, 502 | 206 |
| `GET /api/v1/web/home` | 200, 502 | 200 |
| `GET /api/v1/web/titles/{subject_id}` | 200 | 200 |
| `GET /api/v1/web/home/rails/{rail_id}` | 200, 404, 422 | 200 |
| `GET /api/v1/web/trending` | 200 | 200 |
| `GET /api/v1/web/popular-searches` | 200 | 200 |
| `GET /api/v1/web/recommendations/{subject_id}` | 200, 422 | 200 |
| `GET /api/v1/web/rankings` | 200, 422 | 200 |
| `GET /api/v1/web/rankings/{ranking_id}` | 200, 422, 502 | 200 |
| `GET /api/v1/web/tabs/{tab_id}` | 200 | 200 |
| `GET /api/v1/web/navigation` | 200 | 200 |
| `POST /api/v1/web/filter` | 200, 422, 502 | 200, 422 |
| `GET /api/v1/web/filter/options` | 200, 502 | 200, 422 |
| `GET /api/v1/web/categories/{category}` | 200, 404, 502 | 200 |
| `GET /api/v1/web/snapshot/search` | 200, 502 | 200 |
| `GET /api/v1/web/feedback/{subject_id}` | 200 | 200 |
| `POST /api/v1/web/feedback` | 201 | 422 |
| `GET /api/v1/media/{subject_id}/overview` | 200 | 200, 422 |
| `GET /api/v1/media/{subject_id}/rating` | 200 | 200 |
| `GET /api/v1/media/{subject_id}/trailer` | 200 | 200 |
| `GET /api/v1/media/{subject_id}/cast` | 200 | 200 |
| `GET /api/v1/media/{subject_id}/reviews` | 200, 422, 502 | 200 |
