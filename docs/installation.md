# Installation and operations

## First installation

1. Create a Telegram bot with BotFather and keep its token private.
2. Clone the repository and run `sudo bash install.sh`. The installer does not replace an existing
   Docker installation or clean resources belonging to other projects.
3. Enter your numeric Telegram ID, or open the one-time binding link printed only in the interactive
   terminal. The link is valid for 15 minutes and should not be shared.
4. In the owner private chat, choose **Account → Connect Codex**, open the official HTTPS page, and
   enter the displayed code. The code is never persisted. The message is edited after completion,
   but complete deletion of every Telegram copy cannot be guaranteed.

If device-code sign-in is disabled, enable it in ChatGPT security settings or ask the workspace
administrator. Never send a password, cookies, access or refresh tokens, or `auth.json` through
Telegram.

New installations start in English with the UTC timezone. Change both from **Settings**. An upgrade
from v0.1 retains Russian and the existing timezone until the owner changes them.

## Persistent data

- `data/settings.json`: numeric owner, language, interval, pause state, timezone, notifications, and
  pending owner binding;
- `data/state.json`: latest trusted observation, account fingerprint, at most 200 events, and outbox;
- `data/update-status.json`: safe update result shown by `/diagnostics`;
- `codex-home/auth.json`: official Codex authorization managed by the CLI;
- `.env`: Telegram token.

Container recreation preserves the host-mounted `.env`, `data/`, and `codex-home/`. Rollback never
restores an older `auth.json` automatically.

## Routine operations

```bash
# Running release, previous release, and what automatic deployment waits for
sudo bash scripts/status.sh

# Follow logs
docker compose logs -f --tail=200

# Deploy the newest CI-approved commit now
sudo bash scripts/deploy.sh

# Deliberately retry a previously failed commit
sudo bash scripts/deploy.sh --retry

# Safely replace the Telegram token
sudo bash scripts/change-token.sh
```

`codex-notify-deploy.timer` checks `main` every two minutes and deploys a commit once its GitHub checks
have passed; the manual command above only skips the wait for the next timer run. Every result is
recorded in `data/update-status.json` and shown by `/diagnostics`. Settings such as the watch window
live in `deploy.conf`.

To reconnect, confirm **Account → Sign out of Codex**, then choose **Connect Codex**. Logout clears
comparisons and queued notifications belonging to the previous Codex account. It does not change the
Telegram owner.

## JSON recovery

Automatic updates keep consistent copies under `data/backups/`. Restore a selected absolute path:

```bash
sudo ./scripts/restore-json.sh /absolute/path/to/codex-notify/data/backups/TIMESTAMP-SHA
```

The script validates both files in the application image, stops the single instance, preserves the
current JSON, and only then replaces it. `codex-home` is not changed. During ordinary startup, a
valid `.bak` automatically restores a corrupt primary while the corrupt file is preserved nearby.

## Manual rollback

After a successful update, the previous release (its commit and exact image) and the pre-update JSON
remain available locally:

```bash
sudo bash scripts/rollback.sh
```

Rollback preserves the current `CODEX_HOME`. JSON is restored only when its schema is too new for the
old image. The log warns that changes made after the backup can be lost in that case. Running the
command again returns to the release that was replaced. After a rollback the timer leaves the bot
alone until the next push; `sudo bash scripts/deploy.sh --retry` deploys the newest commit again.

A release that turns unhealthy within ten minutes after it started is rolled back the same way
automatically.

## Removal without deleting data

```bash
sudo systemctl disable --now codex-notify-deploy.timer codex-notify-rebuild.timer
docker compose -p codex-notify stop
docker compose -p codex-notify rm -f
```

Keep `.env`, `data/`, and `codex-home/` to reinstall without reconnecting. For complete removal,
create any required backup and then delete those secret-bearing directories as a separate deliberate
operation.
