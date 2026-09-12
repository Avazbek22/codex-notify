#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
case "$TEST_ROOT" in /tmp/*|/var/tmp/*) ;; *) exit 1;; esac
trap 'rm -rf -- "$TEST_ROOT"' EXIT

make_fake_commands() {
  local fake_bin="$1"
  mkdir -p "$fake_bin"
  cat >"$fake_bin/git" <<'FAKE_GIT'
#!/usr/bin/env bash
set -euo pipefail
printf 'git %s\n' "$*" >>"$FAKE_COMMAND_LOG"
case "$*" in
  *"status --porcelain"*) exit 0;;
  *"rev-parse HEAD"*) cat "$FAKE_STATE_DIR/head";;
  *"rev-parse refs/remotes/origin/deploy"*) cat "$FAKE_STATE_DIR/target";;
  *"merge-base --is-ancestor"*) exit 0;;
  *"diff --name-only --diff-filter=ACDMRTUXB"*) cat "$FAKE_STATE_DIR/changes";;
  *"checkout -q -B"*) printf '%s\n' "${!#}" >"$FAKE_STATE_DIR/head";;
esac
FAKE_GIT

  cat >"$fake_bin/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >>"$FAKE_COMMAND_LOG"
case "$*" in
  "info"|"compose version") exit 0;;
  "image inspect "*|"image tag "*) exit 0;;
  "run --rm --entrypoint python "*"max-schema"*) echo 1; exit 0;;
  "run "*) exit 0;;
  "compose "*" build "*) [[ "${FAKE_FAIL_BUILD:-0}" != 1 ]];;
  "compose "*" ps -q "*) echo fake-container;;
  "compose "*" up "*)
    if [[ "${FAKE_INCOMPATIBLE:-0}" == 1 && ! -f "$FAKE_STATE_DIR/new-started" ]]; then
      touch "$FAKE_STATE_DIR/new-started"
      printf '{"schema_version":2}\n' >"$FAKE_STATE_DIR/data/state.json"
    fi
    ;;
  "inspect "*)
    if [[ "${FAKE_INCOMPATIBLE:-0}" == 1 && -f "$FAKE_STATE_DIR/new-started" && ! -f "$FAKE_STATE_DIR/rollback-tagged" ]]; then
      echo unhealthy
    else
      echo healthy
    fi
    ;;
esac
if [[ "$*" == "image tag "*"rollback"*"local" ]]; then touch "$FAKE_STATE_DIR/rollback-tagged"; fi
FAKE_DOCKER

  cat >"$fake_bin/sleep" <<'FAKE_SLEEP'
#!/usr/bin/env bash
exit 0
FAKE_SLEEP
  chmod 755 "$fake_bin/git" "$fake_bin/docker" "$fake_bin/sleep"
}

prepare_case() {
  local root="$1"
  mkdir -p "$root/.git" "$root/data" "$root/logs" "$root/bin" "$root/lock"
  : >"$root/.env"
  : >"$root/docker-compose.yml"
  printf old-commit >"$root/head"
  printf new-commit >"$root/target"
  printf 'codex_notify/main.py\n' >"$root/changes"
  printf '{"schema_version":1,"owner_id":1234}\n' >"$root/data/settings.json"
  printf '{"schema_version":1,"outbox":[]}\n' >"$root/data/state.json"
  : >"$root/commands.log"
  make_fake_commands "$root/bin"
}

run_deploy() {
  local root="$1"
  shift
  env PATH="$root/bin:$PATH" ROOT_DIR="$root" LOCK_FILE="$root/lock/update.lock" \
    FAKE_STATE_DIR="$root" FAKE_COMMAND_LOG="$root/commands.log" "$@" \
    bash "$REPOSITORY_ROOT/scripts/deploy.sh"
}

success="$TEST_ROOT/success"
prepare_case "$success"
run_deploy "$success"
[[ "$(<"$success/head")" == new-commit ]]
grep -q 'building CI-approved commit=new-commit while the old container remains running' "$success/logs/deploy-"*.log
grep -q 'docker compose .* build --pull codex-notify' "$success/commands.log"
grep -q 'docker compose .* stop -t 30 codex-notify' "$success/commands.log"
grep -q 'docker compose .* up -d --no-deps --force-recreate codex-notify' "$success/commands.log"

docs="$TEST_ROOT/docs"
prepare_case "$docs"
printf 'README.md\ndocs/architecture.md\ntests/test_domain.py\n' >"$docs/changes"
run_deploy "$docs"
grep -q 'documentation/test-only update' "$docs/logs/deploy-"*.log
if grep -q ' build \| up ' "$docs/commands.log"; then
  echo 'documentation-only update touched the container' >&2
  exit 1
fi

failed="$TEST_ROOT/failed"
prepare_case "$failed"
if run_deploy "$failed" FAKE_FAIL_BUILD=1; then
  echo 'expected build failure' >&2
  exit 1
fi
[[ "$(<"$failed/head")" == old-commit ]]
[[ "$(<"$failed/data/.failed-deploy-sha")" == new-commit ]]
grep -q 'docker image tag codex-notify:rollback codex-notify:local' "$failed/commands.log"

incompatible="$TEST_ROOT/incompatible"
prepare_case "$incompatible"
if run_deploy "$incompatible" FAKE_INCOMPATIBLE=1; then
  echo 'expected unhealthy new schema deployment' >&2
  exit 1
fi
grep -q 'new JSON schema is incompatible with rollback' "$incompatible/logs/deploy-"*.log
grep -q '"owner_id":1234' "$incompatible/data/settings.json"
grep -q '"schema_version":1' "$incompatible/data/state.json"
