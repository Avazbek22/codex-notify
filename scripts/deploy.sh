#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEPLOY_REF="${DEPLOY_REF:-deploy}"
SERVICE_KEY="${SERVICE_KEY:-codex-notify}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-codex-notify}"
IMAGE_NAME="${IMAGE_NAME:-codex-notify:local}"
ROLLBACK_IMAGE="${IMAGE_NAME%:*}:rollback"
LOCK_FILE="${LOCK_FILE:-/run/lock/codex-notify-update.lock}"
FAILED_SHA_FILE="$ROOT_DIR/data/.failed-deploy-sha"

old_commit=""
target_commit=""
deployment_started=0
service_stopped=0
backup_dir=""

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

write_update_status() {
  local status="$1" commit="$2" temporary="$ROOT_DIR/data/.update-status.$$"
  case "$status" in success|up_to_date|docs_only|failed) ;; *) return 1;; esac
  [[ "$commit" =~ ^[0-9a-f]{7,40}$ ]] || commit=0000000
  printf '{"status":"%s","commit":"%s","updated_at":"%s"}\n' \
    "$status" "$commit" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$temporary"
  chmod 644 "$temporary"
  mv "$temporary" "$ROOT_DIR/data/update-status.json"
}

docker_cmd() {
  if docker info >/dev/null 2>&1; then docker "$@"; else sudo docker "$@"; fi
}

compose() {
  if docker_cmd compose version >/dev/null 2>&1; then
    docker_cmd compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" "$@"
  else
    docker-compose -p "$COMPOSE_PROJECT" -f "$ROOT_DIR/docker-compose.yml" "$@"
  fi
}

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

wait_until_healthy() {
  local container_id status attempt
  for attempt in {1..36}; do
    container_id="$(compose ps -q "$SERVICE_KEY")"
    if [[ -n "$container_id" ]]; then
      status="$(docker_cmd inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$container_id" 2>/dev/null || true)"
      [[ "$status" == "healthy" ]] && return 0
      [[ "$status" == "unhealthy" ]] && return 1
    fi
    sleep 5
  done
  return 1
}

restore_previous_release() {
  local exit_code=$? old_max current_schema
  [[ "$exit_code" -ne 0 ]] || exit_code=1
  trap - ERR INT TERM
  if [[ "$deployment_started" == "1" ]]; then
    log "deployment failed; restoring commit=$old_commit"
    compose stop -t 30 "$SERVICE_KEY" >/dev/null 2>&1 || true
    if docker_cmd image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
      old_max="$(docker_cmd run --rm --entrypoint python "$ROLLBACK_IMAGE" \
        -m codex_notify.admin max-schema 2>/dev/null || printf 0)"
      current_schema="$(live_schema)"
      if [[ -n "$backup_dir" && "$current_schema" -gt "$old_max" ]]; then
        log "new JSON schema is incompatible with rollback; restoring pre-update JSON; queued changes during failed startup may be lost"
        install -m 600 "$backup_dir/settings.json" "$ROOT_DIR/data/settings.json"
        install -m 600 "$backup_dir/state.json" "$ROOT_DIR/data/state.json"
      fi
      docker_cmd image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME" || true
    fi
    git -C "$ROOT_DIR" checkout -q -B "$DEPLOY_REF" "$old_commit" || true
    compose up -d --no-deps --force-recreate "$SERVICE_KEY" || true
    wait_until_healthy || log "rollback container did not become healthy"
    printf '%s\n' "$target_commit" >"$FAILED_SHA_FILE"
    chmod 600 "$FAILED_SHA_FILE"
    write_update_status failed "$target_commit" || true
  fi
  exit "$exit_code"
}

validate_checkout() {
  [[ -d "$ROOT_DIR/.git" ]] || { log "not a Git checkout: $ROOT_DIR"; return 1; }
  [[ -f "$ROOT_DIR/.env" ]] || { log "missing protected configuration: $ROOT_DIR/.env"; return 1; }
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=no)" ]]; then
    log "tracked files have local changes; refusing automatic deployment"
    return 1
  fi
}

requires_container_update() {
  local path
  while IFS= read -r path; do
    case "$path" in
      *.md | LICENSE | .github/* | docs/* | tests/* | requirements-dev.* | pyproject.toml)
        ;;
      *)
        return 0
        ;;
    esac
  done < <(git diff --name-only --diff-filter=ACDMRTUXB "$old_commit" "$target_commit")
  return 1
}

make_consistent_backup() {
  local stamp
  stamp="$(date -u '+%Y%m%dT%H%M%SZ')-$old_commit"
  backup_dir="$ROOT_DIR/data/backups/$stamp"
  install -d -m 700 "$backup_dir"
  install -m 600 "$ROOT_DIR/data/settings.json" "$backup_dir/settings.json"
  install -m 600 "$ROOT_DIR/data/state.json" "$backup_dir/state.json"
  sync -f "$backup_dir/settings.json" 2>/dev/null || true
  sync -f "$backup_dir/state.json" 2>/dev/null || true
}

main() {
  mkdir -p "$ROOT_DIR/logs" "$ROOT_DIR/data" "$(dirname "$LOCK_FILE")"
  find "$ROOT_DIR/logs" -maxdepth 1 -type f -name 'deploy-*.log' -mtime +60 -delete
  exec >>"$ROOT_DIR/logs/deploy-$(date -u '+%Y-%m-%d').log" 2>&1
  command -v flock >/dev/null 2>&1 || { log "flock is required"; return 1; }
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "another updater is running; skipping"
    return 0
  fi

  validate_checkout
  cd "$ROOT_DIR"
  git fetch -q origin "+refs/heads/$DEPLOY_REF:refs/remotes/origin/$DEPLOY_REF"
  old_commit="$(git rev-parse HEAD)"
  target_commit="$(git rev-parse "refs/remotes/origin/$DEPLOY_REF")"
  if [[ "$old_commit" == "$target_commit" ]]; then
    write_update_status up_to_date "$target_commit"
    return 0
  fi
  if [[ "${FORCE_DEPLOY:-0}" != "1" && -f "$FAILED_SHA_FILE" ]] \
      && [[ "$(tr -d '[:space:]' <"$FAILED_SHA_FILE")" == "$target_commit" ]]; then
    log "commit=$target_commit previously failed; waiting for a newer CI-approved commit"
    return 0
  fi
  git merge-base --is-ancestor "$old_commit" "$target_commit" || {
    log "deploy ref is not a fast-forward from commit=$old_commit"
    return 1
  }
  if ! requires_container_update; then
    log "documentation/test-only update; container restart skipped commit=$target_commit"
    git checkout -q -B "$DEPLOY_REF" "$target_commit"
    rm -f "$FAILED_SHA_FILE"
    write_update_status docs_only "$target_commit"
    return 0
  fi
  docker_cmd image inspect "$IMAGE_NAME" >/dev/null 2>&1 || {
    log "current image is unavailable: $IMAGE_NAME"
    return 1
  }
  docker_cmd image tag "$IMAGE_NAME" "$ROLLBACK_IMAGE"
  deployment_started=1
  trap restore_previous_release ERR INT TERM

  log "building CI-approved commit=$target_commit while the old container remains running"
  git checkout -q -B "$DEPLOY_REF" "$target_commit"
  APP_COMMIT="$target_commit" compose build --pull "$SERVICE_KEY"
  docker_cmd run --rm --entrypoint python "$IMAGE_NAME" -m codex_notify.smoke

  compose stop -t 30 "$SERVICE_KEY"
  service_stopped=1
  make_consistent_backup
  docker_cmd run --rm --user 0:0 -v "$backup_dir:/app/data" --entrypoint python "$IMAGE_NAME" \
    -m codex_notify.admin validate-data
  compose up -d --no-deps --force-recreate "$SERVICE_KEY"
  wait_until_healthy

  rm -f "$FAILED_SHA_FILE"
  find "$ROOT_DIR/data/backups" -mindepth 2 -type f -mtime +30 -delete
  find "$ROOT_DIR/data/backups" -mindepth 1 -type d -empty -delete
  deployment_started=0
  trap - ERR INT TERM
  write_update_status success "$target_commit"
  log "deployment successful commit=$target_commit; Codex authorization directory was preserved"
}

main "$@"
