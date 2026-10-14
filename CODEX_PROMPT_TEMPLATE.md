# Template de prompt pour Codex - Projet Facture

À utiliser au début de chaque nouvelle demande importante.

---

Avant de commencer, lis obligatoirement les fichiers suivants à la racine du projet :

- `AI_RULES.md`
- `PROJECT_CONTEXT.md`

Tu dois respecter ces règles pendant toute la modification.

Si ma demande entre en conflit avec une règle du projet :
- explique le conflit ;
- propose la solution la plus simple ;
- ne contourne pas silencieusement les règles.

Avant de modifier le code :
- vérifie l’état Git avec `git status` ;
- identifie les fichiers concernés ;
- évite les changements sans rapport ;
- crée une branche dédiée si nécessaire.

Pendant la modification :
- garde l’interface simple ;
- ne casse pas les fonctionnalités existantes ;
- conserve les documents uploadés même si l’analyse échoue ;
- ne rends pas les champs inutilement obligatoires ;
- ajoute des logs utiles côté serveur ;
- n’affiche pas d’erreur technique brute à l’utilisateur ;
- ajoute ou adapte les tests si possible.

À la fin :
- lance les tests disponibles ;
- vérifie que l’application démarre ;
- fais un ou plusieurs commits propres ;
- pousse la branche sur GitHub si demandé ;
- fournis un résumé clair des modifications.

Demande à traiter :

[ÉCRIRE ICI LA DEMANDE]