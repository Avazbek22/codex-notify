#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT_DIR"
exec 9>/run/lock/codex-notify-update.lock
flock -n 9 || { echo "Updater is already running" >&2; exit 1; }
docker image inspect codex-notify:rollback >/dev/null
latest_backup="$(find "$ROOT_DIR/data/backups" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
  | sort -nr | head -n 1 | cut -d' ' -f2-)"
[[ -n "$latest_backup" ]] || { echo "No rollback JSON backup" >&2; exit 1; }
old_commit="${latest_backup##*-}"
git cat-file -e "$old_commit^{commit}"
[[ -z "$(git status --porcelain --untracked-files=no)" ]] \
  || { echo "Tracked local changes prevent rollback" >&2; exit 1; }
remote_deploy="$(git rev-parse refs/remotes/origin/deploy 2>/dev/null || true)"

docker compose -p codex-notify -f "$ROOT_DIR/docker-compose.yml" stop -t 30 codex-notify
old_max="$(docker run --rm --entrypoint python codex-notify:rollback -m codex_notify.admin max-schema)"
live_max="$(python3 - "$ROOT_DIR/data/settings.json" "$ROOT_DIR/data/state.json" <<'PY'
import json, sys
print(max(json.load(open(path, encoding="utf-8"))["schema_version"] for path in sys.argv[1:]))
PY
)"
if [[ "$live_max" -gt "$old_max" ]]; then
  echo "Restoring pre-update JSON because the old application cannot read the live schema" >&2
  install -m 600 "$latest_backup/settings.json" "$ROOT_DIR/data/settings.json"
  install -m 600 "$latest_backup/state.json" "$ROOT_DIR/data/state.json"
fi
docker image tag codex-notify:rollback codex-notify:local
git checkout -q -B deploy "$old_commit"
docker compose -p codex-notify -f "$ROOT_DIR/docker-compose.yml" up -d --no-deps --force-recreate codex-notify
if [[ -n "$remote_deploy" ]]; then
  printf '%s\n' "$remote_deploy" >"$ROOT_DIR/data/.failed-deploy-sha"
  chmod 600 "$ROOT_DIR/data/.failed-deploy-sha"
fi
printf '{"status":"rolled_back","commit":"%s","updated_at":"%s"}\n' \
  "$old_commit" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$ROOT_DIR/data/.update-status.tmp"
chmod 644 "$ROOT_DIR/data/.update-status.tmp"
mv "$ROOT_DIR/data/.update-status.tmp" "$ROOT_DIR/data/update-status.json"
echo "Rollback started; CODEX_HOME was preserved. Check docker compose logs and /diagnostics."
