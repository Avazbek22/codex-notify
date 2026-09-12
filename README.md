# Codex Notify

Codex Notify is a self-hosted Telegram bot for one owner and one connected ChatGPT/Codex account.
It reads account and rate-limit information through the official Codex App Server. It never creates
threads or turns, invokes a model, executes Codex tasks, or consumes reset credits.

## Features

- Official ChatGPT device-code sign-in managed by the Codex CLI.
- Support for multiple rate-limit buckets and their actual window durations, usage percentages,
  remaining percentages, and reset times.
- Notifications for confirmed window resets, restored availability, newly available reset credits,
  significant ambiguous changes, and optional reset-credit expiration reminders.
- Preset polling intervals of 5, 15, 30, and 60 minutes, plus custom values from 5 to 1,440 minutes.
- Immediate manual checks, monitoring pause and resume controls, and English/Russian UI.
- Bounded event history and a persistent JSON outbox.
- Interactive installation and CI-approved automatic updates with health checks and rollback.

The application uses no database, webhook, public port, reverse proxy, browser automation, or Docker
socket inside the container.

## Requirements

- Debian or Ubuntu VPS with approximately 2 CPU cores, 2 GB RAM, and 40 GB of storage.
- Docker with Docker Compose.
- Git, Python 3, and systemd for automatic updates.
- A Telegram bot token created through BotFather.
- Network access to GitHub, Telegram, and OpenAI authentication services.

The installer does not change the firewall, SSH configuration, routing, IPv6 settings, global Docker
configuration, or resources belonging to other applications.

## Quick installation on Debian or Ubuntu

Clone the CI-approved deployment branch and run the installer:

```bash
git clone --branch deploy https://github.com/Avazbek22/codex-notify.git
cd codex-notify
sudo ./install.sh
```

For a private fork, use a dedicated read-only deploy key instead of putting a GitHub token in the
clone URL, image, or Telegram configuration.

The installer will:

1. Check the host without replacing an existing Docker installation.
2. Ask for the Telegram bot token without echoing it and validate it with Telegram `getMe`.
3. Accept the owner's numeric Telegram ID or generate a one-time binding link valid for 15 minutes.
4. Create protected persistent directories for bot data and the isolated `CODEX_HOME`.
5. Build and smoke-test the pinned container image.
6. Start the bot and wait for its health check.
7. Offer to enable the separate systemd automatic-update timer; the default is yes.

The installer prints the real Telegram bot link, log command, update command, and next step after a
successful installation.

## First Codex connection

After binding the Telegram owner:

1. Open the bot in a private chat.
2. Select **Account → Connect Codex**.
3. Open the official `https://auth.openai.com/codex/device` page shown by the bot.
4. Enter the one-time code and confirm the sign-in in your browser.

Device-code sign-in is a beta Codex capability. It may need to be enabled in ChatGPT security
settings or allowed by a workspace administrator. Never send a password, cookies, access or refresh
tokens, or `auth.json` to the Telegram bot.

After a successful sign-in, the bot masks the account email, displays the plan when available, saves
the first rate-limit observation as a baseline, and starts monitoring. Existing reset credits are
shown as the current balance and are not announced as newly granted credits.

## Bot controls

The main menu provides:

- **Status** — a compact view of remaining percentages, reset times, credits, freshness, and
  monitoring state; **Details** shows the complete technical view.
- **Check now** — starts an immediate check or joins one already in progress.
- **Settings** — language, polling interval, IANA timezone, pause/resume, and notification categories.
- **History** — paginated recent events.
- **Account** — connect, reconnect, or confirm logout from Codex.
- **Help** — concise operating and security guidance.

English is the default for new installations, with UTC as the initial timezone. Existing v0.1
installations migrate safely with Russian preserved until the owner changes it in **Settings →
Language**. Slash commands are registered only for the owner's private chat. Unauthorized users and
group messages are ignored before handlers or external calls run.

## Persistent data

- `data/settings.json` stores the owner ID and monitoring preferences.
- `data/state.json` stores the latest trusted observation, account identity, up to 200 recent events,
  event deduplication state, and the notification outbox.
- `data/update-status.json` stores a safe summary of the last host update for `/diagnostics`.
- `codex-home/auth.json` is managed and refreshed by the official Codex CLI.
- `.env` stores the Telegram bot token.

The host-mounted `data/` and `codex-home/` directories survive image and container replacement.
Updates and rollbacks never restore an older `auth.json`, because it may contain superseded refresh
credentials.

Application JSON is schema-validated and written using a same-filesystem temporary file, `fsync`, and
atomic replacement. The previous valid version is retained as a backup. If neither a primary file
nor its backup can be validated, the bot remains in a closed diagnostic state instead of reopening
owner registration.

## Operations

```bash
# Follow logs
docker compose -p codex-notify logs -f --tail=200

# Inspect container and health status
docker compose -p codex-notify ps

# Run a CI-approved update check manually
sudo systemctl start codex-notify-update.service
sudo journalctl -u codex-notify-update.service -n 100 --no-pager

# Retry a previously failed deployment SHA deliberately
sudo FORCE_DEPLOY=1 ./scripts/deploy.sh

# Replace the Telegram bot token safely
sudo ./scripts/change-token.sh

# Roll back to the previous image when available
sudo ./scripts/rollback.sh
```

See the [installation and operations guide](docs/installation.md), or its
[Russian translation](docs/ru-installation.md), for JSON recovery, manual rollback, token
replacement, reconnection, and removal without deleting persistent data.

## Automatic-update model

Only the `deploy` branch is installed. CI advances it to an exact `main` commit only after the Python
3.12 and 3.13 checks, linting, formatting, strict type checking, deployment tests, and Docker build
have succeeded. A race check prevents an older workflow from promoting over a newer commit.

The host updater:

- uses a lock to prevent concurrent deployments;
- refuses to overwrite tracked local changes;
- skips container recreation for documentation-only changes;
- builds while the existing container is still running;
- runs a non-polling smoke test and validates a consistent JSON backup;
- preserves the current Codex authorization directory;
- waits for the real application health check;
- rolls back the image and compatible JSON when startup fails; and
- remembers a failed SHA until a newer approved commit appears.

The Python bot has neither root access nor the Docker socket. Updating is handled by the separate host
systemd service. The default timer checks one hour after the previous run with up to 15 minutes of
randomized delay. Run the service manually when an approved update should be applied immediately.

## Development

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
ruff check .
ruff format --check .
mypy codex_notify
pytest
```

Runtime and development dependencies are pinned with hashes. The Docker image pins Python 3.12.11,
the base-image digest, and Codex CLI 0.154.0. It downloads the official Codex release archive and
verifies it against OpenAI's published `codex-package_SHA256SUMS` file.

The image supports `linux/amd64` and `linux/arm64`. CI builds amd64. Both official Codex Linux
artifacts are covered by the upstream checksum file, but the arm64 path still requires a live smoke
test on an arm64 host before production use.

See [architecture and verified interfaces](docs/architecture.md), [security model](SECURITY.md), and
[third-party notices](THIRD_PARTY_NOTICES.md).

## Limitations

- Changes that happen entirely between two polling runs may be missed.
- Notifications are normally delayed by the configured interval.
- Some accounts may not expose `ordinaryUsageAllowed`, reset credits, or complete credit details;
  unavailable information is shown as unknown rather than zero.
- An application crash after Telegram accepts a message but before its acknowledgement is persisted
  can cause a rare duplicate delivery. Exactly-once delivery is not claimed.
- Behavior depends on the pinned Codex version and the fields available to the connected account.
- A real device-code end-to-end test requires the account owner's browser confirmation and cannot be
  replaced by mocked CI tests.

## Security

“Read-only” describes the bot's behavior and RPC allowlist; it does not promise that the stored OAuth
session has narrowly scoped permissions. Compromise of the VPS, root or Docker access, or a future
updated image can expose the saved Codex authorization. Read [SECURITY.md](SECURITY.md) before
deployment.

## License

Original project code is licensed under the [MIT License](LICENSE). Adapted deployment mechanisms and
binary dependencies are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
