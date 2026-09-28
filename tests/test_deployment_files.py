from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_application_version_is_consistent() -> None:
    package = (ROOT / "codex_notify" / "__init__.py").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '__version__ = "0.2.0"' in package
    assert 'version = "0.2.0"' in project
    assert "ARG APP_VERSION=0.2.0" in dockerfile


def test_compose_is_single_unprivileged_service_without_incoming_ports() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.startswith("name: ${APP_SLUG:-codex-notify}\n")
    assert "image: ${APP_SLUG:-codex-notify}:${APP_IMAGE_TAG:-local}" in compose
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


def test_ci_is_read_only_and_gates_deployment() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    deploy_conf = (ROOT / "deploy.conf").read_text(encoding="utf-8")
    assert "contents: write" not in workflow
    assert "--force" not in workflow
    assert "pull_request_target" not in workflow
    assert "uses: actions/checkout@v" not in workflow
    assert "uses: actions/setup-python@v" not in workflow
    assert "uses: docker/setup-buildx-action@v" not in workflow
    assert "uses: docker/build-push-action@v" not in workflow
    assert "DEPLOY_BRANCH=main" in deploy_conf
    assert "REQUIRE_CI=auto" in deploy_conf


def test_updater_preserves_auth_and_has_schema_aware_rollback() -> None:
    names = ("deploy.sh", "lib-production.sh", "hooks.sh", "rollback.sh")
    deploy = "\n".join((ROOT / "scripts" / name).read_text(encoding="utf-8") for name in names)
    hooks = (ROOT / "scripts/hooks.sh").read_text(encoding="utf-8")
    assert "failed-commit" in deploy
    assert "wait_until_stable" in deploy
    assert "hook_before_start" in hooks and "validate-data" in hooks
    assert "hook_before_restore" in hooks and "max-schema" in hooks
    assert "write_update_status" in hooks
    assert "codex-home" not in deploy
    assert "docker system prune" not in deploy
    assert "down -v" not in deploy


def test_installer_validates_getme_and_keeps_secrets_out_of_arguments() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "getMe" in installer
    assert "read -r -s token" in installer
    assert "--config -" in installer
    assert '-v value="$value"' not in installer
    assert "claim_" not in installer
    assert "docker system prune" not in installer
    assert "firewall" not in installer.lower()


def test_installer_scripts_and_primary_docs_are_english() -> None:
    cyrillic = re.compile(r"[А-Яа-яЁё]")
    paths = [
        ROOT / "install.sh",
        *sorted((ROOT / "scripts").glob("*.sh")),
        ROOT / "README.md",
        ROOT / "SECURITY.md",
        ROOT / "docs" / "architecture.md",
        ROOT / "docs" / "installation.md",
    ]
    assert all(not cyrillic.search(path.read_text(encoding="utf-8")) for path in paths)
