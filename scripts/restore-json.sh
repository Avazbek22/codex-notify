#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${1:-}"
[[ -n "$BACKUP_DIR" ]] || { echo "Usage: $0 /absolute/path/to/backup" >&2; exit 2; }
BACKUP_DIR="$(realpath "$BACKUP_DIR")"
ROOT_DIR="$(realpath "$ROOT_DIR")"
[[ "$BACKUP_DIR" != "$ROOT_DIR" && -f "$BACKUP_DIR/settings.json" && -f "$BACKUP_DIR/state.json" ]] \
  || { echo "Backup must contain settings.json and state.json" >&2; exit 2; }

docker run --rm -v "$BACKUP_DIR:/restore:ro" --entrypoint python codex-notify:local \
  -m codex_notify.validate_data --data-dir /restore

compose=(docker compose -p codex-notify -f "$ROOT_DIR/docker-compose.yml")
"${compose[@]}" stop -t 30 codex-notify
recovery="$ROOT_DIR/data/backups/manual-before-restore-$(date -u '+%Y%m%dT%H%M%SZ')"
install -d -m 700 "$recovery"
install -m 600 "$ROOT_DIR/data/settings.json" "$recovery/settings.json"
install -m 600 "$ROOT_DIR/data/state.json" "$recovery/state.json"
install -m 600 "$BACKUP_DIR/settings.json" "$ROOT_DIR/data/settings.json"
install -m 600 "$BACKUP_DIR/state.json" "$ROOT_DIR/data/state.json"
"${compose[@]}" up -d --no-deps --force-recreate codex-notify
printf 'JSON restored. Codex auth directory was not changed. Previous JSON: %s\n' "$recovery"
