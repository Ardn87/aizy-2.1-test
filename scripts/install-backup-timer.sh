#!/usr/bin/env bash
# Installer un timer utilisateur ; aucun service système ni serveur distant modifié.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_SCRIPT="$TASK_ROOT/scripts/scheduled-backup.sh"
if [[ "${1:-}" == "--local" ]]; then TASK_SCRIPT="$TASK_ROOT/scripts/local-backup.sh"; fi
TASK_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$TASK_CONFIG"
cat > "$TASK_CONFIG/aizy-backup.service" <<EOF
[Unit]
Description=Sauvegarde Aizy et verification des documents
[Service]
Type=oneshot
WorkingDirectory=$TASK_ROOT
ExecStart="$TASK_SCRIPT"
EOF
cp "$TASK_ROOT/scripts/aizy-backup.timer" "$TASK_CONFIG/aizy-backup.timer"
systemctl --user daemon-reload
systemctl --user enable --now aizy-backup.timer
systemctl --user list-timers aizy-backup.timer --no-pager
