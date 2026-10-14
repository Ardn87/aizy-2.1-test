#!/bin/sh
# Commandes locales pour une application créée dans NewCode.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_NAME=$(printf %s "$(basename "$ROOT")" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-' | sed 's/[-_]*$//')
PROJECT_NAME=${PROJECT_NAME:-app}
PROJECT="newcode_${PROJECT_NAME}_$(printf %s "$ROOT" | cksum | awk '{print $1}')"

fail() { printf 'Erreur : %s\n' "$*" >&2; exit 1; }

select_contract() {
  BASE=$ROOT
  if [ -e "$ROOT/.newcode" ] || [ -L "$ROOT/.newcode" ]; then
    [ -d "$ROOT/.newcode" ] && [ ! -L "$ROOT/.newcode" ] || fail '.newcode doit être un répertoire ordinaire.'
    BASE=$ROOT/.newcode
  fi
  COMPOSE=
  count=0
  for name in compose.yaml compose.yml docker-compose.yml docker-compose.yaml; do
    candidate=$BASE/$name
    if [ -e "$candidate" ] || [ -L "$candidate" ]; then
      [ -f "$candidate" ] && [ ! -L "$candidate" ] || fail "Fichier Compose invalide : $name"
      COMPOSE=$candidate
      count=$((count + 1))
    fi
  done
  [ "$count" -eq 1 ] || fail "Il faut exactement un fichier Compose dans $BASE. L'application doit encore être préparée."
  MANIFEST=$BASE/app-factory.json
  [ -f "$MANIFEST" ] && [ ! -L "$MANIFEST" ] || fail "Le manifeste $MANIFEST manque. L'application doit encore être préparée."
}

compose() {
  docker compose --project-name "$PROJECT" --project-directory "$ROOT" --env-file /dev/null --file "$COMPOSE" "$@"
}

doctor() {
  select_contract
  command -v docker >/dev/null 2>&1 || fail 'Docker est nécessaire sur cet hôte.'
  docker compose version >/dev/null 2>&1 || fail 'Le module Docker Compose est nécessaire sur cet hôte.'
  compose config --quiet || fail 'Le fichier Compose est invalide.'
  printf 'Contrat local prêt : %s\n' "$COMPOSE"
  printf 'Avant une publication NewCode, vérifiez aussi le healthcheck et le manifeste dans l’atelier.\n'
}

case "${1:-}" in
  doctor) doctor ;;
  install) doctor; compose up --build --detach ;;
  start) doctor; compose up --detach ;;
  stop) select_contract; compose down ;;
  restart) doctor; compose restart ;;
  status) select_contract; compose ps ;;
  logs) select_contract; shift; compose logs --tail=100 "$@" ;;
  credentials) select_contract; compose exec -T web cat /data/demo-credentials.json ;;
  *) printf 'Usage : %s {doctor|install|start|stop|restart|status|logs|credentials}\n' "$0" >&2; exit 2 ;;
esac
