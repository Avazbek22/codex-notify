# Codex Notify

Codex Notify — self-hosted Telegram-бот для одного владельца и одного ChatGPT/Codex-аккаунта.
Он читает состояние аккаунта и лимитов через официальный Codex App Server, не создаёт threads/turns,
не обращается к модели и не расходует reset-кредиты.

## Что умеет

- официальный device-code вход ChatGPT, которым управляет Codex CLI;
- несколько фактических окон лимитов, проценты использования/остатка и времена сброса;
- уведомления о подтверждённом обновлении окна, восстановлении доступности, новом reset-кредите,
  существенном неоднозначном изменении и опционально об истечении кредита;
- интервалы 5, 15, 30, 60 или 5–1440 минут, ручная проверка и пауза;
- ограниченная история и сохраняемая outbox-очередь в JSON;
- установка одним интерактивным сценарием и CI-approved автообновления с healthcheck/rollback.

Нет базы данных, webhook, входящих портов, reverse proxy, Docker socket внутри контейнера и
браузерного парсинга.

## Быстрая установка на Debian/Ubuntu

Репозиторий по умолчанию приватный. На сервере создайте отдельный SSH-ключ и добавьте **только его
public key** в GitHub → repository Settings → Deploy keys с выключенным `Allow write access`:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/codex-notify-deploy -N ''
cat ~/.ssh/codex-notify-deploy.pub
```

Добавьте изолированный alias в `~/.ssh/config`:

```sshconfig
Host github-codex-notify
  HostName github.com
  User git
  IdentityFile ~/.ssh/codex-notify-deploy
  IdentitiesOnly yes
```

Затем:

```bash
git clone --branch deploy git@github-codex-notify:Avazbek22/codex-notify.git
cd codex-notify
sudo ./install.sh
```

Установщик запросит токен, проверит его через Telegram `getMe`, предложит numeric Telegram ID или
покажет одноразовую ссылку привязки на 15 минут, соберёт образ и включит отдельный systemd-таймер
обновлений. Существующие контейнеры, сети, firewall, SSH и Docker daemon config не меняются.

После привязки откройте бота: **Аккаунт → Подключить Codex**, перейдите по адресу
`https://auth.openai.com/codex/device` и введите показанный одноразовый код. Device-code вход имеет
статус beta и может потребовать включения в ChatGPT Security или разрешения администратора workspace.

Подробно: [инструкция на русском](docs/ru-installation.md),
[архитектура и проверенные интерфейсы](docs/architecture.md), [модель угроз](SECURITY.md).

## Разработка

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
ruff check .
ruff format --check .
mypy codex_notify
pytest
```

Docker-сборка поддерживает `linux/amd64` и `linux/arm64`. Оба официальных Linux-артефакта Codex
0.154.0 опубликованы с checksum; CI реально собирает amd64. Arm64-путь проверяется статически и
требует живой smoke test на arm64-хосте перед эксплуатацией.

## Ограничения

События между двумя опросами могут остаться незамеченными. Уведомление обычно задерживается на
выбранный интервал. Некоторые аккаунты не получают `ordinaryUsageAllowed`, reset-кредиты или полные
детали — бот показывает «неизвестно», а не ноль. Авария после ответа Telegram и до записи
подтверждения может дать редкий повтор. Поведение зависит от закреплённой версии Codex; реальный
device-code E2E требует участия владельца и не заменяется mock-тестом.

## Лицензия

Собственный код — MIT. Заимствованные механизмы и бинарные зависимости перечислены в
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
