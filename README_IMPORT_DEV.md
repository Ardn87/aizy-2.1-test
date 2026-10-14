# Aizy 2.1 — dépôt de code pour import dans NewCode

Ce dépôt contient le code Django d’Aizy 2.1 et ses migrations, dans un historique Git neuf. Aucun commit ne contient de base de données, de document client réel, d’archive ou de fichier local de clés ; les pièces jointes du jeu de recette sont entièrement fictives.

La sauvegarde de la base reste dans le projet GitLab privé `sogeta/aizy-2.1`. Le dépôt GitHub de test contient aussi le jeu fictif 2.1 : l’installation NewCode charge automatiquement les scénarios et leurs pièces dans une base vide. Voir `README_DEMO_GITHUB.md`.

Les fichiers `.env`, `keys.json`, `.local/` et le lanceur local `docker-tunnel` restent hors de Git. Le tunnel se configure avec `CLOUDFLARE_TUNNEL_TOKEN` dans l’environnement local. Les chaînes de tests qui simulent des clés API sont assemblées au moment des tests pour éviter les faux positifs des contrôles de secrets.

La copie GitHub de test utilise `.newcode/compose.yaml`, `.newcode/app-factory.json` et `manage.sh` pour le déploiement de démonstration. Le Compose historique reste uniquement dans les versions originales et dans le dépôt GitLab de sauvegarde.
