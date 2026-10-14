# Project Context - Facture

## Objectif du projet

Facture est une application web destinée à aider des restaurants à gérer et vérifier leurs documents fournisseurs.

L’objectif est de simplifier le contrôle entre :
- les factures ;
- les bons de livraison ;
- les catalogues fournisseurs ;
- les commandes ;
- les restaurants ;
- les fournisseurs.

L’application doit permettre de détecter plus facilement :
- les écarts de prix ;
- les écarts de quantité ;
- les erreurs de facturation ;
- les produits non reconnus ;
- les incohérences entre bon de livraison, facture et catalogue.

## Philosophie du projet

Le projet doit rester simple, pratique et compréhensible.

L’utilisateur cible n’est pas forcément technique.
L’interface doit donc être rapide à comprendre et éviter les écrans trop complexes.

L’IA doit assister l’utilisateur, pas remplacer totalement la vérification humaine.

## Documents gérés

Le projet gère principalement :

### Factures

Une facture contient généralement :
- un fournisseur ;
- un restaurant/client ;
- une date ;
- un numéro de facture ;
- des lignes produits ;
- des quantités facturées ;
- des prix unitaires ;
- des totaux ;
- une devise.

La facture sert à vérifier ce qui est facturé.

### Bons de livraison

Un bon de livraison contient généralement :
- un fournisseur ;
- un restaurant/client livré ;
- une date de livraison ;
- un numéro de bon de livraison si présent ;
- des produits livrés ;
- des quantités livrées ;
- des unités ou conditionnements.

Un bon de livraison peut ne pas contenir de prix.
Le bon de livraison sert surtout à vérifier les quantités réellement livrées.

### Catalogues fournisseurs

Un catalogue fournisseur contient généralement :
- des références produits ;
- des noms de produits ;
- des unités ;
- des conditionnements ;
- des prix catalogue ;
- une devise ;
- des périodes de validité éventuelles.

Le catalogue sert à vérifier que les prix facturés correspondent aux prix attendus.

### Commandes

Une commande est un objet interne de l’application.

Elle peut regrouper :
- une ou plusieurs factures ;
- un ou plusieurs bons de livraison ;
- un fournisseur ;
- un restaurant.

La commande ne correspond pas forcément à un document uploadé.
Elle sert surtout à regrouper les éléments à comparer.

## Règles métier importantes

Ne jamais confondre :
- quantité commandée ;
- quantité livrée ;
- quantité facturée ;
- prix catalogue ;
- prix facturé.

Un prix absent sur un bon de livraison n’est pas une erreur.
Une référence produit absente n’est pas forcément bloquante.
Un numéro de bon de livraison absent ne doit pas bloquer l’import.

Si une donnée est incertaine, elle doit être marquée comme à vérifier.

## Restaurants et devises

Certains restaurants peuvent être situés en Suisse.
Il faut donc prévoir que certains documents soient en CHF.

Ne pas supposer que toutes les données financières sont en EUR.
La devise doit idéalement être portée par :
- le restaurant ;
- le fournisseur si nécessaire ;
- le document ;
- les lignes de document si nécessaire.

En cas de doute, la devise doit être marquée comme inconnue ou à vérifier.

## Rôle de l’IA

L’IA doit aider à :
- extraire les données des documents ;
- structurer les lignes produits ;
- repérer les informations principales ;
- signaler les incertitudes ;
- proposer un pré-remplissage.

L’IA ne doit pas :
- inventer des données absentes ;
- valider définitivement un document sans intervention utilisateur ;
- masquer les incertitudes ;
- bloquer tout le processus si une donnée manque.

## États recommandés des documents

Les documents peuvent avoir les statuts suivants :

- `uploade` : le fichier a été reçu et stocké.
- `analyse_en_cours` : l’analyse IA ou le parsing est en cours.
- `importe` : les données ont été extraites avec un niveau suffisant.
- `a_verifier` : des données sont manquantes ou incertaines.
- `analyse_echouee` : l’analyse a échoué, mais le document est conservé.
- `valide` : l’utilisateur a vérifié et validé les données.
- `a_rattacher` : le document doit être relié à une commande, un fournisseur ou un restaurant.

## Interface souhaitée

L’interface doit rester simple.

Les écrans importants sont :
- liste des documents ;
- import de document ;
- détail d’un document ;
- écran de vérification des données extraites ;
- liste des documents à vérifier ;
- gestion des fournisseurs ;
- gestion des restaurants ;
- gestion des catalogues ;
- gestion des commandes.

Les actions importantes doivent être visibles :
- importer ;
- vérifier ;
- modifier ;
- valider ;
- relancer l’analyse IA ;
- supprimer si admin uniquement.

## Sécurité

Les actions destructrices doivent être réservées aux admins.

Un utilisateur classique ne doit pas pouvoir supprimer :
- une facture ;
- un bon de livraison ;
- un catalogue ;
- une commande ;
- un fournisseur ;
- un restaurant.

Les erreurs techniques ne doivent pas être exposées directement aux utilisateurs.

## GitHub

Le projet est versionné sous GitHub.

Les modifications doivent être faites proprement :
- branche dédiée si possible ;
- commits réguliers ;
- messages de commit clairs ;
- pas de push force ;
- pas de modification inutile ;
- tests avant push si possible.

## Priorité générale

Dans le doute, privilégier :
1. la fiabilité ;
2. la simplicité ;
3. la lisibilité du code ;
4. la clarté de l’interface ;
5. la robustesse des imports ;
6. la facilité de correction manuelle.