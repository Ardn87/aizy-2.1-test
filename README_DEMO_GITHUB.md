# Installation et scénarios de la copie GitHub de test

Le dépôt contient le paquet `jeu-donnees-fictif-aizy-2.1/` : 171 scénarios, 1 957 enregistrements métier et 238 pièces fictives (factures, BL et images). Le paquet exclut les utilisateurs, les mots de passe et les clés. Les adresses fournisseur utilisent le domaine de test `.invalid`.

## Démarrage dans NewCode

Importer `https://github.com/Ardn87/aizy-2.1-test.git`, puis lancer le projet avec la procédure de déploiement de l’atelier. Le manifeste `.newcode/app-factory.json` déclare le service Web et la route de santé `/sante/`. Le service utilise une base SQLite et des documents dans le volume privé `aizy_data` ; il n’expose aucun port hôte dans le Compose.

Sur une installation vide, `.newcode/start.sh` génère une clé Django locale, applique les migrations, vérifie les empreintes du paquet, importe les scénarios et crée les comptes de démonstration. Un redémarrage ne réimporte pas les données et ne change pas les mots de passe. Si la base contient déjà des données métier, l’importeur refuse de les écraser.

Dans le terminal du projet :

```sh
./manage.sh doctor
./manage.sh install
./manage.sh credentials
```

`credentials` affiche les quatre identifiants et mots de passe générés dans ce volume. Ils ne figurent ni dans Git ni dans les journaux de démarrage. Les comptes Reception et Revue sont limités à leurs sections respectives et ont accès aux deux restaurants fictifs.

## Repères de recette

- `jeu-donnees-fictif-aizy-2.1/cas-couverts.csv` : cas de prix, catalogue, rapprochement et documents.
- `jeu-donnees-fictif-aizy-2.1/cas-livraison-2.1.json` : défauts de livraison et commentaires sur BL.
- `jeu-donnees-fictif-aizy-2.1/matrice-reclamations-2.1.json` : réclamations par fournisseur et statuts.
- `jeu-donnees-fictif-aizy-2.1/media/` : pièces fictives pour consulter les dossiers et tester le dépôt.

Le paquet contient des états OCR déjà préparés ; aucun appel OpenAI n’est nécessaire pour les afficher. Une analyse OCR réelle sur un nouveau document nécessite une clé API configurée séparément. Les cas fictifs ne représentent pas de vraies opérations des fournisseurs cités.

Pour vérifier l’importeur sur une base SQLite éphémère, avec les dépendances Python installées :

```sh
python jeu-donnees-fictif-aizy-2.1/verifier.py .
```
