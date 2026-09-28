#!/usr/bin/env bash
# Checks a candidate image before it replaces the running bot: the application's
# own smoke test (models, RPC allowlist, Codex CLI version) and a real Telegram
# getMe with the token from .env, plus SMOKE_COMMAND from deploy.conf.
# APP_IMAGE_TAG selects the image.
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-production.sh
source "$SCRIPT_DIR/lib-production.sh"

resolve_app_slug
load_deploy_config

output_file="$(mktemp)"
trap 'rm -f "$output_file"' EXIT

run_in_image() {
  app_compose run --rm --no-deps -T "$SERVICE_KEY" sh -ec "$1" >>"$output_file" 2>&1
}

read -r -d '' checks <<'CHECKS' || true
python -m codex_notify.smoke
python - <<'PY'
import json
import os
import urllib.request

url = "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/getMe"
with urllib.request.urlopen(url, timeout=15) as response:
    raise SystemExit(0 if json.load(response).get("ok") else 1)
PY
CHECKS

if ! run_in_image "$checks" ||
  { [[ -n "$SMOKE_COMMAND" ]] && ! run_in_image "$SMOKE_COMMAND"; }; then
  sed -E "s/$TOKEN_REGEX/<bot-token-redacted>/g" "$output_file" >&2
  log "Candidate image smoke test failed"
  exit 1
fi
if grep -Eq "$TOKEN_REGEX" "$output_file"; then
  log "Candidate image exposed a token in smoke-test output"
  exit 1
fi
log "Candidate image passed the application smoke test and Telegram getMe"
