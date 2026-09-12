#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../../install.sh
source "$ROOT/install.sh"

TEST_ROOT="$(mktemp -d)"
case "$TEST_ROOT" in /tmp/*|/var/tmp/*) ;; *) exit 1;; esac
trap 'rm -rf -- "$TEST_ROOT"' EXIT

env_file="$TEST_ROOT/.env"
cp "$ROOT/.env.example" "$env_file"
chmod 600 "$env_file"
set_env_value TELEGRAM_BOT_TOKEN 'fake-secret-value-for-test' "$env_file"
set_env_value LOG_LEVEL INFO "$env_file"
set_env_value LOG_LEVEL WARNING "$env_file"
[[ "$(env_value TELEGRAM_BOT_TOKEN "$env_file")" == 'fake-secret-value-for-test' ]]
[[ "$(env_value LOG_LEVEL "$env_file")" == WARNING ]]
[[ "$(stat -c '%a' "$env_file")" == 600 ]]
[[ "$(grep -c '^LOG_LEVEL=' "$env_file")" == 1 ]]

# A rerun with canonical data must skip owner initialization rather than overwrite it.
FIRST_CONFIGURATION=0
OWNER_ID=9999
configure_owner
