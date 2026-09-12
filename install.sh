#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPOSITORY="git@github.com:Avazbek22/codex-notify.git"
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
  if [[ "$(id -u)" == "0" ]]; then "$@"; else need sudo || die "Требуется sudo"; sudo "$@"; fi
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
    die "Docker Compose недоступен"
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
  info "Проверка окружения"
  if ! need git || ! need python3 || ! need flock || ! need docker; then
    need apt-get || die "Установите Git, Python 3, flock и Docker вручную"
    as_root apt-get update -y
    need git || as_root apt-get install -y git ca-certificates
    need python3 || as_root apt-get install -y python3
    need flock || as_root apt-get install -y util-linux
    need docker || as_root apt-get install -y docker.io
  fi
  need docker || die "Docker недоступен"
  if ! docker_cmd compose version >/dev/null 2>&1 && ! need docker-compose; then
    as_root apt-get install -y docker-compose-plugin 2>/dev/null \
      || as_root apt-get install -y docker-compose
  fi
  docker_cmd info >/dev/null || die "Docker daemon недоступен"
  ok "Окружение готово; существующие Docker-настройки не изменялись"
}

prepare_repository() {
  info "Подготовка репозитория"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]]; then
      die "В репозитории есть локальные изменения: $INSTALL_DIR"
    fi
    git -C "$INSTALL_DIR" fetch -q origin "$DEPLOY_REF"
    local approved_sha current_sha
    approved_sha="$(git -C "$INSTALL_DIR" rev-parse "origin/$DEPLOY_REF")"
    current_sha="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
    [[ "$current_sha" == "$approved_sha" ]] ||
      die "Установка разрешена только из CI-проверенного origin/$DEPLOY_REF"
  else
    as_root mkdir -p "$(dirname "$INSTALL_DIR")"
    as_root git clone --branch "$DEPLOY_REF" --single-branch "$REPOSITORY" "$INSTALL_DIR"
  fi
  [[ -f "$INSTALL_DIR/docker-compose.yml" ]] || die "Некорректный каталог проекта"
  ok "Репозиторий готов: $INSTALL_DIR"
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
    print("Токен отклонён Telegram getMe", file=sys.stderr)
    raise SystemExit(1)
PY
}

prepare_environment() {
  info "Защищённая конфигурация"
  local env_file="$INSTALL_DIR/.env" token bot_username
  if [[ ! -f "$env_file" ]]; then
    cp "$INSTALL_DIR/.env.example" "$env_file"
  fi
  chmod 600 "$env_file"
  token="$(env_value TELEGRAM_BOT_TOKEN "$env_file")"
  token="${token:-${TELEGRAM_BOT_TOKEN:-}}"
  if [[ -z "$token" ]]; then
    [[ -t 0 ]] || die "TELEGRAM_BOT_TOKEN нужен для неинтерактивной установки"
    printf 'Токен Telegram-бота от BotFather: ' >&2
    read -r -s token
    printf '\n' >&2
  fi
  [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || die "Неверный формат токена"
  bot_username="$(validate_bot_token "$token")" || die "Telegram getMe не подтвердил токен"
  set_env_value TELEGRAM_BOT_TOKEN "$token" "$env_file"
  chmod 600 "$env_file"
  BOT_USERNAME="$bot_username"
  ok "Telegram подтвердил @$BOT_USERNAME; токен не выведен"
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
  info "Сборка проверяемого образа"
  local commit
  commit="$(git -C "$INSTALL_DIR" rev-parse --short=12 HEAD 2>/dev/null || printf unknown)"
  APP_COMMIT="$commit" compose build
  docker_cmd run --rm --entrypoint python codex-notify:local -m codex_notify.smoke
  ok "Образ и Codex 0.154.0 прошли smoke test"
}

configure_owner() {
  [[ "$FIRST_CONFIGURATION" == "1" ]] || return 0
  local owner_id="${OWNER_ID:-}"
  if [[ -z "$owner_id" && -t 0 ]]; then
    printf 'Ваш числовой Telegram ID (Enter — безопасная одноразовая ссылка): ' >&2
    read -r owner_id
  fi
  if [[ -n "$owner_id" ]]; then
    [[ "$owner_id" =~ ^[1-9][0-9]{3,19}$ ]] || die "OWNER_ID должен быть положительным числом"
    compose run --rm --no-deps "$SERVICE_KEY" \
      python -m codex_notify.admin set-owner "$owner_id" >/dev/null
    ok "Владелец Telegram настроен по numeric ID"
  else
    [[ -t 1 ]] || die "Без интерактивного терминала укажите OWNER_ID"
    BINDING_LINK="$(compose run --rm --no-deps "$SERVICE_KEY" \
      python -m codex_notify.admin binding-link --bot-username "$BOT_USERNAME")"
    [[ "$BINDING_LINK" == https://t.me/* ]] || die "Не удалось создать ссылку привязки"
  fi
}

start_bot() {
  info "Запуск Codex Notify"
  compose up -d --no-build
  local attempt container status
  for attempt in {1..36}; do
    container="$(compose ps -q "$SERVICE_KEY")"
    status="$(docker_cmd inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' \
      "$container" 2>/dev/null || true)"
    [[ "$status" == "healthy" ]] && break
    sleep 5
  done
  [[ "$status" == "healthy" ]] || die "Healthcheck не прошёл; смотрите docker compose logs"
  local commit status_file="$INSTALL_DIR/data/update-status.json"
  commit="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  printf '{"status":"installed","commit":"%s","updated_at":"%s"}\n' \
    "$commit" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$status_file.tmp"
  chmod 644 "$status_file.tmp"
  mv "$status_file.tmp" "$status_file"
  ok "Бот запущен и healthcheck успешен"
}

install_updater() {
  local answer="${INSTALL_AUTO_UPDATE:-}"
  if [[ -z "$answer" && -t 0 ]]; then
    printf 'Включить безопасные автообновления из CI-approved deploy? [Y/n]: ' >&2
    read -r answer
  fi
  case "${answer,,}" in n|no|0|false) return 0;; esac
  need systemctl || { printf 'systemd не найден; автообновления пропущены.\n' >&2; return 0; }
  [[ -d /run/systemd/system ]] || return 0
  info "Установка отдельного systemd-таймера обновлений"
  local service=/etc/systemd/system/codex-notify-update.service
  local timer=/etc/systemd/system/codex-notify-update.timer
  sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
      -e "s|__COMPOSE_PROJECT__|$COMPOSE_PROJECT|g" \
      "$INSTALL_DIR/scripts/systemd/codex-notify-update.service" | as_root tee "$service" >/dev/null
  as_root cp "$INSTALL_DIR/scripts/systemd/codex-notify-update.timer" "$timer"
  as_root chmod 644 "$service" "$timer"
  as_root systemctl daemon-reload
  as_root systemctl enable --now codex-notify-update.timer
  ok "Автообновления включены"
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
  printf '\nБот: https://t.me/%s\n' "$BOT_USERNAME"
  if [[ -n "${BINDING_LINK:-}" ]]; then
    printf '\nОдноразовая ссылка (15 минут, покажите только владельцу):\n%s\n' "$BINDING_LINK"
  fi
  printf '\nСледующий шаг: откройте бота, затем «Аккаунт» → «Подключить Codex».\n'
  printf 'Логи: cd %q && docker compose -p %q logs -f --tail=200\n' "$INSTALL_DIR" "$COMPOSE_PROJECT"
  printf 'Обновить: sudo systemctl start codex-notify-update.service\n'
  printf 'Диагностика: команда /diagnostics в личном чате владельца\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
