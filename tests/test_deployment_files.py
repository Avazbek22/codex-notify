from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compose_is_single_unprivileged_service_without_incoming_ports() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.startswith("name: ${COMPOSE_PROJECT_NAME:-codex-notify}\n")
    assert compose.count("  codex-notify:") == 1
    assert "ports:" not in compose
    assert "network_mode: host" not in compose
    assert "/var/run/docker.sock" not in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "./data:/app/data" in compose
    assert "./codex-home:/app/codex-home" in compose
    assert "max-size: 10m" in compose


def test_dockerfile_pins_codex_and_verifies_official_checksum() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "CODEX_VERSION=0.154.0" in dockerfile
    assert "python:3.12.11-slim-bookworm@sha256:" in dockerfile
    assert "codex-package_SHA256SUMS" in dockerfile
    assert "sha256sum -c" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "cargo build" not in dockerfile.lower()


def test_ci_promotes_only_successful_newest_main_without_force() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "needs: [python, container]" in workflow
    assert "contents: write" in workflow
    assert 'test "$CANDIDATE" = "$(git rev-parse refs/remotes/origin/main)"' in workflow
    assert "merge-base --is-ancestor" in workflow
    assert "--force" not in workflow
    assert "pull_request_target" not in workflow


def test_updater_preserves_auth_and_has_schema_aware_rollback() -> None:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    assert 'DEPLOY_REF="${DEPLOY_REF:-deploy}"' in deploy
    assert "previously failed" in deploy
    assert "documentation/test-only update" in deploy
    assert "make_consistent_backup" in deploy
    assert "current_schema" in deploy and "old_max" in deploy
    assert "codex-home" not in deploy
    assert "docker system prune" not in deploy
    assert "down -v" not in deploy
    assert "wait_until_healthy" in deploy
    assert "write_update_status" in deploy


def test_installer_validates_getme_and_keeps_secrets_out_of_arguments() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "getMe" in installer
    assert "read -r -s token" in installer
    assert 'TELEGRAM_TOKEN="$token" python3' in installer
    assert '-v value="$value"' not in installer
    assert "claim_" not in installer
    assert "docker system prune" not in installer
    assert "firewall" not in installer.lower()
