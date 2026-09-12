# Architecture and verified interfaces

The interfaces were last verified on September 12, 2026. The image pins stable **Codex 0.154.0**
(`rust-v0.154.0`, released September 9, 2026). Docker downloads the official amd64 or arm64 release
archive and verifies it against OpenAI's published `codex-package_SHA256SUMS`.

Primary references: [Codex 0.154.0 release](https://github.com/openai/codex/releases/tag/rust-v0.154.0),
[official App Server documentation](https://learn.chatgpt.com/docs/app-server),
[official authentication documentation](https://learn.chatgpt.com/docs/auth),
[rate-limit schema at the pinned tag](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server-protocol/schema/json/v2/GetAccountRateLimitsResponse.json),
and [login schema at the pinned tag](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server-protocol/schema/json/v2/LoginAccountResponse.json).
The Telegram integration targets [aiogram 3.31.0](https://docs.aiogram.dev/en/latest/) and the
[Telegram Bot API](https://core.telegram.org/bots/api). Owner binding follows Telegram's official
[deep-linking format](https://core.telegram.org/bots/features#deep-linking).

## Codex process and protocol

The application starts `codex app-server --stdio --strict-config` as a child process with an isolated
`CODEX_HOME`. The transport is one JSON object per line over stdin/stdout, without `Content-Length`
framing. The client sends `initialize`, waits for the response with the correlated ID, and then sends
the `initialized` notification. `experimentalApi` is not required for the methods used here.

The adapter allowlist contains only:

- `account/read` with `refreshToken: false`;
- `account/login/start` with `type: chatgptDeviceCode`;
- `account/login/cancel`;
- `account/logout`;
- `account/rateLimits/read`.

It cannot call `thread/start`, `turn/start`, command execution,
`account/rateLimitResetCredit/consume`, or arbitrary RPC methods. An ordinary OpenAI API key is not
used as a substitute for ChatGPT account sign-in.

Codex 0.154.0 exposes the legacy singleton `rateLimits`, multi-bucket `rateLimitsByLimitId`, nullable
`ordinaryUsageAllowed`, and `rateLimitResetCredits`. Multi-bucket data takes precedence so a legacy
copy is not counted twice. `availableCount` is authoritative; `credits=null` means the count is known
while details are unavailable. The backend supplies `windowDurationMins`, so the monitor does not
assume fixed five-hour or weekly windows.

App Server notifications are treated as hints, not a guaranteed subscription to account changes on
other devices. Scheduled reads remain the observation source. The first snapshot, account changes,
and the return of previously missing credit details establish a baseline without announcing a new
grant. A reset-like change before the expected time receives one confirmation read and remains an
ambiguous “state changed” event.

## Application boundaries

The Python process is separated by responsibility into the Telegram UI, owner-access middleware,
App Server adapter, monitor and scheduler, network-independent change analysis, JSON repository,
presentation/localization, and outbox delivery. There is one application container and one Codex
child process. Telegram uses long polling and exposes no inbound port.

`settings.json` and `state.json` are Pydantic-validated and written with a same-filesystem temporary
file, `fsync`, and `os.replace`; the previous valid file remains as `.bak`. A new observation, its
events, deduplication state, and outbox are committed in one `state.json` write. A corrupt primary is
preserved and restored only from a validated backup. Otherwise the application enters closed
diagnostic mode. A process lock prevents two pollers or App Server processes from sharing one data
directory.

Schema v2 stores stable event codes and typed presentation parameters instead of rendered language.
Queued v2 notifications are rendered in the owner's selected language at delivery time. A v1
installation migrates atomically, retaining Russian as its selected language, its timezone, owner,
history, outbox, and authorization. Legacy rendered history and queued messages are preserved rather
than parsed or discarded. New installations default to English and UTC.

The Docker health check requires a fresh heartbeat and live background tasks. It deliberately does
not require OpenAI availability or a connected account.
