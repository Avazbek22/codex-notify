# Installation and operations

## First installation

1. Create a Telegram bot with BotFather and keep its token private.
2. Clone the CI-approved `deploy` branch and run `sudo ./install.sh`. The installer does not replace
   an existing Docker installation or clean resources belonging to other projects.
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
# Follow logs
docker compose -p codex-notify logs -f --tail=200

# Inspect the container
docker compose -p codex-notify ps

# Check for a CI-approved update now
sudo systemctl start codex-notify-update.service
sudo journalctl -u codex-notify-update.service -n 100 --no-pager

# Deliberately retry a previously failed SHA
sudo FORCE_DEPLOY=1 ./scripts/deploy.sh

# Safely replace the Telegram token
sudo ./scripts/change-token.sh
```

The default timer checks hourly with up to 15 minutes of randomized delay. The manual command above
applies an already CI-approved update without waiting for the next timer run.

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

After a successful update, the previous image and pre-update JSON remain available locally:

```bash
sudo ./scripts/rollback.sh
```

Rollback preserves the current `CODEX_HOME`. JSON is restored only when its schema is too new for the
old image. The log warns that changes made after the backup can be lost in that case. The failed
remote SHA remains blocked until a newer approved commit appears or an administrator deliberately
uses `FORCE_DEPLOY=1`.

## Removal without deleting data

```bash
sudo systemctl disable --now codex-notify-update.timer
docker compose -p codex-notify stop
docker compose -p codex-notify rm -f
```

Keep `.env`, `data/`, and `codex-home/` to reinstall without reconnecting. For complete removal,
create any required backup and then delete those secret-bearing directories as a separate deliberate
operation.
