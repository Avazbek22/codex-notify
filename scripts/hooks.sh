#!/usr/bin/env bash
# Codex Notify deployment hooks, run by lib-production.sh during releases.
# They keep the JSON state safe across releases and report every update to
# data/update-status.json, which /diagnostics shows. CODEX_HOME is never
# copied or restored: it may hold refresh tokens that must not go back in time.

BACKUP_ROOT="$ROOT_DIR/data/backups"

# Newest schema_version of the live JSON files; 999999 when they are unreadable,
# so that a damaged state is always replaced by the pre-update backup.
live_schema() {
  python3 - "$ROOT_DIR/data/settings.json" "$ROOT_DIR/data/state.json" <<'PY'
import json
import sys

versions = []
for path in sys.argv[1:]:
    try:
        with open(path, encoding="utf-8") as stream:
            versions.append(int(json.load(stream)["schema_version"]))
    except (OSError, ValueError, KeyError, TypeError):
        print(999999)
        raise SystemExit
print(max(versions, default=1))
PY
}

image_max_schema() {
  local schema
  schema="$(docker run --rm --entrypoint python "$1" -m codex_notify.admin max-schema 2>/dev/null)" ||
    return 1
  [[ "$schema" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$schema"
}

latest_backup() {
  find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null |
    sort -nr | head -n 1 | cut -d' ' -f2-
}

write_update_status() {
  local status="$1" commit="$2" temporary
  [[ "$commit" =~ ^[0-9a-f]{7,40}$ ]] || commit=0000000
  [[ -d "$ROOT_DIR/data" ]] || return 0
  temporary="$(mktemp "$ROOT_DIR/data/.update-status.XXXXXX")"
  printf '{"status":"%s","commit":"%s","updated_at":"%s"}\n' \
    "$status" "$commit" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$temporary"
  chmod 644 "$temporary"
  mv -f "$temporary" "$ROOT_DIR/data/update-status.json"
}

# Stops the running bot, takes a consistent copy of the JSON state, and lets the
# new image validate that copy before it starts on the live files.
hook_before_start() {
  local candidate="$1" backup
  app_compose stop -t 30 "$SERVICE_KEY"
  if [[ ! -f "$ROOT_DIR/data/settings.json" || ! -f "$ROOT_DIR/data/state.json" ]]; then
    return 0
  fi
  backup="$BACKUP_ROOT/$(date -u '+%Y%m%dT%H%M%SZ')-${RELEASE_FROM_COMMIT:0:12}"
  install -d -m 700 "$backup"
  install -m 600 "$ROOT_DIR/data/settings.json" "$backup/settings.json"
  install -m 600 "$ROOT_DIR/data/state.json" "$backup/state.json"
  sync -f "$backup/settings.json" 2>/dev/null || true
  sync -f "$backup/state.json" 2>/dev/null || true
  docker run --rm --user 0:0 -v "$backup:/app/data" --entrypoint python "$candidate" \
    -m codex_notify.admin validate-data
  find "$BACKUP_ROOT" -mindepth 2 -type f -mtime +30 -delete
  find "$BACKUP_ROOT" -mindepth 1 -type d -empty -delete
}

# Before an older image runs again, put back the pre-update JSON if the newer
# release already migrated it to a schema the older image cannot read.
hook_before_restore() {
  local image="$1" backup old_max live
  app_compose stop -t 30 "$SERVICE_KEY" >/dev/null 2>&1 || true
  [[ -f "$ROOT_DIR/data/settings.json" ]] || return 0
  # When in doubt, leave the live JSON alone rather than roll it back.
  if ! old_max="$(image_max_schema "$image")"; then
    log "WARNING: could not read the schema version of $image; the JSON was left as is"
    return 1
  fi
  live="$(live_schema)"
  ((live > old_max)) || return 0
  backup="$(latest_backup)"
  if [[ -z "$backup" || ! -f "$backup/settings.json" || ! -f "$backup/state.json" ]]; then
    log "ERROR: the JSON schema is newer than the previous release supports and no backup exists"
    return 1
  fi
  log "The JSON schema is newer than the previous release supports; restoring the pre-update JSON from ${backup#"$ROOT_DIR"/}; changes made since then are lost"
  install -m 600 "$backup/settings.json" "$ROOT_DIR/data/settings.json" || return 1
  install -m 600 "$backup/state.json" "$ROOT_DIR/data/state.json" || return 1
}

hook_after_release() {
  case "$RELEASE_KIND:$1" in
    install:*) write_update_status installed "$RELEASE_TARGET" ;;
    rebuild:*) write_update_status success "$RELEASE_TARGET" ;;
    *:unchanged) write_update_status docs_only "$RELEASE_TARGET" ;;
    *) write_update_status success "$RELEASE_TARGET" ;;
  esac
}

hook_after_failure() {
  write_update_status failed "$RELEASE_TARGET"
}

hook_after_rollback() {
  write_update_status rolled_back "$(state_get current commit)"
}
