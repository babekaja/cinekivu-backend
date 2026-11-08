# Recherche approfondie — MovieBox sur Render, 8 novembre 2026

Le dernier HTTP 429 ne suffit pas à prouver une saturation ni un blocage de
l'adresse Render. Une comparaison contrôlée révèle un défaut dans notre repli
Android : il retirait le Referer attendu par le CDN. Le rétablir est la première
correction concrète. La comparaison finale sur Render reçoit 426 avec httpx comme avec curl_cffi
Chrome 152. Les deux réussissent depuis le Mac. Une sortie réseau acceptée ou
une lecture par le client est donc la piste suivante ; la lecture Render reste
indisponible, et le filtrage exact du fournisseur n’est pas publiquement établi.

## Preuve obtenue sur notre réseau local

Une seule source MobLand S1E1, hôte `bcdnxw.hakunaymatata.com`, est utilisée
pour chaque série. Chaque lecture réussie porte uniquement sur `bytes=0-31`.
Aucun film complet n'est téléchargé ; aucun lien signé n'est publié.

| Client et profil | Statut | Résultat |
| --- | --- | --- |
| httpx HTTP/2, navigateur, Referer mzfi.me | 206 | 32 octets, signature MP4 `ftyp` |
| httpx HTTP/2, Android, sans Referer | 429 | HTML, aucun octet média lu |
| httpx HTTP/2, navigateur, Referer moviebox.ph | 206 | 32 octets, signature MP4 `ftyp` |
| curl_cffi, profil Chrome, Referer moviebox.ph | 206 | 32 octets, signature MP4 `ftyp` |

Un second contrôle garde **tous les en-têtes Android identiques**, à l'exception
de l'ajout de `Referer: https://moviebox.ph/` : sans cet en-tête, HTTP 429 ;
avec cet en-tête, HTTP 206 et `ftyp`. Les réponses se suivent en moins d'une
seconde. Le serveur n'envoie pas de Retry-After dans ces réponses.

Cela établit l'effet du Referer pour ce fichier et ce réseau. Cela ne prouve pas
que le même profil suffira depuis Render. Les quatre profils de la première
série comparent des configurations complètes ; ils n'isolent pas à eux seuls
l'effet de l'empreinte TLS.

Traces locales ignorées par Git : `build/research-cdn-profiles.json` et
`build/research-cdn-referer-control.json`. Les scripts correspondants ne
réutilisent que des sources fraîches obtenues par notre moteur existant.

## Ce que les sources externes établissent

- [Mellow Movies, backend](https://github.com/ndizeyedavid/mellow-movies/blob/43dad0f8de0e6c7c26a170379a81fbfce087d44e/backend/api.py)
  distingue le refus lié au Referer et le refus depuis certains réseaux hébergés.
  Son architecture accepte un relais personnel et une sortie résidentielle.
  Ce sont des observations de l'auteur, pas une mesure indépendante de notre Render.
- [Son dernier correctif desktop, 7 novembre 2026](https://github.com/ndizeyedavid/mellow-movies/commit/43dad0f8de0e6c7c26a170379a81fbfce087d44e)
  conserve une voie locale qui récupère le média depuis le réseau de l'utilisateur.
  Il corrige aussi les plages et les fichiers partiels. Un cache de fragment
  présenté comme fichier complet peut donc introduire un autre défaut de lecture.
- [Kunai, documentation du fournisseur](https://github.com/KitsuneKode/kunai/blob/6d45e505eb01c87b84bea4fcb87be603edb49c52/.docs/providers.md)
  signale des 429 liés à l'environnement de lecture. Pour **VidLink**, le mode
  `webkit` sélectionne un DASH avec cookies sur un autre CDN. Ce paramètre
  appartient à cette API ; il n'est pas démontré qu'il s'applique à MovieBox H5.
- [MovieBox-Tui, changelog](https://github.com/mesamirh/MovieBox-Tui/blob/4598d5497bb902ee7dceb540da2240ec9c7ccdc3/CHANGELOG.md)
  décrit d'autres différences entre en-têtes MP4 et DASH. Cela justifie des
  profils distincts par ressource, plutôt qu'un changement global des en-têtes.

Les sources ne s'accordent donc pas sur un profil universel. Notre comparaison
contrôlée a priorité sur les recettes génériques pour le fichier observé.

## Pistes classées pour CineKivu

| Ordre | Action | Ce qu'elle permet de déterminer |
| --- | --- | --- |
| 1 | Garder le Referer correct dans le repli Android et dans le relais enregistré | Éliminer le 429 que notre propre repli peut produire |
| 2 | Comparaison effectuée depuis Render, même URL fraîche et mêmes en-têtes explicites | HTTP 426 pour les deux ; l’empreinte Chrome seule ne suffit pas dans ce cas |
| 3 | Vérifier les autres sources réellement fournies, notamment DASH et leurs cookies | Trouver une autre voie sans inventer un miroir ou transférer une signature sur un autre domaine |
| 4 | Si les deux transports échouent depuis Render, comparer avec un relais personnel autorisé | Tester l'hypothèse de la sortie réseau |
| 5 | Pour les applications natives, envisager une lecture par le réseau du téléphone | Garder Render pour le catalogue tout en évitant son réseau pour les octets vidéo |

Notre moteur utilise déjà curl_cffi pour obtenir les liens. Les vérifications
MP4/DASH et le relais utilisent encore httpx. Activer HTTP/2 dans httpx ne
reproduit pas le ClientHello TLS ni les paramètres HTTP/2 d'un navigateur.
[Documentation officielle curl_cffi](https://github.com/lexiforest/curl_cffi/blob/main/README.md).

Un test comparatif correct doit garder les en-têtes identiques (et désactiver
les en-têtes automatiques du profil pour cette comparaison), contrôler le
statut, Content-Range, le type MIME et les 32 octets. Si curl réussit, il faudra
employer le même transport pour le relais : réussir la sonde puis revenir à
httpx ne résoudrait pas la lecture. La bibliothèque avertit que son itérateur
streaming utilise une file interne qui peut accumuler des données. Un relais
réel devra gérer le débit du consommateur et fermer la requête en cas d'abandon.
[Documentation officielle du streaming](https://github.com/lexiforest/curl_cffi/blob/main/docs/quick_start.rst).

La lecture directe est une piste d'architecture pour le natif, pas une solution
équivalente dans une page Web : le navigateur contrôle notamment Origin,
Cookie et Referer, avec des règles propres à Fetch.
[MDN](https://developer.mozilla.org/en-US/docs/Glossary/Forbidden_request_header).

## Relais et hébergement : limites concrètes

Render partage ses plages de sortie par région ; des IP dédiées existent,
mais restent une sortie hébergée. En acheter une ou changer de plan ne garantit
pas que ce CDN l'acceptera.
[Documentation Render](https://render.com/docs/outbound-ip-addresses).

Un [projet Worker public](https://github.com/mdtahseen7/MovieBox-API/blob/main/src/index.js)
relaie le corps de réponse natif avec Range et Referer. Il fournit un exemple
technique, sans preuve que notre source précise sera acceptée aujourd'hui sur
Cloudflare. Les [Streams Workers officiels](https://developers.cloudflare.com/workers/runtime-apis/streams/)
permettent de relayer sans charger tout le fichier en mémoire.

Il faut distinguer Worker et Tunnel. La recette « tunnel public gratuit et
illimité pour les films » trouvée dans certains dépôts n'est pas une garantie
valable : la FAQ Cloudflare prévoit des conditions spécifiques pour les vidéos
et gros fichiers sur les routes publiques Free/Pro/Business. Les routes privées
ont un autre périmètre et nécessitent une connectivité privée côté clients.
[FAQ officielle](https://developers.cloudflare.com/cloudflare-one/faq/cloudflare-tunnels-faq/).

Un relais éventuel doit utiliser une machine ou un compte contrôlé par le
propriétaire de CineKivu, rester limité aux ressources enregistrées et conserver
les plages HTTP. Aucun proxy ouvert, identifiant tiers publié ou compte d'essai
réinscrit pour augmenter un quota n'est nécessaire à cette investigation.

## Vérification API de cette recherche

La collection connectée a été consultée avec le plugin Postman. La requête
MobLand existante a été exécutée sans modification de ses assertions avec
Newman, contre localhost et Render : une requête et quatre assertions réussies
sur chaque cible, code de sortie 0. Localhost renvoie une source lisible ; Render
zéro. Le contrat valide aussi l'erreur explicite quand aucun média n'est vérifié :
le succès Postman ne signifie donc pas que la vidéo Render fonctionne.

Le correctif minimal du Referer est appliqué au repli et au test qui vérifie
sa conservation pour le relais. La validation complète et son résultat Render
sont consignés à la fin de ce document une fois observés.

Validation complète du correctif : 351 tests Python et 222 assertions Postman
sur 107 requêtes réussis, code de sortie 0. Les tests isolés du CDN sont simulés ;
les comparaisons de profils détaillées plus haut sont de vraies lectures locales
bornées à 32 octets. Le résultat Render après déploiement reste distinct.

## Sonde comparative depuis le serveur

Le contrôle Render du correctif Referer est observé : le MP4 reste en HTTP 426
sur HTTP/2, le DASH en 429, et aucun média n'est vérifié. La correction élimine
le défaut local établi, sans rétablir la lecture Render à elle seule.

Une comparaison opt-in est ajoutée à la requête existante avec
`probe_transport=true`. Elle choisit un seul MP4 fourni par le moteur, vérifie
son adresse publique, conserve les mêmes en-têtes explicites pour les deux
transports et demande `bytes=0-31`. Aucun URL signé, cookie, jeton, corps d'erreur
ou adresse client n'est renvoyé. La sonde curl utilise un callback borné et ferme
la requête ; elle n'autorise jamais une source pour le lecteur. Les appels sans
ce paramètre ne lancent pas ce contrôle supplémentaire.

La requête MobLand de la collection Postman existante est mise à jour avec ce
paramètre et deux assertions de borne/confidentialité, via le plugin connecté.
Validation : **358 tests Python et 224 assertions Postman sur 107 requêtes**,
code de sortie 0. En local, la comparaison réelle à en-têtes identiques donne
206 et 32 octets MP4 valides avec httpx HTTP/2 comme avec curl_cffi Chrome 152.
La comparaison Render finale est consignée ci-dessous.


## Résultat final et architecture à retenir

La révision `4073c9f` est observée sur Render par son marqueur de santé. La
requête Postman MobLand opt-in est exécutée une fois sur cette révision : six
assertions réussies, code de sortie 0, mais **zéro source jouable**.

| Réseau | httpx HTTP/2 | curl_cffi Chrome 152 / HTTP/2 |
| --- | --- | --- |
| Mac local, profil explicite identique | 206, 32 octets MP4 valides | 206, 32 octets MP4 valides |
| Render, comparaison sur la même URL fournie à chaque transport | 426, aucun média lu | 426, aucun média lu |

La trace sûre est `build/render-browser-transport-comparison.json`. Ce test
écarte l'idée qu'un remplacement simple de httpx par ce profil curl suffirait
pour ce fichier sur cette sortie Render. Il ne démontre ni un blocage de tous
les hébergeurs ni l'impossibilité de tout autre profil ou fournisseur.

Pour CineKivu, la voie la mieux étayée est de **garder Render pour l'API et
séparer le chemin des octets vidéo**. Deux déclinaisons sont possibles :

1. Application native : récupérer et vérifier la source depuis le réseau du
   téléphone, avec les en-têtes attendus, puis utiliser le lecteur partagé.
   Les candidats directs doivent rester distincts des sources déjà vérifiées
   par le serveur. Le téléphone doit valider les plages avant lecture ou
   téléchargement. Cette intégration n'est pas encore réalisée.
2. Frontend Web : relayer les octets par une sortie personnelle ou un service
   explicitement prévu pour ce trafic, après une sonde d'acceptation. Le simple
   navigateur ne peut pas librement reproduire tous les en-têtes. Le relais doit
   préserver Range, cookies nécessaires, annulation et expiration des liens.

Une machine locale allumée peut fournir la sortie déjà acceptée dans nos sondes,
mais sa connexion montante et sa disponibilité deviennent celles du service.
Aucun proxy externe n'est choisi ou acheté, aucun tunnel public n'est ouvert
pendant cette recherche. Changer uniquement le plan Render ne constitue pas
une solution vérifiée.
