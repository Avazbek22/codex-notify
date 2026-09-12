#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=../install.sh
source "$ROOT_DIR/install.sh"
INSTALL_DIR="$ROOT_DIR"

[[ -t 0 ]] || die "Run this command in an interactive terminal"
printf 'New Telegram bot token: ' >&2
read -r -s new_token
printf '\n' >&2
[[ "$new_token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || die "Invalid token format"
bot_username="$(validate_bot_token "$new_token")" || die "Telegram getMe did not validate the token"
set_env_value TELEGRAM_BOT_TOKEN "$new_token" "$ROOT_DIR/.env"
chmod 600 "$ROOT_DIR/.env"
compose up -d --no-deps --force-recreate codex-notify
printf 'Token updated; bot: https://t.me/%s\n' "$bot_username"
