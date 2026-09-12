# Security

## Threat model

“Read-only” describes this bot's behavior and RPC allowlist. It does not promise that the OAuth
session itself has narrowly scoped permissions. The official Codex CLI stores and refreshes ChatGPT
credentials in `CODEX_HOME/auth.json`. Compromise of the VPS, root or Docker access, or a future
updated image can expose that authorization.

The Telegram token is stored only in `.env` with mode 0600. `settings.json`, `state.json`, backups,
and `CODEX_HOME` live in directories with mode 0700. The container runs as UID/GID 10001 without
capabilities, host networking, inbound ports, or the Docker socket. Diagnostics never include raw
RPC payloads, full email addresses, tokens, `auth.json`, or device codes.

Owner binding uses a random 192-bit token. Only its context-bound SHA-256 digest is stored. The link
expires after 15 minutes, is consumed atomically once, and can never replace an existing numeric
owner ID. Treat the unconsumed link as a temporary bearer credential and show it only to the owner.
Configuring the numeric Telegram ID directly avoids that temporary risk.

An outer middleware admits only the configured numeric owner in a private chat. The only exception
is a correctly shaped one-time binding command while no owner exists. Unauthorized messages and
group traffic are ignored before FSM handlers, Codex RPC, or other external calls. Callback queries
receive only an empty acknowledgement. Bot commands are registered only for the owner's private
chat. A stranger may still discover the public bot username, but cannot retrieve account data,
settings, history, device codes, or infer whether an owner is configured.

The privileged updater runs separately on the host and is not reachable from Telegram. It follows
the `deploy` branch, which CI advances without force only after Python, shell, and Docker jobs pass
and only while the candidate SHA remains the newest `main`. Tracked local changes block deployment.
The updater preserves the current `CODEX_HOME` and uses compatible JSON backups when a schema change
must be rolled back.

## Reporting a vulnerability

Use a private GitHub security advisory. Do not attach a Telegram token, device code, `auth.json`,
server IP address, or unredacted private logs.
