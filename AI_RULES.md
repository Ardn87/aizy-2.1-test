# AI Rules - Projet Facture

Ce fichier contient les règles obligatoires à respecter lors de toute modification du projet.

Avant toute modification importante, l’IA doit lire ce fichier et respecter ses règles.

## 1. Simplicité avant tout

Le projet doit rester simple à utiliser et simple à maintenir.

Ne pas ajouter de complexité inutile.
Ne pas créer de grosse architecture sans besoin clair.
Ne pas ajouter de fonctionnalités avancées si une solution simple suffit.
Ne pas transformer une petite demande en refonte complète.

## 2. Interface utilisateur

L’interface doit rester claire, pratique et rapide.

Chaque écran doit avoir un objectif simple.
Éviter les formulaires trop longs.
Éviter les boutons ou options inutiles.
Toujours afficher des messages compréhensibles pour un utilisateur non technique.

Les erreurs techniques ne doivent jamais être affichées directement à l’utilisateur final.
Elles doivent être disponibles uniquement dans les logs ou dans une vue admin si elle existe.

## 3. Documents importés

Un document uploadé ne doit jamais être perdu.

Si l’analyse IA échoue :
- conserver le fichier ;
- conserver une trace en base ;
- mettre un statut clair ;
- permettre une relance de l’analyse.

L’application ne doit jamais planter à cause d’un fichier mal analysé.

## 4. Analyse IA

L’IA sert à pré-remplir les données, pas à décider définitivement.

Les données extraites par l’IA doivent toujours être vérifiables et modifiables par l’utilisateur.

Ne jamais inventer une valeur manquante.
Si une donnée est absente ou incertaine, utiliser `null` ou marquer comme `à vérifier`.

Toujours prévoir :
- un niveau de confiance ;
- des warnings si nécessaire ;
- une conservation de la donnée brute lorsque possible.

## 5. Factures, bons de livraison et catalogues

Ne pas confondre :
- quantité commandée ;
- quantité livrée ;
- quantité facturée ;
- prix catalogue ;
- prix facturé.

Un bon de livraison peut ne pas contenir de prix.
Une facture peut ne pas contenir toutes les informations catalogue.
Un catalogue peut avoir plusieurs devises ou unités.

## 6. Devises

Ne pas supposer que tous les prix sont en euros.

Certains restaurants ou fournisseurs peuvent être en CHF ou dans une autre devise.
La devise doit être stockée clairement lorsque des prix sont présents.

Si la devise est inconnue, ne pas inventer. Marquer comme `à vérifier`.

## 7. Statuts

Les documents doivent avoir des statuts clairs.

Statuts recommandés :
- `uploade`
- `analyse_en_cours`
- `importe`
- `a_verifier`
- `analyse_echouee`
- `valide`
- `a_rattacher`

Ne pas mettre `importe` si les données sont trop incertaines.

## 8. Relations métier

Un document peut être lié à :
- un restaurant ;
- un fournisseur ;
- une commande ;
- des lignes produits.

Ne pas bloquer inutilement l’import si une relation manque.
Dans ce cas, utiliser un statut comme `a_rattacher` ou `a_verifier`.

## 9. Sécurité et rôles

Les actions destructrices doivent être limitées aux admins.

Exemples :
- supprimer une facture ;
- supprimer un bon de livraison ;
- supprimer un catalogue ;
- supprimer une commande ;
- supprimer un fournisseur.

Ne jamais ajouter une action destructive accessible à tous les utilisateurs.

## 10. Git et GitHub

Le projet est versionné sous GitHub.

Avant modification :
- vérifier l’état du dépôt avec `git status` ;
- ne pas écraser les changements existants ;
- créer une branche dédiée si nécessaire.

Pendant le travail :
- faire des commits réguliers ;
- écrire des messages de commit clairs ;
- ne pas modifier des fichiers sans lien avec la demande.

À la fin :
- vérifier les tests ;
- vérifier que l’application démarre ;
- pousser la branche sur GitHub si demandé.

Ne jamais faire de `git push --force` sans instruction explicite.

## 11. Tests

Toute correction importante doit être accompagnée de tests si le projet possède déjà une structure de tests.

Tester au minimum :
- le cas nominal ;
- les erreurs prévisibles ;
- les champs manquants ;
- l’absence de configuration OpenAI ;
- les droits admin si une action sensible est concernée.

## 12. Configuration OpenAI

L’application doit fonctionner même si OpenAI n’est pas configuré.

Si `OPENAI_API_KEY` ou `OPENAI_MODEL` est absent :
- ne pas planter ;
- conserver le document ;
- afficher un message clair ;
- mettre un statut cohérent.

## 13. Logs et erreurs

Ajouter des logs utiles pour les étapes importantes :
- upload ;
- analyse IA ;
- parsing ;
- sauvegarde base de données ;
- erreur de validation ;
- erreur OpenAI.

Les logs doivent aider au diagnostic sans exposer inutilement de données sensibles.

## 14. Règle finale

Avant toute modification importante, relire ce fichier.

Si une demande semble entrer en conflit avec ces règles :
- ne pas ignorer la règle silencieusement ;
- expliquer le conflit ;
- proposer la solution la plus simple et la plus sûre.