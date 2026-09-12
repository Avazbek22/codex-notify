# Архитектура и проверенные интерфейсы

Проверено 12 сентября 2026 года. Закреплён стабильный релиз **Codex 0.154.0**
(`rust-v0.154.0`, опубликован 9 сентября 2026). Docker загружает официальный release archive для
amd64/arm64 и сверяет выбранный файл по опубликованному OpenAI `codex-package_SHA256SUMS`.

Источники: [release 0.154.0](https://github.com/openai/codex/releases/tag/rust-v0.154.0),
[официальный App Server](https://learn.chatgpt.com/docs/app-server),
[официальная авторизация](https://learn.chatgpt.com/docs/auth),
[схема rate limits в теге 0.154.0](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server-protocol/schema/json/v2/GetAccountRateLimitsResponse.json),
[схема login в теге 0.154.0](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server-protocol/schema/json/v2/LoginAccountResponse.json).
Telegram-часть сверена с [aiogram 3.31.0](https://docs.aiogram.dev/en/latest/) и
[Bot API](https://core.telegram.org/bots/api); owner-link следует официальному
[deep linking](https://core.telegram.org/bots/features#deep-linking) (до 64 URL-safe символов).

App Server запускается как дочерний `codex app-server --stdio --strict-config` с отдельным
`CODEX_HOME`. Транспорт — один JSON-объект на строку в stdin/stdout, без Content-Length framing.
Клиент сначала посылает `initialize`, ждёт коррелированный `id`, затем отправляет notification
`initialized`. Включение `experimentalApi` не требуется.

Разрешены только:

- `account/read` (`refreshToken: false`);
- `account/login/start` только с `type: chatgptDeviceCode`;
- `account/login/cancel`;
- `account/logout`;
- `account/rateLimits/read`.

Код не может вызвать `thread/start`, `turn/start`, command execution,
`account/rateLimitResetCredit/consume` или произвольный RPC. Обычный API key не используется.

В 0.154.0 подтверждены `rateLimits` (legacy singleton), `rateLimitsByLimitId` (multi-bucket),
`ordinaryUsageAllowed: boolean|null` и `rateLimitResetCredits`. Multi-bucket имеет приоритет, чтобы
не учитывать legacy-копию дважды. `availableCount` authoritative; `credits=null` означает только
известный счётчик, а массив деталей может быть ограничен backend. Длительность окна приходит в
`windowDurationMins`, поэтому «5 часов/неделя» не зашиты.

Уведомления App Server считаются подсказками, не гарантированной подпиской на внешние изменения.
Плановые чтения остаются источником наблюдений. Первый snapshot, смена аккаунта и восстановление
после отсутствующих credit details создают baseline без «подарка». Ранний похожий на reset скачок
подтверждается одним повторным чтением и остаётся формулировкой «состояние изменилось».

`settings.json` и `state.json` валидируются Pydantic, пишутся temp→fsync→`os.replace`, а предыдущий
валидный файл сохраняется как `.bak`. Snapshot, события и outbox фиксируются одной записью
`state.json`. Corrupt primary переименовывается, затем восстанавливается только валидный backup;
иначе приложение закрывается диагностической ошибкой. Отдельный process lock запрещает два polling
и два App Server на одном data directory.

Python-процесс разделён на Telegram UI, App Server adapter, monitor/scheduler, чистый анализ,
JSON repository и outbox delivery. Docker healthcheck требует свежего heartbeat и живых фоновых
задач, но не требует доступности OpenAI или уже подключённого аккаунта.
