# Validation de l'API

Après toute modification fonctionnelle de l'API, exécuter les tests Python
appropriés et la collection Postman de régression contre le backend mis à jour :

```sh
bash scripts/test_api.sh
```

Utiliser le plugin Postman pour consulter et maintenir la collection existante
`41970206-39202388-c5d9-4d8f-8e8e-f0c042a2e169` dans l'espace personnel
`da838857-b84d-447a-85c2-5e4aaa1d9961` lorsqu'il est connecté. Préserver les
autres requêtes. Exécuter les tests localhost avec le CLI ou Desktop local :
le plugin hébergé ne peut pas atteindre le serveur du Mac.

Le script démarre au besoin l'environnement HTTP synthétique SDK sur
`127.0.0.1:8001`, requis par le dossier Postman de téléchargement. Il arrête
uniquement le processus qu'il a lui-même lancé. Le backend réel doit être
actif sur `127.0.0.1:8000`. Les requêtes SDK utilisent `sdkBaseUrl`, les autres
requêtes utilisent `baseUrl`. Ne pas remplacer les fournisseurs du backend
réel par les fixtures de test.

Le plugin pytest `api_audit_pytest` isole les bases SQLite et enregistre les
méthodes/routes observées dans `postman/reports/api-route-coverage.json`.
Cette couverture avec fournisseurs simulés ne remplace pas une sonde réelle.
Pour l'audit de lecture des fournisseurs :

```sh
.venv/bin/python scripts/audit_all_api.py --media --output postman/reports/all-api-live-final.json
```

Ce script distingue les échecs locaux des indisponibilités fournisseur explicites :
code de sortie 0 si tous les contrôles réussissent, 1 pour un échec à examiner,
2 si seules des indisponibilités fournisseur subsistent. Les lectures vidéo sont
bornées à 32 octets ; les sous-titres sont bornés à 2 Mio. Ne pas effectuer
d'envoi OTP, de création de compte ou de publication réelle pour obtenir du vert.

Rapporter les résultats réellement exécutés, distinguer les tests avec
fournisseurs simulés des appels réels, et enregistrer les limites rencontrées.
Ne jamais présenter un catalogue historique ou une recommandation comme une
preuve qu'un flux vidéo existe ou qu'une piste audio est vérifiée.
