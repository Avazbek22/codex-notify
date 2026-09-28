#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck disable=SC1091 # install.sh is sourced for its helper functions
source "$ROOT_DIR/install.sh"

require_root
[[ -t 0 ]] || die "Run this command in an interactive terminal"
resolve_app_slug
printf 'New Telegram bot token: ' >&2
read -r -s new_token
printf '\n' >&2
[[ "$new_token" =~ ^$TOKEN_REGEX$ ]] || die "Invalid token format"
bot_username="$(validate_bot_token "$new_token")" || die "Telegram getMe did not validate the token"
set_env_value TELEGRAM_BOT_TOKEN "$new_token" "$ROOT_DIR/.env"
chmod 600 "$ROOT_DIR/.env"
app_compose up -d --no-deps --force-recreate "$SERVICE_KEY"
printf 'Token updated; bot: https://t.me/%s\n' "$bot_username"
