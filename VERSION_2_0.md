# Aizy 2.0 — copie de travail

Créée le 13 octobre 2026 à partir de la version d’origine avant 1.0, en fonctionnement sur le port 8010.
La copie inclut le code effectivement présent, y compris les modifications locales, et les neuf dossiers de démonstration de la base de présentation active.
Les comptes, rôles, accès aux restaurants et mots de passe de cette version sont conservés.

## Démarrage

Exécuter `./Lancer-Aizy.sh`, puis ouvrir http://localhost:8013.
Le serveur possède ses propres cookies, son environnement Python et ses fichiers médias.
Base : `aizy_v2`, conteneur `aizy-v2-db`, volume `aizy_v2_pg`, port PostgreSQL 55453.
La version sur 8010 et les copies 1.0/1.1 restent indépendantes.

Le dossier privé `.local` contient la configuration, un snapshot initial et la configuration IA de la version source si elle était présente. Ne pas le publier.
Le démarrage ordinaire conserve les données ; il ne restaure pas le snapshot à chaque lancement.
`VERSION_2_0_SOURCE.json` décrit la provenance du code et ses empreintes.
Le tag local `v2.0.0-base` représente le point de départ avant les futures modifications.
