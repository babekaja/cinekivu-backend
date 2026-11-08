# Validation des ajouts — 7 novembre 2026

Ce rapport conserve les résultats du 7 novembre. La validation la plus récente
du SDK et du backend est dans [validation-sdk-api.md](validation-sdk-api.md)
(8 novembre : 204 tests Python, 72 requêtes et 152 assertions Postman).

Backend démarré sur `http://127.0.0.1:8000`, avec les trois nouveaux modules
chargés. Les changements existants de `cinekivu_web.py` ont été préservés.

## Tests Python

Commande : `.venv/bin/python -m pytest tests -q`

Résultat : **174 tests réussis**, code de sortie **0**. Cela comprend les
112 tests précédents et 62 tests ajoutés : recommandations/corpus (23),
référence (27), métadonnées externes et erreurs de transport (12).

Les tests de fournisseur simulent les réponses au niveau du transport HTTP.
Ils vérifient notamment les paramètres envoyés, le cache et la traduction
des erreurs 429, 500, JSON/GraphQL invalide et timeout. Ils n'établissent pas
la disponibilité des services externes ; celle-ci est contrôlée séparément.

Un avertissement de dépréciation de Starlette concernant TestClient/httpx
est présent ; aucun test n'échoue.

## Collection Postman et API active

Collection existante préservée et enrichie via le plugin Postman :
`41970206-39202388-c5d9-4d8f-8e8e-f0c042a2e169`.

Les 15 requêtes précédentes sont conservées ; 29 nouvelles requêtes et leurs
assertions ont été ajoutées. Le contenu complet a été relu depuis le plugin
et enregistré dans `postman/collections/cinekivu-regression.postman_collection.json`.

Exécution effective par **Newman 6.2.3**, le runner de collections Postman,
contre le serveur local : **44 requêtes réussies, 93 assertions réussies,
0 échec**, code de sortie **0**, durée **14,3 secondes**.

La première construction de l'index TF-IDF sur les 5 000 films a pris environ
8,2 secondes dans ce run ; les recommandations suivantes ont répondu en
quelques dizaines de millisecondes. L'index est partagé en mémoire et reconstruit
après redémarrage. Ces mesures décrivent une exécution locale, pas un benchmark.

Le plugin connecté expose la gestion des collections mais pas `runCollection`
dans les outils effectivement disponibles. Un runner cloud ne peut pas joindre
`127.0.0.1`. Les tests ont donc été exécutés sur le Mac avec Newman, à partir
de la collection synchronisée. Le gros téléchargement du CLI Postman a été
arrêté après la réussite de Newman ; aucune exécution de ce binaire partiel
n'est présentée comme un test réussi. Aucun workspace CLI n'a été initialisé,
et `postman status` n'est pas disponible sur ce chemin de validation.

Rapports détaillés locaux (ignorés par Git) :
`postman/reports/newman.json`, `postman/reports/newman.xml`.

## Vérifications réelles des connecteurs

Les routes `/api/v1/enrichment/search` ont interrogé AniList et MangaDex avec
la recherche Naruto et une limite de 1. Les deux ont répondu HTTP 200 avec un
résultat normalisé. La collection a ensuite vérifié ces routes avec le cache
chaud ; cela ne constitue pas un second appel réseau aux fournisseurs.

La sonde directe Jikan a dépassé le délai de connexion de 12 secondes.
Ce connecteur n'est donc pas activé. Les sondes ne prouvent pas la lecture
d'une vidéo ni la disponibilité d'un chapitre manga.

## Relancer

Le runner Newman est installé localement dans `build/api-tools/` (ignoré
par Git). Avec le backend actif :

```sh
bash scripts/test_api.sh
```

Le script exécute les tests Python puis la collection. Il détecte Postman CLI,
Newman dans PATH ou le runner local ; `POSTMAN_BIN`, `NEWMAN_BIN` et
`PYTHON_BIN` permettent de préciser les exécutables. Le fichier `AGENTS.md`
conserve la consigne de tester l'API après les prochains changements.

Les métadonnées et les validations sont contrôlées. La lecture vidéo complète
sur les appareils Android/iOS et les fournisseurs de streaming n'a pas été
rejouée dans cette validation : les tests de lecture existants utilisent leurs
fixtures habituelles.
