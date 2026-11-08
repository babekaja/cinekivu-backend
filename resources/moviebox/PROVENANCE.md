# Provenance

`movies.csv` est le sous-ensemble de 5 000 films livré dans l'archive
`moviebox-master.zip`, chemin `moviebox/dataset/movies.csv`. Le fichier est
conservé à l'identique, y compris ses identifiants de 0 à 4999.

Le logiciel Moviebox est de Mario Sinani et Klaudio Sinani ; sa licence MIT
est conservée dans `LICENSE.md`. L'implémentation CineKivu reprend l'approche
TF-IDF/cosinus, avec un index creux partagé et des réponses JSON.

Les données proviennent du [CMU Movie Summary Corpus](https://www.cs.cmu.edu/~ark/personas/)
de David Bamman, Brendan O'Connor et Noah A. Smith, extraites de Wikipedia et
Freebase. La page d'origine distribue les données sous une licence
Creative Commons Attribution-ShareAlike ; cette provenance est distincte de
la licence MIT du logiciel Moviebox. Les résumés sont en anglais.

Référence : *Learning Latent Personas of Film Characters*, ACL 2013.
