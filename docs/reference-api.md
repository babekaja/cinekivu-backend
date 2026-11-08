# Métadonnées de référence V5/V6

Le préfixe `/api/v1/reference` regroupe les fonctions de métadonnées de
`reference/MovieBox_V6` trouvé dans l'archive CineKivu V28. Il contient
601 œuvres et 610 éditions, relevées le 15 octobre 2026.

Chaque réponse est enveloppée dans `code/data` et indique `historical: true`,
`sourceDate: "2026-10-15"`, `streamAvailability: "unknown"`.
Les routes fonctionnent hors ligne et ne créent pas de session fournisseur.

- `/search` : recherche paginée, paramètres `q`, `category`, `type`, `genre`, `format`,
  `collection`, `year_min`, `year_max`, `original_language`, `edition_language`,
  `audio_language`, `subtitle_language`, `verified_audio`, `verified_subtitles`,
  `authorized_playback`, `locale`, `sort` (`title`, `year_desc`, `year_asc`), `page`, `page_size` (1–100).
- `/taxonomy`, `/categories`, `/genres`, `/formats`, `/collections`, `/stats` :
  classification et statistiques de l'archive.
- `/languages`, `/locales/{locale}` : profils linguistiques et traductions.
- `/sources` : provenance des données et références de taxonomie.
- `/works/{id}`, `/works/{id}/editions`, `/works/{id}/recommendations?limit=12` :
  fiches, éditions et recommandations par intersection de genres.
- `/editions/{id}` et `/editions/{id}/{section}` : sections `tracks`, `seasons`,
  `episodes`, `volumes`, `chapters` et `rights`.

Exemples d'identifiants : `WRK-00001`, `EDT-00001`. Les identifiants de
l'archive, du corpus de recommandations et du fournisseur vidéo sont distincts.
Les filtres audio/sous-titres exigent une piste marquée vérifiée dans les
données. Une langue indiquée dans un titre ne suffit jamais. Aucune des pistes
du relevé fourni n'est vérifiée : ces filtres renvoient donc une liste vide.

Le catalogue actuel reste disponible dans les routes `/api/v1/web` et
`/api/v1/cinekivu`. Les démonstrations synthétiques V6 et les recherches
ponctuelles de métadonnées externes ne sont pas des flux de films supplémentaires.
