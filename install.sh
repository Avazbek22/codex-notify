#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPOSITORY="https://github.com/Avazbek22/codex-notify.git"
DEPLOY_REF="deploy"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-codex-notify}"
SERVICE_KEY="codex-notify"
BOT_UID="${BOT_UID:-10001}"
BOT_GID="${BOT_GID:-10001}"

if [[ -d "$SCRIPT_DIR/.git" ]]; then
  INSTALL_DIR="${INSTALL_DIR:-$SCRIPT_DIR}"
  REPOSITORY="${REPOSITORY:-$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || true)}"
else
  INSTALL_DIR="${INSTALL_DIR:-/opt/codex-notify}"
  REPOSITORY="${REPOSITORY:-$DEFAULT_REPOSITORY}"
fi
[[ -n "$REPOSITORY" ]] || REPOSITORY="$DEFAULT_REPOSITORY"

info() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [[ "$(id -u)" == "0" ]]; then "$@"; else need sudo || die "sudo is required"; sudo "$@"; fi
}

docker_cmd() {
  if docker info >/dev/null 2>&1; then docker "$@"; else as_root docker "$@"; fi
}

compose() {
  if docker_cmd compose version >/dev/null 2>&1; then
    docker_cmd compose -p "$COMPOSE_PROJECT" -f "$INSTALL_DIR/docker-compose.yml" "$@"
  elif need docker-compose; then
    if docker info >/dev/null 2>&1; then
      docker-compose -p "$COMPOSE_PROJECT" -f "$INSTALL_DIR/docker-compose.yml" "$@"
    else
      as_root docker-compose -p "$COMPOSE_PROJECT" -f "$INSTALL_DIR/docker-compose.yml" "$@"
    fi
  else
    die "Docker Compose is unavailable"
  fi
}

env_value() {
  local key="$1" file="$2"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$file"
}

set_env_value() {
  local key="$1" value="$2" file="$3" temporary="$3.tmp"
  ENV_WRITE_VALUE="$value" awk -v key="$key" '
    BEGIN { done=0 }
    index($0, key "=") == 1 { print key "=" ENVIRON["ENV_WRITE_VALUE"]; done=1; next }
    { print }
    END { if (!done) print key "=" ENVIRON["ENV_WRITE_VALUE"] }
  ' "$file" >"$temporary"
  chmod 600 "$temporary"
  mv "$temporary" "$file"
}

install_prerequisites() {
  info "Checking the host environment"
  if ! need git || ! need python3 || ! need flock || ! need docker; then
    need apt-get || die "Install Git, Python 3, flock, and Docker manually"
    as_root apt-get update -y
    need git || as_root apt-get install -y git ca-certificates
    need python3 || as_root apt-get install -y python3
    need flock || as_root apt-get install -y util-linux
    need docker || as_root apt-get install -y docker.io
  fi
  need docker || die "Docker is unavailable"
  if ! docker_cmd compose version >/dev/null 2>&1 && ! need docker-compose; then
    as_root apt-get install -y docker-compose-plugin 2>/dev/null \
      || as_root apt-get install -y docker-compose
  fi
  docker_cmd info >/dev/null || die "The Docker daemon is unavailable"
  ok "Host is ready; existing Docker settings were not changed"
}

prepare_repository() {
  info "Preparing the repository"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]]; then
      die "The repository has tracked local changes: $INSTALL_DIR"
    fi
    git -C "$INSTALL_DIR" fetch -q origin "$DEPLOY_REF"
    local approved_sha current_sha
    approved_sha="$(git -C "$INSTALL_DIR" rev-parse "origin/$DEPLOY_REF")"
    current_sha="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
    [[ "$current_sha" == "$approved_sha" ]] ||
      die "Installation is allowed only from CI-approved origin/$DEPLOY_REF"
  else
    as_root mkdir -p "$(dirname "$INSTALL_DIR")"
    as_root git clone --branch "$DEPLOY_REF" --single-branch "$REPOSITORY" "$INSTALL_DIR"
  fi
  [[ -f "$INSTALL_DIR/docker-compose.yml" ]] || die "Invalid project directory"
  ok "Repository is ready: $INSTALL_DIR"
}

validate_bot_token() {
  local token="$1"
  TELEGRAM_TOKEN="$token" python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

token = os.environ["TELEGRAM_TOKEN"]
try:
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/getMe", timeout=15
    ) as response:
        payload = json.load(response)
    username = payload.get("result", {}).get("username") if payload.get("ok") else None
    if not username:
        raise ValueError
    print(username)
except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError):
    print("Telegram getMe rejected the token", file=sys.stderr)
    raise SystemExit(1)
PY
}

prepare_environment() {
  info "Preparing protected configuration"
  local env_file="$INSTALL_DIR/.env" token bot_username
  if [[ ! -f "$env_file" ]]; then
    cp "$INSTALL_DIR/.env.example" "$env_file"
  fi
  chmod 600 "$env_file"
  token="$(env_value TELEGRAM_BOT_TOKEN "$env_file")"
  token="${token:-${TELEGRAM_BOT_TOKEN:-}}"
  if [[ -z "$token" ]]; then
    [[ -t 0 ]] || die "TELEGRAM_BOT_TOKEN is required for non-interactive installation"
    printf 'Telegram bot token from BotFather: ' >&2
    read -r -s token
    printf '\n' >&2
  fi
  [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || die "Invalid token format"
  bot_username="$(validate_bot_token "$token")" || die "Telegram getMe did not validate the token"
  set_env_value TELEGRAM_BOT_TOKEN "$token" "$env_file"
  chmod 600 "$env_file"
  BOT_USERNAME="$bot_username"
  ok "Telegram confirmed @$BOT_USERNAME; the token was not printed"
}

prepare_directories() {
  local first=0
  [[ -f "$INSTALL_DIR/data/settings.json" ]] || first=1
  as_root mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/codex-home" "$INSTALL_DIR/logs"
  as_root chown "$BOT_UID:$BOT_GID" "$INSTALL_DIR/data" "$INSTALL_DIR/codex-home"
  as_root chmod 700 "$INSTALL_DIR/data" "$INSTALL_DIR/codex-home" "$INSTALL_DIR/logs"
  FIRST_CONFIGURATION="$first"
}

build_image() {
  info "Building the verified image"
  local commit
  commit="$(git -C "$INSTALL_DIR" rev-parse --short=12 HEAD 2>/dev/null || printf unknown)"
  APP_COMMIT="$commit" compose build
  docker_cmd run --rm --entrypoint python codex-notify:local -m codex_notify.smoke
  ok "The image and Codex 0.154.0 passed the smoke test"
}

configure_owner() {
  [[ "$FIRST_CONFIGURATION" == "1" ]] || return 0
  local owner_id="${OWNER_ID:-}"
  if [[ -z "$owner_id" && -t 0 ]]; then
    printf 'Your numeric Telegram ID (Enter for a secure one-time link): ' >&2
    read -r owner_id
  fi
  if [[ -n "$owner_id" ]]; then
    [[ "$owner_id" =~ ^[1-9][0-9]{3,19}$ ]] || die "OWNER_ID must be a positive number"
    compose run --rm --no-deps "$SERVICE_KEY" \
      python -m codex_notify.admin set-owner "$owner_id" >/dev/null
    ok "Telegram owner configured by numeric ID"
  else
    [[ -t 1 ]] || die "Set OWNER_ID when no interactive terminal is available"
    BINDING_LINK="$(compose run --rm --no-deps "$SERVICE_KEY" \
      python -m codex_notify.admin binding-link --bot-username "$BOT_USERNAME")"
    [[ "$BINDING_LINK" == https://t.me/* ]] || die "Could not create the binding link"
  fi
}

start_bot() {
  info "Starting Codex Notify"
  compose up -d --no-build
  local attempt container status
  for attempt in {1..36}; do
    container="$(compose ps -q "$SERVICE_KEY")"
    status="$(docker_cmd inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' \
      "$container" 2>/dev/null || true)"
    [[ "$status" == "healthy" ]] && break
    sleep 5
  done
  [[ "$status" == "healthy" ]] || die "Health check failed; inspect docker compose logs"
  local commit status_file="$INSTALL_DIR/data/update-status.json"
  commit="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  printf '{"status":"installed","commit":"%s","updated_at":"%s"}\n' \
    "$commit" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$status_file.tmp"
  chmod 644 "$status_file.tmp"
  mv "$status_file.tmp" "$status_file"
  ok "The bot is running and healthy"
}

install_updater() {
  local answer="${INSTALL_AUTO_UPDATE:-}"
  if [[ -z "$answer" && -t 0 ]]; then
    printf 'Enable safe automatic updates from the CI-approved deploy branch? [Y/n]: ' >&2
    read -r answer
  fi
  case "${answer,,}" in n|no|0|false) return 0;; esac
  need systemctl || { printf 'systemd was not found; automatic updates were skipped.\n' >&2; return 0; }
  [[ -d /run/systemd/system ]] || return 0
  info "Installing the separate systemd update timer"
  local service=/etc/systemd/system/codex-notify-update.service
  local timer=/etc/systemd/system/codex-notify-update.timer
  sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
      -e "s|__COMPOSE_PROJECT__|$COMPOSE_PROJECT|g" \
      "$INSTALL_DIR/scripts/systemd/codex-notify-update.service" | as_root tee "$service" >/dev/null
  as_root cp "$INSTALL_DIR/scripts/systemd/codex-notify-update.timer" "$timer"
  as_root chmod 644 "$service" "$timer"
  as_root systemctl daemon-reload
  as_root systemctl enable --now codex-notify-update.timer
  ok "Automatic updates are enabled"
}

main() {
  install_prerequisites
  prepare_repository
  prepare_environment
  prepare_directories
  build_image
  configure_owner
  start_bot
  install_updater
  printf '\nBot: https://t.me/%s\n' "$BOT_USERNAME"
  if [[ -n "${BINDING_LINK:-}" ]]; then
    printf '\nOne-time owner link (15 minutes; show it only to the owner):\n%s\n' "$BINDING_LINK"
  fi
  printf '\nNext: open the bot, then choose Account → Connect Codex.\n'
  printf 'Logs: cd %q && docker compose -p %q logs -f --tail=200\n' "$INSTALL_DIR" "$COMPOSE_PROJECT"
  printf 'Update: sudo systemctl start codex-notify-update.service\n'
  printf 'Diagnostics: use /diagnostics in the owner private chat\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
