# Aizy 2.1 — dépôt de code pour import dans NewCode

Ce dépôt contient le code Django d’Aizy 2.1 et ses migrations, dans un historique Git neuf. Aucun commit ne contient de base de données, de document métier, d’archive ou de fichier local de clés.

La sauvegarde de la base et les jeux de données fictifs restent dans le projet GitLab privé `sogeta/aizy-2.1`. Après l’import du code, créez une base PostgreSQL vide, appliquez les migrations, puis installez séparément les données de test si nécessaire.

Les fichiers `.env`, `keys.json`, `.local/` et le lanceur local `docker-tunnel` restent hors de Git. Le tunnel se configure avec `CLOUDFLARE_TUNNEL_TOKEN` dans l’environnement local. Les chaînes de tests qui simulent des clés API sont assemblées au moment des tests pour éviter les faux positifs des contrôles de secrets.

La structure de publication NewCode doit être adaptée au contrat de l’atelier avant déploiement : le Compose historique d’Aizy sert au lancement local d’origine et ne respecte pas encore les contraintes de publication NewCode.
