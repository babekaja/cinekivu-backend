# Validation SDK et API — 8 novembre 2026

## Résultat final

- **204 tests Python réussis**, code de sortie 0.
- **72 requêtes Postman, 152 assertions réussies, aucun échec**, code de sortie 0.
- **Sonde réelle réussie : Avatar (2009), 1080p, HTTP 206, 32 octets reçus**,
  signature MP4 `ftyp` vérifiée. Aucun film complet n'a été téléchargé.
- Backend utilisateur actif sur `http://127.0.0.1:8000`. Le serveur SDK de test
  sur le port 8001 a été démarré puis arrêté automatiquement par le script.

## Ajouts testés

Le nouveau module `cinekivu_downloads.py` ajoute la résolution d'un film ou
épisode par titre/année ou identifiant, le choix BEST/WORST/qualité exacte,
les lots d'épisodes, les fichiers nommés, les sous-titres SRT/VTT, HEAD et la
reprise HTTP. Trente tests unitaires s'ajoutent aux 174 précédents.

Les cas couvrent notamment : année qui ne correspond pas au premier résultat,
qualité absente sans substitution, sous-titres seuls sans appel vidéo, langue
manquante, langue régionale demandée distincte du code effectivement déclaré,
lot partiellement disponible, validation avant appel réseau, expiration et
borne du registre d'assets, conversion SRT et relais Range/If-Range.

## Postman connecté et HTTP réel

La collection existante est maintenue via le **plugin Postman**, identifiant
`41970206-39202388-c5d9-4d8f-8e8e-f0c042a2e169`. Ses 44 requêtes précédentes
sont préservées. Le nouveau dossier « SDK 0.2.4 — téléchargement HTTP et
reprise » ajoute 28 requêtes et 59 assertions.

La collection complète a été relue depuis le plugin, puis exécutée localement
avec Newman 6.2.3. Le dernier run dure **8,445 secondes**, avec 0 échec.
Le plugin hébergé ne fournit pas de runner local utilisable ; aucun run cloud
sur localhost ni `postman status` n'est revendiqué. Aucun autre workspace CLI
n'a été initialisé.

Les requêtes habituelles et les capacités SDK visent le backend actif via
`baseUrl=http://127.0.0.1:8000`. Les scénarios SDK visent
`sdkBaseUrl=http://127.0.0.1:8001`, la vraie application CineKivu avec un
fournisseur de métadonnées simulé et un CDN HTTP local d'octets synthétiques.
Le code de résolution, les sondes MP4 et le relais de livraison s'exécutent
réellement ; aucun mode de test ne remplace les fournisseurs du serveur utilisateur.

Ce dossier vérifie les fichiers et les en-têtes reçus : transfert intégral,
HEAD, Range initial, reprise exacte du reste, reconstitution du fichier,
If-Range différent qui redonne le fichier complet, suffixe Range, plages
multiples refusées et sous-titres SRT nommés. Après suppression des registres
de jetons, la reprise stable est vérifiée ainsi que le compteur d'appels :
une nouvelle résolution fournisseur et au moins quatre requêtes CDN ont
bien eu lieu. Le fichier synthétique est une fixture de transport, pas une
validation de décodage vidéo.

Les deux connecteurs externes AniList et MangaDex ont aussi répondu HTTP 200
pendant ce run de régression.

## Correction découverte par la sonde réelle

La première sonde Avatar a échoué : le résolveur reprenait la taille de page
30 du workflow SDK, refusée avec HTTP 400 par le fournisseur natif actuel.
Une comparaison directe a confirmé : `perPage=20` renvoie 20 résultats,
`perPage=30` échoue. Le résolveur et les fixtures utilisent maintenant 20 ;
la régression a été rejouée après correction.

La sonde finale a obtenu :

```json
{"status":200,"durationSeconds":15.45,"subjectId":"8906247916759695608","title":"Avatar","year":"2009","quality":"1080p","rangeStatus":206,"bytesReceived":32,"mp4Signature":true}
```

La lecture réseau est strictement arrêtée après 32 octets. Ce contrôle atteste
la recherche, la sélection, la résolution et le relais pour ce titre au moment
du test. Il ne prouve pas la disponibilité de tous les films/épisodes, le bon
doublage audio, ni la lecture complète sur les appareils natifs.

## Reproduire

Avec le backend utilisateur actif :

```sh
bash scripts/test_api.sh
```

Le script utilise l'environnement Python local, démarre au besoin la fixture
SDK puis l'arrête, et détecte le runner Newman installé dans `build/api-tools/`.
Des options supplémentaires sont transmises au runner. La commande utilisée
pour le run final :

```sh
bash scripts/test_api.sh --reporters cli,json,junit --reporter-json-export postman/reports/newman-sdk-2026-11-08.json --reporter-junit-export postman/reports/newman-sdk-2026-11-08.xml --timeout-request 40000
```

Rapports détaillés locaux, ignorés par Git : les deux fichiers ci-dessus,
`postman/reports/sdk-fixture.log` et `postman/reports/live-sdk-2026-11-08.json`.
L'avertissement Starlette TestClient/httpx antérieur demeure ; aucune assertion
n'échoue. `git diff --check` est également passé.
