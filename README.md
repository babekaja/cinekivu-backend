# CineKivu Backend

Backend Python/FastAPI de CineKivu pour le catalogue, la recherche, les épisodes,
les sous-titres et le relais de lecture MP4, DASH et HLS. Le catalogue et les
flux dépendent du fournisseur MovieBox et de sa disponibilité réseau.

Ce dépôt contient uniquement le backend ; les applications Android, iOS,
Desktop et Web sont dans le projet CineKivu principal.

## Installation

Python 3.11 ou ultérieur (vérifications effectuées avec Python 3.13) :

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Le client HTTP `curl_cffi` est inclus dans `requirements.txt` pour utiliser
le transport navigateur prévu par le moteur, y compris sur Render. Le fichier
`requirements-extras.txt` reste disponible pour les anciennes installations.
La santé `/api/v1/cinekivu/health` indique le transport réellement chargé dans
`data.provider_http_client`.

## Démarrage

Depuis la racine de ce dépôt :

```sh
python -m uvicorn moviebox_api_server:app --host 127.0.0.1 --port 8000
```

- Santé : `http://127.0.0.1:8000/health`
- Santé des modules de lecture : `http://127.0.0.1:8000/api/health`
- Documentation interactive : `http://127.0.0.1:8000/docs`

Sur un hébergeur ou pour un appareil du réseau local, écouter sur `0.0.0.0` et
utiliser le port attribué par l'hébergeur :

```sh
python -m uvicorn moviebox_api_server:app --host 0.0.0.0 --port "${PORT:-8000}"
```

## Render et lecture vidéo

Build command : `pip install -r requirements.txt`.
Start command : `sh start-render.sh`.
Health check : `/health`.

Le service conserve les jetons temporaires de lecture en mémoire : utiliser
un seul worker et une seule instance. Plusieurs workers sans registre partagé
peuvent renvoyer « Session vidéo expirée » juste après la résolution d'un flux.

Les routes publiques `/api/v1/cinekivu/*` et les relais `/api/play/*`,
`/api/adaptive/*`, `/api/download/*` n'initialisent pas de session personnelle.
Cela évite un appel d'authentification fournisseur supplémentaire avant de
récupérer une fiche, vérifier une source ou lire une plage vidéo. Les endpoints
de bibliothèque, compte et progression gardent leur session personnelle.

En cas de lecture impossible, examiner `data.issues`, `data.message` et
`data.playable_count` de `/api/v1/cinekivu/titles/{id}/streams`. Une réponse
HTTP 200 de `/health` prouve uniquement que le serveur est actif. La fiche
et la vidéo peuvent encore échouer si les fournisseurs ne répondent pas ou
refusent l'adresse de sortie de l'hébergeur. Ne pas publier les cookies CDN
ni des URL de lecture privées dans les logs ou les tickets.

Guides officiels : [FastAPI sur Render](https://render.com/docs/deploy-fastapi)
et [adresses réseau sortantes](https://render.com/docs/outbound-ip-addresses).

## Configuration et données

Les variables se définissent dans l'environnement du processus :

| Variable | Utilisation |
| --- | --- |
| `MOVIEBOX_DB_PATH` | Base SQLite de bibliothèque et progression ; défaut : `data/moviebox_ott.sqlite3`. |
| `CINEKIVU_FEEDBACK_DB` | Base SQLite des retours ; défaut : `data/cinekivu_feedback.sqlite3`. |
| `MOVIEBOX_SESSION_SECRET` | Secret de signature des sessions ; utiliser une valeur aléatoire d'au moins 32 caractères sur un hébergeur. |
| `CORS_ORIGINS` | Origines Web autorisées, séparées par des virgules. |
| `FRONTEND_URL` | Origine Web autorisée supplémentaire. |

Les bases SQLite et, en l'absence de variable de secret, `data/.session_secret`
sont créées automatiquement. Utiliser un disque persistant pour conserver les
données après un redémarrage de l'hébergement. Les données locales, secrets,
environnements Python et caches de tests sont exclus de Git.

## Cache du catalogue

- Configuration des filtres Films/Séries/Animés : cache partagé de 10 minutes.
- Listes de titres : cache de 30 secondes, limité à 128 pages et séparé par
  catégorie, page et filtres.
- Les appels identiques simultanés partagent une seule requête au fournisseur.
- Les erreurs et les URL de lecture ne sont pas mises en cache. Les caches
  sont conservés en mémoire et se vident au redémarrage.

## Fonctions issues des archives

- `/api/v1/recommender` : catalogue local de 5 000 films, recherche, genres,
  synopsis et recommandations TF-IDF/cosinus. [Contrat](docs/recommender-api.md).
- `/api/v1/reference` : 601 œuvres et 610 éditions de référence, avec filtres,
  taxonomie, collections, langues et traductions. [Contrat](docs/reference-api.md).
- `/api/v1/enrichment/search` : recherche de métadonnées anime/manga dans
  AniList et MangaDex. [Contrat](docs/enrichment-api.md).
- `/api/v1/downloads` : recherche par titre et année, qualités exactes,
  lots d'épisodes, fichiers nommés, sous-titres SRT/VTT et reprise HTTP.
  [Contrat](docs/downloads-sdk-api.md).

Les deux sources locales fonctionnent hors ligne, sans session fournisseur. Les
identifiants locaux sont distincts des identifiants de lecture MovieBox.
Le catalogue de référence est un relevé historique du 15 octobre 2026.

[Analyse des ZIP](docs/ANALYSE_ZIP_ET_AJOUTS.md) et
[validation des API](docs/validation-api.md).
La [passe complémentaire du SDK](docs/ANALYSE_SDK_MOVIEBOX_024.md) et sa
[validation du 8 novembre](docs/validation-sdk-api.md) détaillent les nouveaux téléchargements.
L'[audit complet des 134 opérations](docs/AUDIT_COMPLET_API.md) décrit les
corrections de compatibilité, les résultats Postman et les limites des fournisseurs.

## Validation après chaque changement d'API

Exécuter les tests Python ci-dessous puis la collection Postman contre le
backend actif. La collection est conservée dans ce dépôt et dans le plugin
Postman. Le runner cloud ne peut pas atteindre `127.0.0.1` : utiliser le
CLI officiel ou Postman Desktop sur la même machine.

`bash scripts/test_api.sh` enchaîne les tests Python et la collection avec
Postman CLI ou Newman. Le runner Newman installé dans `build/api-tools/` est
détecté automatiquement ; il peut aussi être fourni par `NEWMAN_BIN`.
Le script prépare aussi le serveur de test SDK isolé sur le port 8001 pour
contrôler les transferts HTTP et la reprise sans télécharger de film tiers.
Il isole les bases de test et produit la couverture des routes dans
`postman/reports/api-route-coverage.json`.

```sh
bash scripts/test_api.sh
```

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

Les tests utilisent des fournisseurs simulés et ne nécessitent pas de
connexion aux services MovieBox. Les scripts de `scripts/` sont des sondes
supplémentaires pour un serveur actif et peuvent contacter les fournisseurs.

## Organisation

- `moviebox_api_server.py` : application FastAPI principale.
- `cinekivu_web.py`, `cinekivu_native_catalogue.py` : catalogue et filtres.
- `cinekivu_metadata_cache.py` : cache de métadonnées partagé.
- `cinekivu_bridge.py`, `cinekivu_source/` : résolution et relais de lecture.
- `moviebox_api/` : client mobile et routes de bibliothèque.
- `fixtures/`, `tests/` : données et tests indépendants du réseau.

La licence MIT de la source Moviebox-API adaptée est conservée dans
[`cinekivu_source/LICENSE`](cinekivu_source/LICENSE), copyright 2026 Walter.
