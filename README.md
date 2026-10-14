# Aizy 2.1 — démonstration avec données fictives

Cette copie privée sert à tester l’application et ses parcours métier dans NewCode. Elle contient le code Django, les migrations, **171 scénarios fictifs** et leurs pièces de démonstration. Elle ne contient ni base de données préremplie, ni compte, ni mot de passe, ni clé API.

Le déploiement de test est défini dans `.newcode/compose.yaml`. Au premier démarrage sur une base vide, il applique les migrations, importe les données fictives et crée quatre comptes de test (Superadmin, Admin, Reception, Revue) avec des mots de passe aléatoires conservés uniquement dans un volume privé. Les démarrages suivants préservent les décisions prises pendant les essais.

Dans un terminal du projet NewCode, après le démarrage :

```sh
./manage.sh credentials
```

Cette commande affiche les identifiants créés pour cette installation. `./manage.sh status` et `./manage.sh logs` permettent de vérifier le démarrage. Les scénarios sont indexés dans `jeu-donnees-fictif-aizy-2.1/cas-couverts.csv`, et leurs règles d’import figurent dans [README_DEMO_GITHUB.md](README_DEMO_GITHUB.md).

Les états OCR du jeu sont préparés : tester une **nouvelle** analyse réelle demande de configurer séparément l’API OpenAI. Le dépôt GitLab privé de sauvegarde Aizy 2.1 et les versions figées ne sont pas modifiés par cette copie.
