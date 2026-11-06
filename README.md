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

Le client HTTP optionnel `curl_cffi` peut être installé avec
`python -m pip install -r requirements-extras.txt`.

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

## Tests

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
