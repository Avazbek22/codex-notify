# Установка и эксплуатация

## Первый запуск

1. Создайте Telegram-бота у BotFather и сохраните token.
2. Дайте VPS read-only deploy key к приватному репозиторию, как показано в README.
3. Запустите `sudo ./install.sh`. Существующий Docker не переустанавливается и чужие ресурсы не
   очищаются.
4. Укажите свой numeric Telegram ID либо откройте показанную только в терминале одноразовую ссылку.
5. В личном чате выберите **Аккаунт → Подключить Codex**, откройте официальный HTTPS URL и введите
   код. Код не сохраняется; после результата сообщение редактируется, но полное удаление всех копий
   Telegram не гарантируется.

Если device-code выключен, включите его в ChatGPT Security или попросите администратора workspace.
Не отправляйте в Telegram пароль, cookies, access/refresh token или `auth.json`.

## Данные

- `data/settings.json`: numeric owner, интервал, пауза, timezone, уведомления и незавершённая привязка;
- `data/state.json`: последнее наблюдение, account fingerprint, максимум 200 событий и outbox;
- `data/update-status.json`: безопасный результат последней проверки обновлений для `/diagnostics`;
- `codex-home/auth.json`: официальная авторизация Codex; обновляется самим CLI;
- `.env`: Telegram token.

При пересоздании контейнера все три host path сохраняются. Старый `auth.json` при rollback никогда не
восстанавливается автоматически.

## Обычные операции

```bash
# Логи
docker compose -p codex-notify logs -f --tail=200

# Проверить контейнер
docker compose -p codex-notify ps

# Ручная проверка CI-approved обновления
sudo systemctl start codex-notify-update.service
sudo journalctl -u codex-notify-update.service -n 100 --no-pager

# Повторить ранее неудачный SHA осознанно
sudo FORCE_DEPLOY=1 ./scripts/deploy.sh

# Безопасно сменить Telegram token
sudo ./scripts/change-token.sh
```

Повторный вход: **Аккаунт → Выйти**, подтвердите, затем **Подключить Codex**. Logout очищает сравнение
старого аккаунта и его неотправленную очередь, но не назначает нового Telegram-владельца.

## Восстановление JSON

Автообновления оставляют согласованные копии в `data/backups/`. Для выбранной абсолютной папки:

```bash
sudo ./scripts/restore-json.sh /absolute/path/to/codex-notify/data/backups/TIMESTAMP-SHA
```

Скрипт сначала валидирует оба JSON в контейнере, останавливает единственный экземпляр, сохраняет
текущие JSON отдельно и только затем заменяет их. `codex-home` не меняется. Если `.bak` подходит,
обычный startup восстанавливает corrupt primary автоматически и сохраняет corrupt-файл рядом.

## Ручной rollback

После успешного обновления предыдущий image и pre-update JSON остаются локально:

```bash
sudo ./scripts/rollback.sh
```

Rollback сохраняет актуальный `CODEX_HOME`; JSON откатывается только если его schema уже несовместима
со старым image. В таком случае лог прямо предупреждает, что изменения outbox во время неудачного
старта могли быть потеряны. Неудачный remote SHA запоминается до появления нового.

## Удаление без потери данных

```bash
sudo systemctl disable --now codex-notify-update.timer
docker compose -p codex-notify stop
docker compose -p codex-notify rm -f
```

Не удаляйте `.env`, `data/` и `codex-home/`: это оставляет возможность переустановить приложение без
повторной настройки. Для полного удаления этих секретов и авторизации сначала сделайте backup, затем
удалите каталоги вручную как отдельное осознанное действие.
