#!/usr/bin/env bash
# Sauvegarde du lancement local Podman ; les identifiants restent dans .local/env.
set -euo pipefail
umask 077
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
source .local/env
TASK_BACKUPS="$TASK_ROOT/.local/backups"
mkdir -p "$TASK_BACKUPS"
exec 9>"$TASK_ROOT/.local/backup.lock"
flock -n 9 || exit 0
TASK_STAMP="$(date +%Y%m%d_%H%M%S)"
TASK_DUMP="$TASK_BACKUPS/postgres_$TASK_STAMP.dump"
TASK_MEDIA="$TASK_BACKUPS/media_$TASK_STAMP.tar.gz"
.venv/bin/python manage.py verify_backup > "$TASK_DUMP.manifest.json"
podman exec aizy-local-db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$TASK_DUMP"
tar -czf "$TASK_MEDIA" -C "$TASK_ROOT" media
sha256sum "$TASK_DUMP" "$TASK_MEDIA" > "$TASK_DUMP.sha256"
echo "Sauvegarde locale créée : $TASK_STAMP"
