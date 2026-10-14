# Application Factures & Bons de livraison

Base Django 5.2 LTS dockerisee pour une application monolithique de gestion de
factures et de bons de livraison pour restaurants.

## Stack

- Django 5.2 LTS
- PostgreSQL
- Docker Compose
- Templates Django + Bootstrap 5
- Documents uploades dans `./media`, monte dans le conteneur sur `/app/media`
- Fichiers statiques collectes dans `./staticfiles`

## Installation locale

Si Docker et le plugin Compose sont deja installes, verifier simplement :

```bash
docker --version
docker compose version
```

Sinon, installer Docker avec les paquets officiels Docker. Les commandes ci-dessous
installent Docker Engine, Buildx et le plugin `docker compose`.

### Debian / WSL Debian

Sur Debian, installer Docker depuis le depot officiel Docker :

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
```

Sur WSL avec Docker Desktop et l'integration WSL activee, il n'est generalement
pas necessaire d'installer Docker dans Debian : ouvrir Docker Desktop puis verifier
`docker --version` et `docker compose version` dans le terminal WSL.

Verifier l'installation :

```bash
docker --version
docker compose version
docker run --rm hello-world
```

Si `docker` repond par une erreur de permission, fermer/reouvrir la session ou
relancer `newgrp docker`.

### Fedora

Sur Fedora, installer Docker depuis le depot officiel Docker :

```bash
sudo dnf remove -y docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate docker-logrotate docker-selinux docker-engine-selinux docker-engine || true
sudo dnf config-manager addrepo --from-repofile https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf install -y git docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
```

Verifier l'installation :

```bash
docker --version
docker compose version
docker run --rm hello-world
```

Le `docker-compose.yml` utilise des bind mounts avec l'option `:Z` pour que les
volumes locaux fonctionnent aussi avec SELinux active sur Fedora.

### Demarrage du projet

Depuis une nouvelle machine :

```bash
git clone https://github.com/FeraudTino/Factcheck.git
cd Factcheck
```

Depuis le dossier du projet :

```bash
cp .env.example .env
printf '{"OPENAI_API_KEY":"","OPENAI_MODEL":"gpt-4.1-mini"}\n' > keys.json
chmod 600 keys.json
mkdir -p media staticfiles backups
docker compose up --build -d
docker compose logs -f web
```

`keys.json` est necessaire car il est monte dans le conteneur par Docker
Compose. Il peut garder une cle OpenAI vide en local : l'application demarre
quand meme, conserve les documents importes et signale simplement que l'IA n'est
pas configuree.

Au premier lancement, le service web attend PostgreSQL, applique les migrations
Django, collecte les fichiers statiques, puis demarre le serveur de
developpement.

Initialiser ensuite les roles applicatifs et creer un administrateur :

```bash
docker compose exec web python manage.py init_roles
docker compose exec web python manage.py createsuperuser
```

L'application est disponible ici :

```text
http://localhost:8000
```

Si le port `8000` est deja utilise sur la machine, changer `WEB_PORT` dans
`.env`, par exemple :

```env
WEB_PORT=8001
```

Puis redemarrer :

```bash
docker compose up -d
```

### Script de gestion

Le script racine automatise l'installation Fedora/Debian, le demarrage,
l'arret, les backups et les restaurations sans toucher aux autres projets
Docker :

```bash
./script.sh
```

Commandes directes utiles :

```bash
./script.sh install local
./script.sh install server
./script.sh start local
./script.sh start server
./script.sh restart
./script.sh stop
./script.sh backup
./script.sh restore
./script.sh status
./script.sh mode
```

Le script utilise `COMPOSE_PROJECT_NAME`, choisit automatiquement un port libre
si `WEB_PORT` est deja pris, et restaure automatiquement le dernier couple
`postgres_YYYYMMDD_HHMMSS.dump` + `media_YYYYMMDD_HHMMSS.tar.gz` present dans
`backups/` pendant l'installation.

Le mode `local` lance uniquement PostgreSQL et Django. Le mode `server` lance
aussi le service `tunnel` et exige `CLOUDFLARE_TUNNEL_TOKEN` dans `.env`.
Le choix est conserve dans `AIZY_DEPLOY_MODE`. Depuis le menu interactif,
utiliser `Activer mode local` ou `Activer mode serveur` avant d'installer ou
de demarrer le projet.

## Commandes utiles

Voir les logs du serveur Django :

```bash
docker compose logs -f web
```

Arreter les conteneurs :

```bash
docker compose down
```

Arreter les conteneurs et supprimer le volume PostgreSQL :

```bash
docker compose down -v
```

Lancer les tests :

```bash
docker compose exec web python manage.py test
```

Initialiser les groupes et permissions par defaut :

```bash
docker compose exec web python manage.py init_roles
```

Creer un compte administrateur :

```bash
docker compose exec web python manage.py createsuperuser
```

Ouvrir un shell Django :

```bash
docker compose exec web python manage.py shell
```

Mettre a jour apres un `git pull` :

```bash
git pull
docker compose up --build -d
docker compose logs -f web
```

## Backup et restauration

Creer une sauvegarde de la base PostgreSQL et du dossier `media` :

```bash
./script.sh backup
```

Equivalent historique :

```bash
scripts/backup.sh
```

Les fichiers sont crees dans le dossier `backups/` :

```text
backups/postgres_YYYYMMDD_HHMMSS.dump
backups/media_YYYYMMDD_HHMMSS.tar.gz
```

Le dump PostgreSQL est genere avec `pg_dump` depuis le conteneur `db`. L'archive
media compresse le dossier local `./media`.

Restaurer grossierement une sauvegarde :

```bash
./script.sh restore backups/postgres_YYYYMMDD_HHMMSS.dump backups/media_YYYYMMDD_HHMMSS.tar.gz
```

Equivalent historique :

```bash
scripts/restore.sh backups/postgres_YYYYMMDD_HHMMSS.dump backups/media_YYYYMMDD_HHMMSS.tar.gz
```

La restauration demande une confirmation manuelle, arrete le service `web`,
remplace la base PostgreSQL, remplace le dossier `media`, puis redemarre `web`.
Verifier les chemins avant de confirmer.

## Configuration

Les variables principales sont documentees dans `.env.example`.

Pour la production, changer au minimum :

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG=0`
- `DJANGO_ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS` si le site est en HTTPS
- `POSTGRES_PASSWORD`
- `DJANGO_START_COMMAND` pour utiliser Gunicorn

Les documents uploades sont stockes localement dans `./media` et disponibles
dans le conteneur sous `/app/media`. Ils ne doivent pas etre exposes
publiquement via `/media/...` : la consultation passe par les vues Django
protegees `/factures/<id>/fichier/` et `/bons-livraison/<id>/fichier/`.

En production, ne pas configurer Nginx ou Apache pour servir directement le
dossier `./media`.

## Deploiement Linux

Cette configuration vise un deploiement simple sur un serveur Linux avec Docker
Compose, PostgreSQL, stockage local des documents et sauvegardes locales.

### Prerequis serveur

Installer Docker en suivant la section correspondant au systeme :

- Debian : `Installation locale > Debian / WSL Debian`
- Fedora : `Installation locale > Fedora`

Installer ensuite Nginx.

Sur Debian :

```bash
sudo apt update
sudo apt install -y nginx
```

Sur Fedora :

```bash
sudo dnf install -y nginx
sudo systemctl enable --now nginx
```

Placer le projet sur le serveur, par exemple :

```bash
sudo mkdir -p /srv/factcheck
sudo chown "$USER":"$USER" /srv/factcheck
cd /srv/factcheck
git clone https://github.com/FeraudTino/Factcheck.git .
```

### Variables d'environnement

Creer le fichier `.env` :

```bash
cp .env.example .env
printf '{"OPENAI_API_KEY":"","OPENAI_MODEL":"gpt-4.1-mini"}\n' > keys.json
chmod 600 keys.json
```

Pour une production HTTPS derriere Nginx, utiliser des valeurs de ce type :

```env
DJANGO_SECRET_KEY=une-cle-longue-et-secrete
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=factures.example.com
CSRF_TRUSTED_ORIGINS=https://factures.example.com

DJANGO_START_COMMAND=gunicorn facture_project.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 60

DJANGO_SESSION_COOKIE_SECURE=1
DJANGO_CSRF_COOKIE_SECURE=1
DJANGO_USE_X_FORWARDED_PROTO=1

POSTGRES_PASSWORD=un-mot-de-passe-postgres-solide
WEB_PORT=127.0.0.1:8000

OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
```

`WEB_PORT=127.0.0.1:8000` limite l'acces direct au conteneur Django depuis la
machine locale. Nginx devient alors le point d'entree public.

Si Nginx termine HTTPS et redirige deja HTTP vers HTTPS, `DJANGO_SECURE_SSL_REDIRECT`
peut rester a `0`. L'activer seulement si la configuration proxy est bien
verifiee.

### Build et lancement

Construire l'image :

```bash
docker compose build
```

Lancer l'application :

```bash
docker compose up -d
```

Au demarrage, le service `web` applique les migrations, collecte les fichiers
statiques, puis lance la commande definie par `DJANGO_START_COMMAND`.

Commandes utiles en production :

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py collectstatic --noinput
docker compose exec web python manage.py init_roles
docker compose exec web python manage.py createsuperuser
docker compose logs -f web
```

### Nginx

Un exemple est fourni dans :

```text
deploy/nginx/factures.conf
```

Adapter le domaine, le chemin `/srv/factcheck` et les certificats, puis copier
la configuration.

Sur Debian :

```bash
sudo cp deploy/nginx/factures.conf /etc/nginx/sites-available/factures.conf
sudo ln -s /etc/nginx/sites-available/factures.conf /etc/nginx/sites-enabled/factures.conf 2>/dev/null || true
sudo nginx -t
sudo systemctl reload nginx
```

Sur Fedora :

```bash
sudo cp deploy/nginx/factures.conf /etc/nginx/conf.d/factures.conf
sudo nginx -t
sudo systemctl reload nginx
```

Si `firewalld` est actif sur Fedora, ouvrir HTTP et HTTPS :

```bash
sudo firewall-cmd --permanent --add-service=http --add-service=https
sudo firewall-cmd --reload
```

La configuration sert `./staticfiles` via Nginx, mais ne sert pas `./media`.
Les documents sensibles restent consultes uniquement par les vues Django
protegees.

### Donnees persistantes

- PostgreSQL : volume Docker nomme `postgres_data`
- Documents uploades : dossier local `./media`, monte dans `/app/media`
- Fichiers statiques collectes : dossier local `./staticfiles`
- Backups : dossier local `./backups`, monte dans `/app/backups`

### Backups

Creer une sauvegarde :

```bash
./script.sh backup
```

Ou via le wrapper historique :

```bash
scripts/backup.sh
```

Les fichiers sont stockes dans :

```text
backups/postgres_YYYYMMDD_HHMMSS.dump
backups/media_YYYYMMDD_HHMMSS.tar.gz
```

Restaurer manuellement :

```bash
./script.sh restore backups/postgres_YYYYMMDD_HHMMSS.dump backups/media_YYYYMMDD_HHMMSS.tar.gz
```

Ou via le wrapper historique :

```bash
scripts/restore.sh backups/postgres_YYYYMMDD_HHMMSS.dump backups/media_YYYYMMDD_HHMMSS.tar.gz
```

## Analyse IA

L'analyse IA est centralisee dans `core/services/document_ai.py`.

Le service appelle directement l'API OpenAI. Il ne retourne pas de donnees
simulees et n'invente pas de numero, date, montant ou ligne produit.

Configurer OpenAI dans `.env` ou dans les variables d'environnement du serveur :

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
```

En developpement local uniquement, il est aussi possible d'utiliser un fichier
local `keys.json` monte en lecture seule dans Docker :

```bash
printf '{"OPENAI_API_KEY":"","OPENAI_MODEL":"gpt-4.1-mini"}\n' > keys.json
chmod 600 keys.json
docker compose up --build
```

Le fichier local doit respecter cette forme :

```json
{
  "OPENAI_API_KEY": "sk-proj-...",
  "OPENAI_MODEL": "gpt-4.1-mini"
}
```

`keys.json` ne doit jamais etre envoye sur Git, ne doit jamais etre copie dans
l'image Docker, et ne doit etre utilise qu'en developpement. En production,
utiliser uniquement les variables d'environnement `OPENAI_API_KEY` et
`OPENAI_MODEL`.

Si `OPENAI_API_KEY` et `OPENAI_MODEL` sont definies dans l'environnement, elles
sont prioritaires sur `keys.json`. Le chargement depuis `keys.json` est refuse
quand `DEBUG=False`.

Lors de l'upload d'une facture ou d'un bon de livraison, l'application envoie le
document a l'API OpenAI via la Responses API, demande une reponse JSON stricte,
puis pre-remplit les champs principaux. Les PDF sont envoyes en Base64 comme
`input_file`; les images JPG, JPEG et PNG sont envoyees en Base64 comme
`input_image`. Le document reste toujours rattache au fournisseur choisi par
l'utilisateur et passe seulement au statut `a_valider`. L'IA ne valide jamais
automatiquement un document.

Tester l'analyse directement sur un fichier local dans le conteneur :

```bash
docker compose exec web python manage.py test_ai_invoice /app/media/factures/test.pdf
docker compose exec web python manage.py test_ai_delivery /app/media/bons_livraison/test.pdf
```

Si `OPENAI_API_KEY` est absente, le fichier importe est conserve et
l'utilisateur voit :

```text
Document importé, mais OpenAI n’est pas configuré.
```

Si l'analyse echoue, le fichier importe est conserve et l'utilisateur voit le
message suivant :

```text
Document importé, mais l’analyse automatique a échoué.
```

Ne jamais committer de vraie cle API dans le depot.

## Règles de développement

Avant toute modification du projet, lire :

- `AI_RULES.md`
- `PROJECT_CONTEXT.md`

Ces fichiers définissent les règles métier, techniques et UX à respecter pour le projet Facture.

## Améliorations de contrôle et d’exploitation

Les 19 améliorations approuvées sont détaillées dans [le guide d’utilisation et d’exploitation](docs/ameliorations-19.md). Le traitement des documents requiert désormais le worker `python manage.py process_analysis_jobs`, également défini dans Docker Compose. Le lancement local `.local/start.sh` le démarre automatiquement.

Les sauvegardes contiennent un manifeste des documents et leurs empreintes. La restauration vérifie une base et des fichiers temporaires avant toute bascule. `scripts/install-backup-timer.sh` installe une sauvegarde quotidienne utilisateur ; ajouter `--local` pour le lancement local Podman. Le profil `docker-compose.production.yml` utilise Gunicorn et impose les variables de production.

La page [Catalogues](docs/catalogues.md) regroupe désormais les produits de chaque fournisseur issus de ses BL et factures. Les prix relevés et les documents d’origine restent distincts des prix catalogue HT confirmés. La commande `build_supplier_catalogues` propose un aperçu ; `--apply --actor ID_ADMIN` applique l’enrichissement sans écraser les tarifs confirmés.

Le [dépôt mobile](docs/depot-mobile.md) reprend le style de la revue des prix et propose la photo ou le fichier, l’aperçu des pièces, leur retrait avant envoi et un reçu. Les erreurs laissent la sélection disponible ; le restaurant est déduit uniquement lorsqu’un seul restaurant est autorisé.

La charte commune, les rôles Réception/Revue et les prix initialisés depuis les documents sont décrits dans [docs/catalogues-prix-roles-design.md](docs/catalogues-prix-roles-design.md). « Ajouter un produit » ouvre désormais une saisie ; « Actualiser depuis les documents » enrichit le catalogue depuis les BL et factures.

Le menu mobile repliable, les nouveaux logos et la saisie administrateur de la configuration IA sont décrits dans [docs/navigation-logo-ia.md](docs/navigation-logo-ia.md). Après saisie de la clé dans « Analyse IA », testez la connexion avant de relancer les documents en échec.
