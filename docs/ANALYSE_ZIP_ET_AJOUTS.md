# Analyse des archives et ajouts API

Analyse du 7 novembre 2026.

Complément du 8 novembre : les empreintes Python des 28 archives sont inchangées.
Les téléchargements du SDK ont été intégrés et vérifiés, y compris en réel :
voir [l'analyse complémentaire](ANALYSE_SDK_MOVIEBOX_024.md) et
[les résultats de validation](validation-sdk-api.md).

28 archives pertinentes inventoriées : 27 dans Téléchargements et le ZIP du projet courant.
Les 15 archives CV_BACKEND contiennent au total 1500 CV PDF, sans code backend. Seule leur structure a été inspectée.

## Inventaire

| Archive | Fichiers utiles | Python | Routes déclarées |
|---|---:|---:|---:|
| CV_BACKEND_01.zip | 100 | 0 | 0 |
| CV_BACKEND_02.zip | 100 | 0 | 0 |
| CV_BACKEND_03.zip | 100 | 0 | 0 |
| CV_BACKEND_04.zip | 100 | 0 | 0 |
| CV_BACKEND_05.zip | 100 | 0 | 0 |
| CV_BACKEND_06.zip | 100 | 0 | 0 |
| CV_BACKEND_07.zip | 100 | 0 | 0 |
| CV_BACKEND_08.zip | 100 | 0 | 0 |
| CV_BACKEND_09.zip | 100 | 0 | 0 |
| CV_BACKEND_10.zip | 100 | 0 | 0 |
| CV_BACKEND_11.zip | 100 | 0 | 0 |
| CV_BACKEND_12.zip | 100 | 0 | 0 |
| CV_BACKEND_13.zip | 100 | 0 | 0 |
| CV_BACKEND_14.zip | 100 | 0 | 0 |
| CV_BACKEND_15.zip | 100 | 0 | 0 |
| CineKivu-Angular-V12-Accueil-Decouverte-Source.zip | 194 | 1 | 0 |
| CineKivu-Lottie-26-Animations-Couleurs-V20.zip | 108 | 0 | 0 |
| CineKivu-V28-Splash-Lottie-Toutes-Plateformes.zip | 750 | 86 | 141 |
| CineKivu-V36-iOS-Native-Liquid-Glass (1).zip | 444 | 79 | 128 |
| CineKivu-V36-iOS-Native-Liquid-Glass 2.zip | 444 | 79 | 128 |
| CineKivu-V36-iOS-Native-Liquid-Glass.zip | 444 | 79 | 128 |
| CineKivu-V40-Android-Natif-Navigation-PredictiveBack-Tablettes.zip | 484 | 85 | 128 |
| Moviebox-API-main.zip | 10 | 2 | 10 |
| backend-v3.zip | 53 | 44 | 116 |
| cinekivu-backend-main.zip | 54 | 44 | 116 |
| moviebox-api-main (1).zip | 67 | 52 | 0 |
| moviebox-master.zip | 35 | 10 | 0 |
| backend-v3.zip (projet courant) | 53 | 44 | 116 |

Les nombres de routes sont des déclarations statiques, pas une affirmation que tous les serveurs archivés sont exécutables ou leurs fournisseurs disponibles.

## Comparaison et décision

| Famille | Fonctionnalités | Résultat |
|---|---|---|
| moviebox-master | Recherche/liste par ID, synopsis, recommandations TF-IDF/cosinus, CLI | Portées dans `cinekivu_recommendations.py`, 6 routes et CLI JSON. Corpus de 5 000 films conservé. |
| V28, référence V5/V6 | Classification, filtres avancés, éditions, pistes, saisons/épisodes/volumes/chapitres, langues, i18n, statistiques, recommandations par genres | Portées dans `cinekivu_reference.py`, 15 routes. 601 œuvres, 610 éditions. Données historiques signalées, séparées du catalogue actuel. |
| V28/V36/V40 et backend-v3 | Catalogue courant, recherche/autocomplétion, fiches, acteurs, recommandations fournisseur, notes/avis, bibliothèque, progression, préférences, filtres | Déjà présents dans le backend actuel. Aucun remplacement par une version ancienne. |
| Moviebox-API-main | Résolution MP4/DASH, sous-titres, éditions audio, relais vidéo, HLS et Range | Le moteur adapté est déjà présent dans `cinekivu_source/`, `cinekivu_bridge.py` et les routes de livraison. |
| moviebox-api-main (1), SDK 0.2.4 | Recherche, détails, choix de qualité, sous-titres, téléchargements avec reprise/concurrence, commandes CLI | Équivalents déjà répartis entre le backend et les clients natifs. Le SDK homonyme n’est pas installé par-dessus `moviebox_api/`. |
| SDK MovieAuto | Recherche/téléchargement automatique | Défaut détecté : le film sélectionné par année est réassigné au premier résultat. Non importé tel quel ; le choix explicite du titre actuel est conservé. |
| SDK TVSeriesAuto | Classe sans implémentation (`pass`) | Non fonctionnel, non ajouté comme fonctionnalité. |
| V5/V6 : outils et démos | Recherche de métadonnées anime/manga et lecteur synthétique de 9 secondes | AniList et MangaDex intégrés dans `/api/v1/enrichment/search`, vérifiés par des appels HTTP réels. Jikan non activé : délai de connexion dépassé pendant la sonde. Les démos restent des fixtures, pas des films supplémentaires. |
| Angular V12 / Lottie V20 / interfaces V28–V40 | Interfaces et ressources des clients | Aucune nouvelle route backend ; les sources des applications actuelles sont préservées. |

## Compatibilité

- Les routes existantes sont conservées. La modification préexistante de `cinekivu_web.py` est préservée.
- Les nouveaux préfixes `/api/v1/recommender` et `/api/v1/reference` fonctionnent sans session ni requête au fournisseur.
- Les identifiants `movieId`, `WRK-*`, `EDT-*` et `subjectId` ne sont jamais assimilés.
- Les recommandations locales ne deviennent pas une déclaration de disponibilité vidéo ou de doublage.
- Aucun ancien script d’installation d’archive n’a été exécuté.

## Contrats et tests

Voir `docs/recommender-api.md`, `docs/reference-api.md`, `docs/enrichment-api.md` et la collection `postman/collections/cinekivu-regression.postman_collection.json`.

Les résultats de validation effectifs sont enregistrés dans `docs/validation-api.md` après exécution.
