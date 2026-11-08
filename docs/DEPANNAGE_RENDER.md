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

## Résultat après déploiement des diagnostics

Le déploiement `252d50c` a été observé sur Render. MobLand S1E1 renvoie
`playable_count=0`, avec HTTP 406 pour les huit domaines Web interrogés
(mzfi.me, 123moviesfree.club, movie-box.co, movieboxonline.net, sflix.film,
h5-api.aoneroom.com, netnaija.film, movieboxhd.net). Le chemin mobile renvoie
`provider_rejected` et la récupération resource ne fournit aucun flux.

Il s'agit donc d'un refus HTTP avant le relais vidéo. La nature du refus
(négociation HTTP, transport, filtrage réseau) n'est pas établie par le seul
statut 406. Un essai du transport navigateur déjà prévu par le moteur est
préparé : curl_cffi installé par défaut et profil Chrome pris dans sa version
supportée actuelle, plutôt qu'un profil fixé à Chrome 120. Ce changement
doit être vérifié sur Render avant d'être présenté comme une correction de
lecture. Documentation du transport :
https://curl-cffi.readthedocs.io/en/v0.11.4/impersonate.html

Validation du transport installé (curl_cffi 0.17.0 sur Mac, profil Chrome courant) : 328 tests Python et 222 assertions Postman réussis. Le résultat sur Render doit être mesuré après le nouveau déploiement.

## Contre-vérification réelle après activation du transport

Le commit `9039a56` a été observé sur Render : la santé indique
`provider_http_client=curl_cffi`. MobLand S1E1 renvoie toujours zéro flux,
HTTP 406 sur les huit accès Web, refus mobile et aucune récupération resource.
Changer de transport ne résout donc pas ce cas. Aucun test de lecture Render
ne doit être présenté comme réussi. Le contrôle local conserve deux MP4
avec ce transport dans la collection Postman.

Le refus apparaît avant l'accès au fichier CDN : c'est l'API fournisseur de
lecture qui refuse les requêtes issues de Render. Un filtrage du réseau ou
de l'adresse de sortie est probable, sans être prouvé par le seul statut 406.
L'étape externe restante consiste à faire vérifier cette adresse par le
fournisseur et ses règles d'accès, ou à utiliser un hébergement/réseau accepté
par celui-ci. Le tableau de bord Render expose les plages dans Connect > Outbound
(documentation officielle liée plus haut). Les URL de CDN et cookies n'ont
pas été publiés dans ce diagnostic.

## Essai du contexte client réel

La piste signalée dans l'issue communautaire
https://github.com/walterwhite-69/Moviebox-API/issues/4 et la PR non fusionnée
https://github.com/walterwhite-69/Moviebox-API/pull/16 est implémentée avec
validation et isolation : adresse publique réelle fournie par Cloudflare sur
Render, en-têtes de contexte navigateur propres au domaine et renouvellement
401/441 borné. Aucune IP résidentielle inventée, aucun proxy tiers ajouté.

341 tests Python et 222 assertions Postman réussis avant publication. Les tests
d'IP utilisent des transports simulés, y compris deux clients simultanés et le
passage dans les threads FastAPI. Les lectures réelles de la collection sont
exécutées hors Render ; le résultat Render reste à vérifier après déploiement.

Références du périmètre de confiance :
https://developers.cloudflare.com/fundamentals/reference/http-headers/
https://render.com/docs/environment-variables

## Contexte réel observé et vérification du CDN

Après `882faaa`, Render obtient trois sources de mzfi.me : HTTP 200, code 0.
Le refus 406 est donc levé pour cette requête réelle. Le CDN refuse toutefois
les vérifications MP4 et DASH avec HTTP 426 ; `playable_count` reste zéro.

Le transport CDN négocie maintenant HTTP/2, pour les sondes comme pour le relais.
En cas de 426, le diagnostic indique le protocole réellement négocié et, si sa
valeur est bornée et syntaxiquement sûre, l'en-tête Upgrade. Une réponse 426
n'est pas assimilée à une preuve qu'HTTP/2 suffira ; le résultat Render doit
être observé. Référence : https://www.rfc-editor.org/rfc/rfc9110.html#name-426-upgrade-required

Validation locale de ce changement : 344 tests Python et 222 assertions
Postman (107 requêtes), tous réussis. Une sonde réelle MobLand S1E1 en HTTP/2,
avec puis sans en-têtes d'adresse client réelle, obtient dans les deux cas
HTTP 206, `bytes 0-31/673011531`, signature MP4 `ftyp`. Ce contrôle local ne
valide pas le réseau Render.

Render confirme le 426 même après négociation HTTP/2, sans en-tête Upgrade.
Le statut correspond donc ici à un refus du CDN ; changer seulement de protocole
n'a pas rendu MobLand lisible.

Un repli borné sur le profil de lecture Android est ajouté pour les seuls hôtes
`hakunaymatata.com` et leurs sous-domaines, après 426/428 uniquement. Il conserve
les URL signées, le cookie DASH et les plages HTTP ; il n'envoie pas les en-têtes
Origin/Referer/adresse transférée du navigateur. Le profil accepté n'est conservé
qu'après vérification du média. Les 403/404 ne sont pas contournés par cette voie.
Piste issue du code public du relais, sans réutilisation de son service ni de secrets :
https://github.com/byspun/spun-moviebox-api/blob/main/relay/server.js

Validation : 351 tests Python et 222 assertions Postman (107 requêtes) réussis.
Les dix tests de protocole couvrent notamment la conservation du profil accepté
pour le relais, l'absence de repli sur d'autres domaines et les refus persistants.
Le résultat Render de ce repli reste à vérifier après publication.

## Contre-vérification Render du repli Android

La révision `46e59c1` est observée par la santé publique. Deux contrôles MobLand
S1E1, espacés de plus d'une minute sans sonde média, donnent toujours zéro média
vérifié : les vérifications MP4 et DASH reçoivent HTTP 429. Plusieurs accès Web
renvoient bien HTTP 200, code 0 et trois liens. La lecture n'est donc pas rétablie
sur Render. Le dernier statut ne permet pas d'attribuer le refus au seul protocole,
au profil ou à l'adresse réseau ; il faut distinguer les liens obtenus d'un média
lisible. Le repli ne relance pas une réponse 429, 403 ou 404.

Les sondes Render cessent après cette contre-vérification pour ne pas aggraver
une éventuelle limitation. Une sortie réseau autorisée par le fournisseur ou
l'examen de ses règles est la prochaine piste, sans garantie que changer de
plan Render ou de transport suffise. Aucun relais public tiers ni identifiant
récupéré sur Internet n'a été utilisé. Le correctif est partiel, sans nouvelle
preuve de lecture Render.
