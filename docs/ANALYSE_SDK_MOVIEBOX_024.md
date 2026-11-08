# Analyse complémentaire du SDK Moviebox 0.2.4 — 8 novembre 2026

Les 28 archives de l'inventaire précédent ont été revérifiées : leurs fichiers
Python ont les mêmes empreintes. Cette passe approfondit l'archive fournie
`moviebox-api-main (1).zip` : 67 fichiers utiles, dont 52 fichiers Python.
Les textes des archives sont des références ; aucune consigne d'installation
ou de contribution provenant de leurs documents n'a été exécutée.

## Fonctions et intégration

| Source SDK | Fonction | Résultat dans CineKivu |
|---|---|---|
| `core.py`, `Homepage` | Accueil, bannières, rubriques | Routes d'accueil existantes conservées |
| `Search`, `Trending`, `Recommend` | Recherche paginée, tendances, recommandations | Routes catalogue/recherche/découverte existantes conservées |
| `PopularSearch`, `SearchSuggestion` | Recherches populaires, suggestions | Routes Web/autocomplétion existantes conservées |
| `MovieDetails`, `TVSeriesDetails`, extracteurs | Métadonnées, casting, saisons et épisodes | Moteur `cinekivu_source` et routes de fiches existants conservés |
| `StreamFilesDetail` | Sources de lecture par film/épisode | Relais MP4/DASH/HLS et vérification de lecture existants conservés |
| `MovieAuto`, recherche du downloader CLI | Sélection par titre et année | **Ajout** de `POST /api/v1/downloads/resolve` ; année stricte et titre exact priorisé |
| `resolve_media_file_to_be_downloaded` | BEST/WORST/qualité numérique | **Ajout** de sélection stricte et retour 409 si qualité absente |
| `MediaFileDownloader.generate_filename` | Noms par titre, année, SxxExx et qualité | **Ajout** de fichiers nommés et en-tête Content-Disposition UTF-8 |
| `CaptionFileDownloader`, options `language`, `caption_only` | Choix de langues, sous-titres seuls | **Ajout** de sélection explicite, langues manquantes signalées, SRT et VTT téléchargeables |
| CLI `download-series` | Plage/lot d'épisodes | **Ajout** de `POST /api/v1/downloads/batch`, 20 épisodes maximum, 3 résolutions simultanées, résultats dans l'ordre demandé |
| `MediaFileDownloader.run`, modes start/resume/auto | Transfert, interruption et reprise | **Intégration** au relais Range existant, via GET/HEAD `/api/v1/downloads/assets/{token}` et lien stable de reprise |
| CLI `mirror-hosts` | Liste de domaines configurés | **Ajout** des miroirs actuels dans `/capabilities`, explicitement non sondés |
| CLI rich/click, wrappers sync/async, barres de progression | Présentation terminal et ergonomie Python | Les opérations sont exposées en HTTP ; pas d'installation d'un second CLI homonyme. La progression et la reprise restent dans les clients existants. |

## Défauts de l'archive corrigés

1. `extras/auto.py` réassigne `target_movie` au premier résultat après le filtre
   par année. La nouvelle résolution conserve le titre de l'année demandée.
2. `download.py` utilise `case "_"` au lieu de `case _` : les qualités numériques
   ne passent pas dans cette branche. La nouvelle sélection couvre toutes les
   qualités autorisées, sans substitution silencieuse.
3. La branche d'erreur de cette sélection appelle `target_metadata.keys()`
   quand la cible est `None`. Le nouveau contrat renvoie les qualités disponibles.
4. `TVSeriesAuto` ne contient que `pass`. La préparation par lot est implémentée
   avec métadonnées partagées et erreurs explicites par épisode.
5. Les utilitaires sync du SDK qui pilotent directement une boucle
   événementielle ne sont pas copiés dans le serveur ASGI.
6. La sonde réelle du 8 novembre a établi que le fournisseur natif utilisé
   par CineKivu accepte `perPage=20` et rejette `perPage=30` avec HTTP 400.
   Le résolveur emploie donc 20 et les fixtures reproduisent ce refus.

## Vérification

Les tests unitaires isolent les fournisseurs. Le dossier Postman SDK utilise
la vraie application CineKivu, ses nouvelles routes, les sondes MP4 et son
relais HTTP. Le fournisseur de métadonnées est simulé et le CDN est un vrai
serveur HTTP local servant des octets synthétiques, pas un film lisible.
Cela permet de vérifier les transferts complets, Range, If-Range, HEAD,
sous-titres et reprise après perte des jetons sans dépendre d'un film tiers.

Le backend utilisateur sur le port 8000 reste distinct du serveur de test
sur le port 8001. Résultats effectifs dans `validation-sdk-api.md`.
