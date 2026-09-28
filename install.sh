#!/usr/bin/env bash
# One-time server setup that is safe to run again at any time. It installs
# missing prerequisites, prepares .env and the private data directories,
# configures the Telegram owner on the first run, starts the checked-out commit
# as a verified release, and enables automatic deployment for this bot only.
set -Eeuo pipefail

REPOSITORY="${REPOSITORY:-https://github.com/Avazbek22/codex-notify.git}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run outside a checkout (for example through curl): clone first, then run the
# checkout's own installer.
if [[ ! -f "$SCRIPT_DIR/scripts/lib-production.sh" ]]; then
  target="${INSTALL_DIR:-/opt/codex-notify}"
  if [[ ! -d "$target/.git" ]]; then
    command -v git >/dev/null 2>&1 || {
      printf 'Install git first: sudo apt-get install -y git\n' >&2
      exit 1
    }
    if [[ "$(id -u)" == "0" ]]; then
      git clone --branch main "$REPOSITORY" "$target"
    else
      sudo git clone --branch main "$REPOSITORY" "$target"
    fi
  fi
  exec bash "$target/install.sh" "$@"
fi

ROOT_DIR="${ROOT_DIR:-$SCRIPT_DIR}"
# shellcheck source=scripts/lib-production.sh
source "$SCRIPT_DIR/scripts/lib-production.sh"

DEFAULT_APP_SLUG="codex-notify"
BOT_UID="${BOT_UID:-10001}"
BOT_GID="${BOT_GID:-10001}"
FIRST_CONFIGURATION=0
BOT_USERNAME=""
BINDING_LINK=""

install_prerequisites() {
  local -a packages=()
  if [[ "${INSTALL_SKIP_PREREQUISITES:-0}" == "1" ]]; then
    return 0
  fi
  command_exists git || packages+=(git)
  command_exists docker || packages+=(docker.io)
  command_exists flock || packages+=(util-linux)
  command_exists curl || packages+=(curl)
  command_exists python3 || packages+=(python3)
  if ((${#packages[@]} > 0)); then
    command_exists apt-get || die "Install ${packages[*]} manually"
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates "${packages[@]}"
  fi
  if ! docker compose version >/dev/null 2>&1 && ! command_exists docker-compose; then
    apt-get install -y --no-install-recommends docker-compose-v2 ||
      apt-get install -y --no-install-recommends docker-compose-plugin ||
      apt-get install -y --no-install-recommends docker-compose
  fi
  docker info >/dev/null 2>&1 || die "The Docker daemon is not running"
  compose version >/dev/null
}

validate_repository() {
  local branch
  [[ -d "$ROOT_DIR/.git" ]] ||
    die "Clone the repository with git before running install.sh"
  run_git remote get-url origin >/dev/null 2>&1 ||
    die "Git remote 'origin' is required for automatic deployment"
  branch="$(run_git branch --show-current)"
  [[ "$branch" == "$DEPLOY_BRANCH" ]] ||
    die "Check out $DEPLOY_BRANCH before running install.sh (now on '$branch')"
  [[ -z "$(run_git status --porcelain --untracked-files=no)" ]] ||
    die "Tracked files have local changes in $ROOT_DIR; commit or restore them first"
}

# Prints the bot username when Telegram accepts the token. The token is passed
# to curl on stdin, never on the command line.
validate_bot_token() {
  local response
  response="$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$1" |
    curl --silent --show-error --fail --max-time 15 --config - 2>/dev/null)" || return 1
  python3 -c '
import json
import sys

reply = json.load(sys.stdin)
username = (reply.get("result") or {}).get("username") if reply.get("ok") else None
if not username:
    raise SystemExit(1)
print(username)
' <<<"$response"
}

prepare_environment() {
  local env_file="$ROOT_DIR/.env" token
  if [[ ! -f "$env_file" ]]; then
    install -m 600 "$ROOT_DIR/.env.example" "$env_file"
  fi
  chmod 600 "$env_file"
  token="$(env_value TELEGRAM_BOT_TOKEN "$env_file")"
  token="${token:-${TELEGRAM_BOT_TOKEN:-}}"
  if [[ -z "$token" ]]; then
    [[ -t 0 ]] || die "TELEGRAM_BOT_TOKEN is required for a non-interactive installation"
    printf 'Telegram bot token from BotFather: ' >&2
    read -r -s token
    printf '\n' >&2
  fi
  [[ "$token" =~ ^$TOKEN_REGEX$ ]] || die "Invalid token format"
  BOT_USERNAME="$(validate_bot_token "$token")" ||
    die "Telegram getMe did not validate the token"
  if [[ "$(env_value TELEGRAM_BOT_TOKEN "$env_file")" != "$token" ]]; then
    set_env_value TELEGRAM_BOT_TOKEN "$token" "$env_file"
  fi
  log "Telegram confirmed @$BOT_USERNAME; the token was not printed"

  # Pin the name so that renaming the directory never orphans the bot.
  if [[ -z "${APP_SLUG:-}" ]]; then
    APP_SLUG="$(env_value APP_SLUG "$env_file")"
  fi
  if [[ -z "$APP_SLUG" ]]; then
    APP_SLUG="$(env_value COMPOSE_PROJECT_NAME "$env_file")"
  fi
  APP_SLUG="${APP_SLUG:-$DEFAULT_APP_SLUG}"
  resolve_app_slug
  if [[ "$(env_value APP_SLUG "$env_file")" != "$APP_SLUG" ]]; then
    set_env_value APP_SLUG "$APP_SLUG" "$env_file"
  fi
  chmod 600 "$env_file"
  if [[ "$(id -u)" == "0" && -d "$ROOT_DIR/.git" ]]; then
    chown "$(stat -c '%u:%g' "$ROOT_DIR/.git")" "$env_file"
  fi
}

prepare_directories() {
  [[ -f "$ROOT_DIR/data/settings.json" ]] || FIRST_CONFIGURATION=1
  mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/codex-home" "$ROOT_DIR/logs"
  chown "$BOT_UID:$BOT_GID" "$ROOT_DIR/data" "$ROOT_DIR/codex-home"
  chmod 700 "$ROOT_DIR/data" "$ROOT_DIR/codex-home" "$ROOT_DIR/logs"
}

# The owner is configured once, before the bot first starts, with the new image.
configure_owner() {
  local owner_id="${OWNER_ID:-}"
  [[ "$FIRST_CONFIGURATION" == "1" ]] || return 0
  if [[ -z "$owner_id" && -t 0 ]]; then
    printf 'Your numeric Telegram ID (Enter for a secure one-time link): ' >&2
    read -r owner_id
  fi
  APP_COMMIT="$(run_git rev-parse --short=12 HEAD)" APP_IMAGE_TAG=candidate \
    app_compose build "$SERVICE_KEY"
  if [[ -n "$owner_id" ]]; then
    [[ "$owner_id" =~ ^[1-9][0-9]{3,19}$ ]] || die "OWNER_ID must be a positive number"
    APP_IMAGE_TAG=candidate app_compose run --rm --no-deps -T "$SERVICE_KEY" \
      python -m codex_notify.admin set-owner "$owner_id" >/dev/null
    log "Telegram owner configured by numeric ID"
  else
    [[ -t 1 ]] || die "Set OWNER_ID when no interactive terminal is available"
    BINDING_LINK="$(APP_IMAGE_TAG=candidate app_compose run --rm --no-deps -T "$SERVICE_KEY" \
      python -m codex_notify.admin binding-link --bot-username "$BOT_USERNAME")"
    [[ "$BINDING_LINK" == https://t.me/* ]] || die "Could not create the binding link"
  fi
}

automatic_updates_wanted() {
  local answer="${INSTALL_AUTO_UPDATE:-}"
  if [[ -z "$answer" && -t 0 ]]; then
    printf 'Enable safe automatic updates from %s? [Y/n]: ' "$DEPLOY_BRANCH" >&2
    read -r answer
  fi
  case "${answer,,}" in
    n | no | 0 | false) return 1 ;;
  esac
}

# Older versions deployed from a CI-promoted "deploy" branch with an hourly
# codex-notify-update timer and kept rollback state in data/.
migrate_legacy_state() {
  local tag unit legacy_failed="$ROOT_DIR/data/.failed-deploy-sha"
  if [[ -f "$legacy_failed" && ! -f "$(state_file failed-commit)" ]]; then
    tr -d '[:space:]' <"$legacy_failed" >"$(state_file failed-commit)"
  fi
  rm -f "$legacy_failed"
  for tag in rollback install-rollback pre-manual-rollback; do
    remove_image_tag "$APP_SLUG:$tag"
  done
  unit="$APP_SLUG-update"
  if [[ -f "$SYSTEMD_DIR/$unit.timer" || -f "$SYSTEMD_DIR/$unit.service" ]]; then
    "$SYSTEMCTL" disable --now "$unit.timer" >/dev/null 2>&1 || true
    rm -f "$SYSTEMD_DIR/$unit.timer" "$SYSTEMD_DIR/$unit.service"
    "$SYSTEMCTL" daemon-reload || true
    log "Removed the old $unit timer; $APP_SLUG-deploy.timer replaces it"
  fi
}

print_summary() {
  log "Installation complete: $APP_SLUG"
  printf '\nBot: https://t.me/%s\n' "$BOT_USERNAME"
  if [[ -n "$BINDING_LINK" ]]; then
    printf '\nOne-time owner link (15 minutes; show it only to the owner):\n%s\n' "$BINDING_LINK"
  fi
  cat <<SUMMARY

Next: open the bot, then choose Account -> Connect Codex.

  Status:      sudo bash scripts/status.sh
  Roll back:   sudo bash scripts/rollback.sh
  Deploy:      sudo bash scripts/deploy.sh   (optional; the timer checks every two minutes)
  Diagnostics: /diagnostics in the owner's private chat
SUMMARY
}

main() {
  install_prerequisites
  load_deploy_config
  validate_repository
  prepare_environment
  prepare_directories
  prepare_state_dir
  open_log
  acquire_lock 0 || die "Another deployment is running; try again in a minute"
  recover_interrupted_release
  adopt_running_release
  migrate_legacy_state
  configure_owner
  release_commit "$(run_git rev-parse HEAD)" install
  if automatic_updates_wanted; then
    install_units || die "Could not install the systemd timers"
  else
    log "Automatic updates were not enabled; run install.sh again to enable them"
  fi
  print_summary
}

# scripts/change-token.sh and tests/shell/test-install.sh source this file.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if [[ "$(id -u)" != "0" ]]; then
    command_exists sudo || die "Run install.sh as root"
    exec sudo --preserve-env=TELEGRAM_BOT_TOKEN,OWNER_ID,APP_SLUG,INSTALL_AUTO_UPDATE,INSTALL_SKIP_PREREQUISITES \
      bash "$SCRIPT_DIR/install.sh" "$@"
  fi
  main "$@"
fi
