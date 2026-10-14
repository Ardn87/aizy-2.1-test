#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR="$ROOT_DIR/backups"
MEDIA_DIR="$ROOT_DIR/media"
STATIC_DIR="$ROOT_DIR/staticfiles"
DEFAULT_PROJECT_NAME="aizy"
DEFAULT_WEB_PORT="8000"
DEFAULT_DEPLOY_MODE="local"
DEFAULT_RUNTIME_UID="1000"
DEFAULT_RUNTIME_GID="1000"

DOCKER_CMD=()
COMPOSE_CMD=()
PROJECT_NAME="$DEFAULT_PROJECT_NAME"
DEPLOY_MODE="$DEFAULT_DEPLOY_MODE"
OPENAI_CONFIGURED=0
CLOUDFLARE_CONFIGURED=0

info() {
  printf '%s\n' "$*"
}

warn() {
  printf 'Attention: %s\n' "$*" >&2
}

fatal() {
  printf 'Erreur: %s\n' "$*" >&2
  exit 1
}

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    fatal "sudo est requis pour installer ou demarrer les paquets systeme."
  fi
}

is_interactive() {
  [ -t 0 ] && [ -t 1 ]
}

confirm() {
  local prompt="$1"
  local answer
  if ! is_interactive; then
    return 1
  fi
  read -r -p "$prompt [o/N] " answer
  case "$answer" in
    o|O|oui|Oui|OUI|y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

trim_quotes() {
  local value="${1:-}"
  value="${value%$'\r'}"
  value="${value#\"}"
  value="${value%\"}"
  value="${value#\'}"
  value="${value%\'}"
  printf '%s' "$value"
}

env_get() {
  local key="$1"
  local line value
  [ -f ".env" ] || return 0
  line="$(grep -E "^[[:space:]]*${key}=" .env | tail -n 1 || true)"
  [ -n "$line" ] || return 0
  value="${line#*=}"
  trim_quotes "$value"
}

env_set() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  if [ -f ".env" ]; then
    awk -v key="$key" -v value="$value" '
      BEGIN { done = 0 }
      $0 ~ "^[[:space:]]*" key "=" {
        print key "=" value
        done = 1
        next
      }
      { print }
      END {
        if (!done) {
          print key "=" value
        }
      }
    ' .env > "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" > "$tmp"
  fi
  mv "$tmp" .env
}

ensure_env_key() {
  local key="$1"
  local default_value="${2:-}"
  if [ ! -f ".env" ] || ! grep -qE "^[[:space:]]*${key}=" .env; then
    printf '\n%s=%s\n' "$key" "$default_value" >> .env
  fi
}

json_get() {
  local key="$1"
  [ -f "keys.json" ] || return 0
  sed -nE "s/^[[:space:]]*\"${key}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*$/\1/p" keys.json | head -n 1
}

ensure_local_files() {
  if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
      cp .env.example .env
      info "Fichier .env cree depuis .env.example"
    else
      touch .env
      info "Fichier .env cree."
    fi
  fi

  ensure_env_key "COMPOSE_PROJECT_NAME" "$DEFAULT_PROJECT_NAME"
  ensure_env_key "WEB_PORT" "$DEFAULT_WEB_PORT"
  ensure_env_key "AIZY_DEPLOY_MODE" "$DEFAULT_DEPLOY_MODE"
  ensure_env_key "CLOUDFLARE_TUNNEL_TOKEN" ""
  ensure_env_key "OPENAI_API_KEY" ""
  ensure_env_key "OPENAI_MODEL" "gpt-4.1-mini"
  ensure_env_key "AIZY_RUNTIME_UID" "$DEFAULT_RUNTIME_UID"
  ensure_env_key "AIZY_RUNTIME_GID" "$DEFAULT_RUNTIME_GID"

  if [ ! -f "keys.json" ]; then
    if [ -f "keys.example.json" ]; then
      cp keys.example.json keys.json
      chmod 600 keys.json 2>/dev/null || true
      info "Fichier keys.json cree depuis keys.example.json"
    else
      printf '{\n  "OPENAI_API_KEY": "",\n  "OPENAI_MODEL": "gpt-4.1-mini"\n}\n' > keys.json
      chmod 600 keys.json 2>/dev/null || true
      info "Fichier keys.json cree avec une configuration vide"
    fi
  fi

  mkdir -p "$MEDIA_DIR" "$STATIC_DIR" "$BACKUP_DIR"
  PROJECT_NAME="$(env_get COMPOSE_PROJECT_NAME)"
  [ -n "$PROJECT_NAME" ] || PROJECT_NAME="$DEFAULT_PROJECT_NAME"
}

ensure_runtime_permissions() {
  local runtime_uid runtime_gid directory
  local directories=("$MEDIA_DIR" "$STATIC_DIR" "$BACKUP_DIR")

  runtime_uid="$(env_get AIZY_RUNTIME_UID)"
  runtime_gid="$(env_get AIZY_RUNTIME_GID)"
  runtime_uid="${runtime_uid:-$DEFAULT_RUNTIME_UID}"
  runtime_gid="${runtime_gid:-$DEFAULT_RUNTIME_GID}"

  if ! printf '%s' "$runtime_uid" | grep -Eq '^[0-9]+$'; then
    fatal "AIZY_RUNTIME_UID invalide: ${runtime_uid}"
  fi
  if ! printf '%s' "$runtime_gid" | grep -Eq '^[0-9]+$'; then
    fatal "AIZY_RUNTIME_GID invalide: ${runtime_gid}"
  fi

  mkdir -p "${directories[@]}"
  for directory in "${directories[@]}"; do
    if find "$directory" \( ! -uid "$runtime_uid" -o ! -gid "$runtime_gid" \) -print -quit | grep -q .; then
      info "Correction des droits Docker: ${directory} -> ${runtime_uid}:${runtime_gid}"
      run_root chown -R "${runtime_uid}:${runtime_gid}" "$directory"
    fi
    run_root chmod -R u+rwX "$directory"
  done
}

detect_os() {
  if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    printf '%s' "${ID:-unknown}"
  else
    printf 'unknown'
  fi
}

docker_compose_available() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

install_docker_debian() {
  local os_id codename arch repo_url
  # shellcheck disable=SC1091
  . /etc/os-release
  os_id="${ID:-debian}"
  codename="${VERSION_CODENAME:-stable}"
  arch="$(dpkg --print-architecture)"
  repo_url="https://download.docker.com/linux/debian"
  [ "$os_id" = "ubuntu" ] && repo_url="https://download.docker.com/linux/ubuntu"

  run_root apt-get update
  run_root apt-get install -y ca-certificates curl git tar gzip
  run_root install -m 0755 -d /etc/apt/keyrings

  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    run_root curl -fsSL "${repo_url}/gpg" -o /etc/apt/keyrings/docker.asc
    run_root chmod a+r /etc/apt/keyrings/docker.asc
  fi

  if [ ! -f /etc/apt/sources.list.d/docker.sources ]; then
    printf 'Types: deb\nURIs: %s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$repo_url" "$codename" "$arch" |
      run_root tee /etc/apt/sources.list.d/docker.sources >/dev/null
  fi

  run_root apt-get update
  run_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

install_docker_fedora() {
  local repo_file="/etc/yum.repos.d/docker-ce.repo"
  local repo_url="https://download.docker.com/linux/fedora/docker-ce.repo"

  run_root dnf install -y ca-certificates curl git tar gzip dnf-plugins-core

  if [ ! -f "$repo_file" ]; then
    if dnf config-manager --help 2>&1 | grep -q "addrepo"; then
      run_root dnf config-manager addrepo --from-repofile="$repo_url"
    elif dnf config-manager --help 2>&1 | grep -q -- "--add-repo"; then
      run_root dnf config-manager --add-repo "$repo_url"
    else
      run_root curl -fsSL "$repo_url" -o "$repo_file"
    fi
  fi

  run_root dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

enable_docker_service() {
  if command -v systemctl >/dev/null 2>&1; then
    run_root systemctl enable --now docker || true
  fi
}

ensure_system_dependencies() {
  local os_id
  os_id="$(detect_os)"

  if command -v docker >/dev/null 2>&1 && docker_compose_available; then
    enable_docker_service
    return 0
  fi

  info "Docker ou Docker Compose est absent."
  if is_interactive && ! confirm "Installer/mettre a jour Docker pour ${os_id} maintenant ?"; then
    fatal "Installation annulee."
  fi

  case "$os_id" in
    fedora) install_docker_fedora ;;
    debian|ubuntu) install_docker_debian ;;
    *)
      fatal "OS non gere automatiquement: ${os_id}. Installe Docker et le plugin docker compose, puis relance le script."
      ;;
  esac

  enable_docker_service
}

detect_docker_cmd() {
  DOCKER_CMD=()

  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER_CMD=(docker)
    return 0
  fi

  enable_docker_service

  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER_CMD=(docker)
    return 0
  fi

  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER_CMD=(sudo docker)
    return 0
  fi

  fatal "Docker ne repond pas. Sur Fedora, verifie que le service docker est demarre et que l'utilisateur a les droits necessaires."
}

docker_cmd() {
  "${DOCKER_CMD[@]}" "$@"
}

detect_compose() {
  COMPOSE_CMD=()

  if docker_cmd compose version >/dev/null 2>&1; then
    COMPOSE_CMD=("${DOCKER_CMD[@]}" compose)
    return 0
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    if [ "${DOCKER_CMD[0]:-}" = "sudo" ]; then
      COMPOSE_CMD=(sudo docker-compose)
    else
      COMPOSE_CMD=(docker-compose)
    fi
    return 0
  fi

  fatal "Docker Compose n'est pas disponible."
}

compose() {
  "${COMPOSE_CMD[@]}" -p "$PROJECT_NAME" "$@"
}

sanitize_project_name() {
  local value="$1"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g; s/^-+//; s/-+$//')"
  [ -n "$value" ] || value="$DEFAULT_PROJECT_NAME"
  case "$value" in
    [a-z0-9]*) ;;
    *) value="${DEFAULT_PROJECT_NAME}-${value}" ;;
  esac
  printf '%s' "$value"
}

normalize_path_if_exists() {
  local path="$1"
  if [ -n "$path" ] && [ -d "$path" ]; then
    (cd "$path" && pwd -P)
  else
    printf '%s' "$path"
  fi
}

project_name_collides() {
  local name="$1"
  local root_path container_id working_dir normalized_working_dir
  root_path="$(normalize_path_if_exists "$ROOT_DIR")"

  while IFS= read -r container_id; do
    [ -n "$container_id" ] || continue
    working_dir="$(docker_cmd inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container_id" 2>/dev/null || true)"
    normalized_working_dir="$(normalize_path_if_exists "$working_dir")"
    if [ -z "$normalized_working_dir" ] || [ "$normalized_working_dir" != "$root_path" ]; then
      return 0
    fi
  done < <(docker_cmd ps -a --filter "label=com.docker.compose.project=${name}" --format '{{.ID}}' 2>/dev/null || true)

  return 1
}

resolve_project_name() {
  local configured base candidate index
  configured="$(env_get COMPOSE_PROJECT_NAME)"
  base="$(sanitize_project_name "${configured:-$DEFAULT_PROJECT_NAME}")"
  candidate="$base"
  index=2

  while project_name_collides "$candidate"; do
    candidate="${base}-${index}"
    index=$((index + 1))
  done

  PROJECT_NAME="$candidate"
  if [ "$configured" != "$PROJECT_NAME" ]; then
    env_set "COMPOSE_PROJECT_NAME" "$PROJECT_NAME"
    info "Nom Docker Compose ajuste automatiquement: ${PROJECT_NAME}"
  fi
}

parse_web_port() {
  local raw="$1"
  local bind_host=""
  local port="$raw"

  raw="${raw:-$DEFAULT_WEB_PORT}"
  if printf '%s' "$raw" | grep -q ':'; then
    bind_host="${raw%:*}"
    port="${raw##*:}"
    bind_host="${bind_host#[}"
    bind_host="${bind_host%]}"
  fi

  if ! printf '%s' "$port" | grep -Eq '^[0-9]+$'; then
    fatal "WEB_PORT invalide: ${raw}"
  fi

  printf '%s|%s\n' "$bind_host" "$port"
}

format_web_port() {
  local bind_host="$1"
  local port="$2"
  if [ -n "$bind_host" ]; then
    printf '%s:%s' "$bind_host" "$port"
  else
    printf '%s' "$port"
  fi
}

docker_port_used_by_current_project() {
  local port="$1"
  local id project
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    project="$(docker_cmd inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$id" 2>/dev/null || true)"
    [ "$project" = "$PROJECT_NAME" ] && return 0
  done < <(docker_cmd ps --filter "publish=${port}" --format '{{.ID}}' 2>/dev/null || true)
  return 1
}

docker_port_conflict_name() {
  local port="$1"
  local id project name
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    project="$(docker_cmd inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$id" 2>/dev/null || true)"
    if [ "$project" != "$PROJECT_NAME" ]; then
      name="$(docker_cmd inspect -f '{{ .Name }}' "$id" 2>/dev/null | sed 's#^/##' || true)"
      printf '%s' "${name:-$id}"
      return 0
    fi
  done < <(docker_cmd ps --filter "publish=${port}" --format '{{.ID}}' 2>/dev/null || true)
  return 1
}

host_port_listening() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    ss -H -ltn "sport = :${port}" 2>/dev/null | grep -q .
    return $?
  fi

  if command -v python3 >/dev/null 2>&1; then
    python3 - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("0.0.0.0", port))
except OSError:
    sys.exit(0)
finally:
    sock.close()
sys.exit(1)
PY
    return $?
  fi

  return 1
}

port_available_for_project() {
  local port="$1"

  if docker_port_used_by_current_project "$port"; then
    return 0
  fi

  if docker_port_conflict_name "$port" >/dev/null; then
    return 1
  fi

  if host_port_listening "$port"; then
    return 1
  fi

  return 0
}

ensure_free_web_port() {
  local raw parsed bind_host port candidate candidate_raw conflict
  raw="$(env_get WEB_PORT)"
  [ -n "$raw" ] || raw="$DEFAULT_WEB_PORT"
  parsed="$(parse_web_port "$raw")"
  bind_host="${parsed%%|*}"
  port="${parsed##*|}"

  if port_available_for_project "$port"; then
    return 0
  fi

  conflict="$(docker_port_conflict_name "$port" || true)"
  if [ -n "$conflict" ]; then
    warn "le port ${port} est deja utilise par le conteneur Docker '${conflict}'."
  else
    warn "le port ${port} est deja utilise sur cette machine."
  fi

  candidate=$((port + 1))
  while [ "$candidate" -le $((port + 100)) ]; do
    if port_available_for_project "$candidate"; then
      candidate_raw="$(format_web_port "$bind_host" "$candidate")"
      env_set "WEB_PORT" "$candidate_raw"
      info "WEB_PORT ajuste automatiquement: ${candidate_raw}"
      return 0
    fi
    candidate=$((candidate + 1))
  done

  fatal "aucun port libre trouve entre ${port} et $((port + 100))."
}

show_url() {
  local web_port
  web_port="$(env_get WEB_PORT)"
  [ -n "$web_port" ] || web_port="$DEFAULT_WEB_PORT"

  case "$web_port" in
    127.0.0.1:*|localhost:*) info "Application locale: http://${web_port}" ;;
    *:*) info "Application locale: http://${web_port}" ;;
    *) info "Application locale: http://localhost:${web_port}" ;;
  esac
}

is_secret_set() {
  local value="$1"
  [ -n "$value" ] &&
    [ "$value" != "change-me" ] &&
    [ "$value" != "change-me-in-production" ] &&
    [ "$value" != "sk-..." ] &&
    [ "$value" != "null" ]
}

append_csv_value() {
  local key="$1"
  local value="$2"
  local current
  current="$(env_get "$key")"
  if [ -z "$current" ]; then
    env_set "$key" "$value"
  elif ! printf ',%s,' "$current" | grep -Fq ",${value},"; then
    env_set "$key" "${current},${value}"
  fi
}

ensure_default_allowed_hosts() {
  append_csv_value "DJANGO_ALLOWED_HOSTS" "localhost"
  append_csv_value "DJANGO_ALLOWED_HOSTS" "127.0.0.1"
  append_csv_value "DJANGO_ALLOWED_HOSTS" "0.0.0.0"
}

normalize_deploy_mode() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    local|loc|dev|developpement) printf '%s' "local" ;;
    server|serveur|prod|production|cloudflare) printf '%s' "server" ;;
    *) printf '%s' "$DEFAULT_DEPLOY_MODE" ;;
  esac
}

load_deploy_mode() {
  local configured normalized
  configured="$(env_get AIZY_DEPLOY_MODE)"
  normalized="$(normalize_deploy_mode "${configured:-$DEFAULT_DEPLOY_MODE}")"
  DEPLOY_MODE="$normalized"
  if [ "$configured" != "$normalized" ]; then
    env_set "AIZY_DEPLOY_MODE" "$normalized"
  fi
}

set_deploy_mode() {
  local requested="$1"
  DEPLOY_MODE="$(normalize_deploy_mode "$requested")"
  env_set "AIZY_DEPLOY_MODE" "$DEPLOY_MODE"
  case "$DEPLOY_MODE" in
    server) info "Mode d'execution: serveur avec tunnel Cloudflare" ;;
    *) info "Mode d'execution: local sans tunnel Cloudflare" ;;
  esac
}

select_deploy_mode() {
  local requested="${1:-}"
  local choice
  load_deploy_mode

  if [ -n "$requested" ]; then
    set_deploy_mode "$requested"
    return 0
  fi

  if ! is_interactive; then
    return 0
  fi

  info
  info "Mode d'execution:"
  info "1) Local  - application locale sans tunnel Cloudflare"
  info "2) Serveur - application publiee avec tunnel Cloudflare"
  read -r -p "Choix [mode actuel: ${DEPLOY_MODE}] : " choice
  case "$choice" in
    1|local|Local|LOCAL) set_deploy_mode "local" ;;
    2|server|serveur|Serveur|SERVER|SERVEUR) set_deploy_mode "server" ;;
    "") info "Mode conserve: ${DEPLOY_MODE}" ;;
    *) warn "choix invalide, mode conserve: ${DEPLOY_MODE}" ;;
  esac
}

is_server_mode() {
  [ "$DEPLOY_MODE" = "server" ]
}

check_runtime_configuration() {
  local openai_key cloudflare_token debug_value value public_host start_command
  load_deploy_mode
  ensure_default_allowed_hosts

  openai_key="$(env_get OPENAI_API_KEY)"
  if ! is_secret_set "$openai_key"; then
    openai_key="$(json_get OPENAI_API_KEY)"
  fi

  cloudflare_token="$(env_get CLOUDFLARE_TUNNEL_TOKEN)"
  debug_value="$(env_get DJANGO_DEBUG)"

  OPENAI_CONFIGURED=0
  CLOUDFLARE_CONFIGURED=0
  is_secret_set "$openai_key" && OPENAI_CONFIGURED=1
  if is_server_mode && is_secret_set "$cloudflare_token"; then
    CLOUDFLARE_CONFIGURED=1
  fi

  info
  info "Verification configuration:"
  if is_server_mode; then
    info "- Mode: serveur avec tunnel Cloudflare"
  else
    info "- Mode: local sans tunnel Cloudflare"
  fi

  if [ "$OPENAI_CONFIGURED" -eq 1 ]; then
    info "- OpenAI API key: OK"
  else
    info "- OpenAI API key: MANQUANTE"
  fi

  if ! is_server_mode; then
    info "- Cloudflare tunnel token: non requis en mode local"
  elif [ "$CLOUDFLARE_CONFIGURED" -eq 1 ]; then
    info "- Cloudflare tunnel token: OK"
  else
    info "- Cloudflare tunnel token: MANQUANT"
  fi

  if [ "${debug_value:-1}" = "0" ] && ! is_secret_set "$(env_get OPENAI_API_KEY)"; then
    warn "DJANGO_DEBUG=0, la cle OpenAI doit etre dans .env et pas seulement dans keys.json."
  fi

  if is_interactive && [ "$OPENAI_CONFIGURED" -eq 0 ]; then
    read -r -s -p "Saisir OPENAI_API_KEY maintenant ? Laisser vide pour ignorer: " value
    info
    if [ -n "$value" ]; then
      env_set "OPENAI_API_KEY" "$value"
      OPENAI_CONFIGURED=1
      info "OPENAI_API_KEY enregistree dans .env"
    fi
  fi

  if is_server_mode && is_interactive && [ "$CLOUDFLARE_CONFIGURED" -eq 0 ]; then
    read -r -s -p "Saisir CLOUDFLARE_TUNNEL_TOKEN maintenant ? Laisser vide pour ignorer: " value
    info
    if [ -n "$value" ]; then
      env_set "CLOUDFLARE_TUNNEL_TOKEN" "$value"
      CLOUDFLARE_CONFIGURED=1
      info "CLOUDFLARE_TUNNEL_TOKEN enregistre dans .env"
    fi
  fi

  if is_server_mode && [ "$CLOUDFLARE_CONFIGURED" -eq 0 ]; then
    fatal "le mode serveur necessite CLOUDFLARE_TUNNEL_TOKEN dans .env."
  fi

  if is_server_mode; then
    start_command="$(env_get DJANGO_START_COMMAND)"
    if [ -z "$start_command" ] || printf '%s' "$start_command" | grep -q "runserver"; then
      env_set "DJANGO_START_COMMAND" "gunicorn facture_project.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 60"
      info "- Django: demarrage Gunicorn active pour le tunnel Cloudflare"
    fi
    if [ "$(env_get DJANGO_USE_X_FORWARDED_PROTO)" != "1" ]; then
      env_set "DJANGO_USE_X_FORWARDED_PROTO" "1"
      info "- Django: X-Forwarded-Proto active pour Cloudflare"
    fi

    if is_interactive; then
      public_host="$(env_get PUBLIC_HOSTNAME)"
      if [ -z "$public_host" ]; then
        read -r -p "Domaine public Cloudflare (ex: aizy.sogeta.com, vide pour ignorer): " public_host
        if [ -n "$public_host" ]; then
          env_set "PUBLIC_HOSTNAME" "$public_host"
        fi
      fi
      if [ -n "$public_host" ]; then
        append_csv_value "DJANGO_ALLOWED_HOSTS" "$public_host"
        append_csv_value "CSRF_TRUSTED_ORIGINS" "https://${public_host}"
      fi
    fi
  else
    CLOUDFLARE_CONFIGURED=0
  fi
}

check_docker() {
  detect_docker_cmd
}

prepare_docker_runtime() {
  load_deploy_mode
  check_runtime_configuration
  ensure_runtime_permissions
  check_docker
  detect_compose
  resolve_project_name
}

services_to_start() {
  if is_server_mode && [ "$CLOUDFLARE_CONFIGURED" -eq 1 ]; then
    printf '%s\n' "db" "web" "tunnel"
  else
    printf '%s\n' "db" "web"
  fi
}

wait_service_state() {
  local service="$1"
  local expected="$2"
  local timeout="${3:-120}"
  local id status health waited
  waited=0

  while [ "$waited" -lt "$timeout" ]; do
    id="$(compose ps -q "$service" 2>/dev/null | tail -n 1 || true)"
    if [ -n "$id" ]; then
      status="$(docker_cmd inspect -f '{{ .State.Status }}' "$id" 2>/dev/null || true)"
      health="$(docker_cmd inspect -f '{{ if .State.Health }}{{ .State.Health.Status }}{{ end }}' "$id" 2>/dev/null || true)"
      if [ "$expected" = "healthy" ]; then
        [ "$health" = "healthy" ] && return 0
      elif [ "$status" = "$expected" ]; then
        return 0
      fi
    fi
    sleep 1
    waited=$((waited + 1))
  done

  compose logs --tail=80 "$service" || true
  fatal "le service '${service}' n'a pas atteint l'etat '${expected}' apres ${timeout}s."
}

wait_started_services() {
  wait_service_state "db" "healthy" 120
  wait_service_state "web" "healthy" 180
  if is_server_mode && [ "$CLOUDFLARE_CONFIGURED" -eq 1 ]; then
    wait_service_state "tunnel" "running" 90
  fi
}

compose_up() {
  local services=()
  mapfile -t services < <(services_to_start)
  if ! is_server_mode; then
    compose stop tunnel >/dev/null 2>&1 || true
  fi
  if ! compose up -d "${services[@]}"; then
    compose ps || true
    compose logs --tail=120 web || true
    [ "$CLOUDFLARE_CONFIGURED" -eq 1 ] && compose logs --tail=80 tunnel || true
    fatal "docker compose up a echoue."
  fi
  wait_started_services
}

compose_up_build() {
  local services=()
  mapfile -t services < <(services_to_start)
  if ! is_server_mode; then
    compose stop tunnel >/dev/null 2>&1 || true
  fi
  if ! compose up --build -d "${services[@]}"; then
    compose ps || true
    compose logs --tail=120 web || true
    [ "$CLOUDFLARE_CONFIGURED" -eq 1 ] && compose logs --tail=80 tunnel || true
    fatal "docker compose up --build a echoue."
  fi
  wait_started_services
}

post_start_message() {
  show_url
  if is_server_mode && [ "$CLOUDFLARE_CONFIGURED" -eq 1 ]; then
    info "Tunnel Cloudflare: demarre via le service docker compose 'tunnel'."
  elif is_server_mode; then
    info "Tunnel Cloudflare: non demarre, token manquant dans .env."
  else
    info "Tunnel Cloudflare: desactive en mode local."
  fi
  info "Projet Docker Compose: ${PROJECT_NAME}"
}

backup_pair_lines() {
  local db_backup base timestamp media_backup
  shopt -s nullglob
  for db_backup in "$BACKUP_DIR"/postgres_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9].dump; do
    base="$(basename "$db_backup")"
    timestamp="${base#postgres_}"
    timestamp="${timestamp%.dump}"
    media_backup="$BACKUP_DIR/media_${timestamp}.tar.gz"
    [ -f "$media_backup" ] || continue
    printf '%s|%s|%s\n' "$timestamp" "$db_backup" "$media_backup"
  done | sort -r
}

latest_backup_pair() {
  backup_pair_lines | head -n 1
}

print_backup_pairs() {
  local line index timestamp db_backup media_backup
  index=1
  while IFS='|' read -r timestamp db_backup media_backup; do
    [ -n "$timestamp" ] || continue
    printf '%2d) %s\n    DB:    %s\n    Media: %s\n' "$index" "$timestamp" "$db_backup" "$media_backup"
    index=$((index + 1))
  done < <(backup_pair_lines)
}

create_backup_files() {
  local prefix="${1:-}"
  local timestamp db_backup media_backup
  timestamp="$(date +%Y%m%d_%H%M%S)"
  if [ -n "$prefix" ]; then
    db_backup="$BACKUP_DIR/postgres_${prefix}_${timestamp}.dump"
    media_backup="$BACKUP_DIR/media_${prefix}_${timestamp}.tar.gz"
  else
    db_backup="$BACKUP_DIR/postgres_${timestamp}.dump"
    media_backup="$BACKUP_DIR/media_${timestamp}.tar.gz"
  fi

  mkdir -p "$BACKUP_DIR" "$MEDIA_DIR"

  info "Backup PostgreSQL vers ${db_backup}"
  compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$db_backup"

  info "Compression du dossier media vers ${media_backup}"
  tar -czf "$media_backup" -C "$ROOT_DIR" media

  compose run --rm -T --no-deps web python manage.py verify_backup > "${db_backup}.manifest.json" || {
    warn "Verification du backup echouee : corriger les fichiers manquants avant une restauration."; return 1;
  }
  sha256sum "$db_backup" "$media_backup" > "${db_backup}.sha256"
  info "Backup termine."
  info "Base PostgreSQL : ${db_backup}"
  info "Fichiers media  : ${media_backup}"
}

backup_project() {
  info
  info "Backup du projet..."
  prepare_docker_runtime
  compose up -d db
  wait_service_state "db" "healthy" 120
  create_backup_files ""
}

safe_replace_media() {
  local root_path target_path
  root_path="$(normalize_path_if_exists "$ROOT_DIR")"
  mkdir -p "$MEDIA_DIR"
  target_path="$(normalize_path_if_exists "$MEDIA_DIR")"
  case "$target_path" in
    "$root_path"/media) ;;
    *) fatal "chemin media inattendu: ${target_path}" ;;
  esac

  rm -rf -- "$MEDIA_DIR"
  mkdir -p "$MEDIA_DIR"
}

restore_backup_pair() {
  local db_backup="$1"
  local media_backup="$2"
  local require_confirmation="${3:-1}"
  local restart_after="${4:-1}"
  local db_name confirmation

  [ -f "$db_backup" ] || fatal "dump PostgreSQL introuvable: ${db_backup}"
  [ -f "$media_backup" ] || fatal "archive media introuvable: ${media_backup}"

  compose up -d db
  wait_service_state "db" "healthy" 120

  db_name="$(compose exec -T db printenv POSTGRES_DB | tr -d '\r')"

  info
  info "Restauration demandee:"
  info "  Base  : ${db_backup}"
  info "  Media : ${media_backup}"
  info "  Projet: ${PROJECT_NAME}"
  info "  DB    : ${db_name}"

  if [ "$require_confirmation" = "1" ]; then
    if ! is_interactive; then
      fatal "restauration non interactive refusee sans confirmation."
    fi
    read -r -p "Cette operation remplace la base et media. Taper RESTORE pour confirmer: " confirmation
    [ "$confirmation" = "RESTORE" ] || fatal "restauration annulee."
  fi

  # Valider l'archive avant toute interruption ou remplacement.
  local stage_dir stage_db old_db stamp backup_media
  stamp="$(date +%Y%m%d%H%M%S)"
  stage_dir="$ROOT_DIR/.restore-stage-$stamp"
  stage_db="aizy_restore_$stamp"
  old_db="aizy_previous_$stamp"
  backup_media="$ROOT_DIR/media.before-restore-$stamp"
  case "$db_name" in
    *[!a-zA-Z0-9_]*|'') fatal "nom de base non pris en charge pour une bascule sure." ;;
  esac
  python3 "$ROOT_DIR/scripts/verify_backup_pair.py" "$db_backup" "$media_backup"
  python3 "$ROOT_DIR/scripts/media_archive.py" "$media_backup" "$stage_dir"
  compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$stage_db"
  if ! compose exec -T db sh -c 'pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$1"' sh "$stage_db" < "$db_backup"; then
    fatal "dump invalide : base courante conservee, zone de restauration disponible pour diagnostic."
  fi
  compose run --rm -T --no-deps -e POSTGRES_DB="$stage_db" -e MEDIA_ROOT="/app/.restore-stage-$stamp/media" web python manage.py migrate --noinput
  if [ -f "${db_backup}.manifest.json" ]; then
    cp "${db_backup}.manifest.json" "$stage_dir/manifest.json"
    compose run --rm -T --no-deps -e POSTGRES_DB="$stage_db" -e MEDIA_ROOT="/app/.restore-stage-$stamp/media" web python manage.py verify_backup --manifest="/app/.restore-stage-$stamp/manifest.json"
  else
    compose run --rm -T --no-deps -e POSTGRES_DB="$stage_db" -e MEDIA_ROOT="/app/.restore-stage-$stamp/media" web python manage.py verify_backup
  fi

  info "Arret des services puis sauvegarde de securite obligatoire"
  compose stop tunnel worker web
  if ! create_backup_files "before_restore"; then
    compose start web worker tunnel || true
    fatal "backup de securite impossible : bascule annulee."
  fi

  # Renommage transactionnel : l'ancienne base reste disponible, aucun dropdb.
  if ! compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' <<SQL
BEGIN;
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('$db_name','$stage_db') AND pid <> pg_backend_pid();
ALTER DATABASE "$db_name" RENAME TO "$old_db";
ALTER DATABASE "$stage_db" RENAME TO "$db_name";
COMMIT;
SQL
  then
    compose start web worker tunnel || true
    fatal "bascule base echouee : fichiers courants conserves."
  fi
  if ! mv "$MEDIA_DIR" "$backup_media" || ! mv "$stage_dir/media" "$MEDIA_DIR"; then
    compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' <<SQL
BEGIN;
ALTER DATABASE "$db_name" RENAME TO "$stage_db";
ALTER DATABASE "$old_db" RENAME TO "$db_name";
COMMIT;
SQL
    if [ -d "$backup_media" ] && [ ! -d "$MEDIA_DIR" ]; then mv "$backup_media" "$MEDIA_DIR"; fi
    compose start web worker tunnel || true
    fatal "bascule media echouee : restauration de l'ancienne base effectuee."
  fi
  ensure_runtime_permissions
  info "Ancienne base conservee : $old_db ; anciens fichiers : $backup_media"
  if [ "$restart_after" = "1" ]; then
    ensure_free_web_port
    compose_up
  fi

  info "Restauration terminee."
}

select_backup_pair_interactive() {
  local count choice line
  count="$(backup_pair_lines | sed '/^$/d' | wc -l | tr -d ' ')"
  [ "$count" -gt 0 ] || fatal "aucun couple de backup postgres_*.dump + media_*.tar.gz trouve dans ${BACKUP_DIR}."

  print_backup_pairs
  read -r -p "Numero du backup a restaurer [1] : " choice
  choice="${choice:-1}"
  if ! printf '%s' "$choice" | grep -Eq '^[0-9]+$' || [ "$choice" -lt 1 ] || [ "$choice" -gt "$count" ]; then
    fatal "choix invalide."
  fi

  line="$(backup_pair_lines | sed -n "${choice}p")"
  printf '%s\n' "$line"
}

restore_project() {
  local line timestamp db_backup media_backup
  info
  info "Restauration d'un backup..."

  if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    cat <<EOF
Usage:
  ./script.sh restore
  ./script.sh restore --latest
  ./script.sh restore backups/postgres_YYYYMMDD_HHMMSS.dump backups/media_YYYYMMDD_HHMMSS.tar.gz

La restauration remplace la base PostgreSQL et le dossier media du projet
Docker Compose courant. Une sauvegarde de securite before_restore est creee
avant le remplacement.
EOF
    return 0
  fi

  prepare_docker_runtime

  if [ "${1:-}" = "--latest" ]; then
    line="$(latest_backup_pair)"
  elif [ -n "${1:-}" ] && [ -n "${2:-}" ]; then
    db_backup="$1"
    media_backup="$2"
    restore_backup_pair "$db_backup" "$media_backup" 1 1
    return 0
  elif is_interactive; then
    line="$(select_backup_pair_interactive)"
  else
    line="$(latest_backup_pair)"
  fi

  [ -n "$line" ] || fatal "aucun couple de backup postgres_*.dump + media_*.tar.gz trouve dans ${BACKUP_DIR}."
  timestamp="${line%%|*}"
  db_backup="$(printf '%s' "$line" | cut -d '|' -f 2)"
  media_backup="$(printf '%s' "$line" | cut -d '|' -f 3)"
  info "Backup selectionne: ${timestamp}"
  restore_backup_pair "$db_backup" "$media_backup" 1 1
}

auto_restore_latest_backup_on_install() {
  local line timestamp db_backup media_backup
  line="$(latest_backup_pair)"
  [ -n "$line" ] || return 0

  timestamp="${line%%|*}"
  db_backup="$(printf '%s' "$line" | cut -d '|' -f 2)"
  media_backup="$(printf '%s' "$line" | cut -d '|' -f 3)"

  info
  info "Backup detecte pour installation: ${timestamp}"
  info "Restauration automatique du couple le plus recent."
  restore_backup_pair "$db_backup" "$media_backup" 0 1
}

install_project() {
  apply_deploy_mode_arg "${1:-}"
  info
  info "Installation du projet..."
  ensure_system_dependencies
  prepare_docker_runtime
  ensure_free_web_port

  compose_up_build
  auto_restore_latest_backup_on_install
  compose exec -T web python manage.py init_roles

  info
  info "Installation terminee."
  post_start_message
  info

  if confirm "Creer un superutilisateur maintenant ?"; then
    compose exec web python manage.py createsuperuser
  else
    info "Creation du superutilisateur ignoree."
  fi
}

start_project() {
  apply_deploy_mode_arg "${1:-}"
  info
  info "Demarrage du projet..."
  prepare_docker_runtime
  ensure_free_web_port
  compose_up
  post_start_message
}

restart_project() {
  apply_deploy_mode_arg "${1:-}"
  info
  info "Redemarrage du projet..."
  prepare_docker_runtime
  ensure_free_web_port
  compose down
  compose_up
  post_start_message
}

stop_project() {
  info
  info "Arret du projet..."
  check_docker
  detect_compose
  resolve_project_name
  compose down
  info "Projet stoppe. Les autres projets Docker ne sont pas modifies."
}

status_project() {
  info
  info "Etat du projet..."
  prepare_docker_runtime
  compose ps
  show_url
}

mode_project() {
  select_deploy_mode "${1:-}"
}

apply_deploy_mode_arg() {
  if [ -n "${1:-}" ]; then
    set_deploy_mode "$1"
  else
    load_deploy_mode
  fi
}

show_menu() {
  load_deploy_mode
  clear 2>/dev/null || true
  info "======================================"
  info " AIZY - Gestion du projet"
  info " Projet Docker: ${PROJECT_NAME}"
  info " Mode: ${DEPLOY_MODE}"
  info "======================================"
  info "1) Installer le projet"
  info "2) Demarrer le projet"
  info "3) Redemarrer le projet"
  info "4) Stopper le projet"
  info "5) Backup base + media"
  info "6) Restaurer un backup"
  info "7) Voir l'etat Docker"
  info "8) Activer mode local (sans tunnel)"
  info "9) Activer mode serveur (avec tunnel)"
  info "10) Quitter"
  info
}

run_menu() {
  local choice
  while true; do
    show_menu
    read -r -p "Choix [1-10] : " choice
    case "$choice" in
      1) install_project; break ;;
      2) start_project; break ;;
      3) restart_project; break ;;
      4) stop_project; break ;;
      5) backup_project; break ;;
      6) restore_project; break ;;
      7) status_project; break ;;
      8) mode_project local; sleep 1 ;;
      9) mode_project server; sleep 1 ;;
      10|q|Q) info "Annule."; exit 0 ;;
      *) info "Choix invalide."; sleep 1 ;;
    esac
  done
}

usage() {
  cat <<EOF
Usage:
  ./script.sh
  ./script.sh install [local|server]
  ./script.sh start [local|server]
  ./script.sh restart [local|server]
  ./script.sh stop
  ./script.sh backup
  ./script.sh restore [--latest|postgres.dump media.tar.gz]
  ./script.sh status
  ./script.sh mode [local|server]

Le script utilise toujours le projet Docker Compose configure par
COMPOSE_PROJECT_NAME et ne stoppe pas les autres projets Docker.
EOF
}

main() {
  local action="${1:-menu}"
  shift || true

  ensure_local_files

  case "$action" in
    menu) run_menu ;;
    install) install_project "$@" ;;
    start) start_project "$@" ;;
    restart) restart_project "$@" ;;
    stop) stop_project ;;
    backup) backup_project ;;
    restore) restore_project "$@" ;;
    status) status_project ;;
    mode) mode_project "$@" ;;
    list-backups) print_backup_pairs ;;
    -h|--help|help) usage ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"
