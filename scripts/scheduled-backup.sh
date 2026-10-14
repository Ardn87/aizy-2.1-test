#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec flock -n "$TASK_ROOT/.backup.lock" "$TASK_ROOT/scripts/backup.sh"
