# Aizy 2.1 — copie de recette

Copie du code de la référence Aizy 2.0 figée, avec base PostgreSQL, médias et cookies séparés.
Dossier : `/var/home/JJ/Documents/APPS/Aizy 2.1`. Adresse : http://localhost:8014/.
La 2.0 reste sur http://localhost:8013/ ; sa sauvegarde et son tag `aizy-2.0.0` sont conservés dans son dossier.

Démarrage : `./Lancer-Aizy-2.1.sh` ou `.local/start.sh`.
Point initial : 2 restaurants, 47 fournisseurs, 143 factures/avoirs, 95 BL, 39 catalogues, 95 signalements dont 60 actifs, 77 réclamations.
Les comptes utilisateurs et leurs mots de passe sont conservés. Max et Leo sont limités au Belvédère, reception a les deux restaurants.
Les données métier sont toutes fictives et incluent 171 scénarios. Les clés API ne sont pas copiées : configurer Diagnostic IA pour une extraction réelle.

Procédure : `output/recette-aizy-2.1/procedures-tests.html`, 477 tests dont un parcours essentiel de 20.
Versions PDF, CSV, JSON et fichiers de dépôt dans le même dossier. Deux ZIP sont disponibles dans `output/`.
Les tests manuels ne sont pas précochés réussis ; les vérifications de fabrication et navigateur sont dans `verification-livrable.json`.

Pour rejouer : exporter les résultats, arrêter la 2.1 avec Ctrl+C, lancer `.local/reset-tests.sh --confirmer`, puis redémarrer.
Cette restauration revient au point initial de la seule base 2.1, comptes compris, et remet les pièces fictives initiales. Les autres originaux déposés restent sur disque.
Le script refuse si la 2.1 est active, si le dossier, la version ou la configuration PostgreSQL diffèrent.
Accès Wi-Fi actuel : http://192.168.1.75:8014/ (adresse dépendante du réseau). Aucun test caméra sur appareil physique n’a été effectué.
