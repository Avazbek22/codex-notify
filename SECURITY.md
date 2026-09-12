# Security

## Модель угроз

«Только чтение» означает ограничение поведения этого бота и его RPC allowlist, а не обещание узких
прав OAuth-сессии. Официальный Codex CLI сохраняет ChatGPT-токены в `CODEX_HOME/auth.json` и сам их
обновляет. Компрометация VPS, root/Docker-доступа или будущего обновлённого кода опасна для этой
сохранённой авторизации.

Telegram token хранится только в `.env` с режимом 0600. `settings.json`, `state.json`, backups и
`CODEX_HOME` находятся в каталогах 0700; контейнер работает UID/GID 10001, без capabilities,
host network, входящих портов и Docker socket. Диагностика не выводит raw RPC, полные email, токены,
auth.json или device code.

Привязка владельца использует случайный 192-битный token, в JSON хранится только контекстный SHA-256.
Ссылка живёт 15 минут, принимается атомарно один раз и никогда не заменяет уже установленный numeric
owner ID. Проверки private chat и owner ID применяются и к сообщениям, и к callback query.

Автообновлятор привилегирован только на host и не доступен из Telegram. Он берёт branch `deploy`,
которую CI продвигает без force только после Python, shell и Docker jobs и только если SHA всё ещё
является newest `main`. Локальные tracked changes блокируют обновление.

## Сообщить об уязвимости

Используйте private security advisory GitHub. Не прикладывайте Telegram token, device code,
`auth.json`, server IP или необработанные логи с приватными данными.
