# @tg MCP server

Личный Telegram MCP на Bun. Основа: mcp-telegram-cloud 2.63.3, ревизия `d2548becf2a124481313635b6c6d888cd3903fe3`; MIT-лицензия сохранена. Доработки: явный выбор аккаунта, названия и переименование, независимый отзыв MCP-клиентов, защищённая первичная настройка, круглая кастомная иконка и локальные псевдонимы контактов. Сопоставление возможностей — в `../CAPABILITIES.md`.

## Запуск

Требуются Bun 1.4.3 и HTTPS reverse proxy.

```sh
bun install --frozen-lockfile --production --ignore-scripts
bun src/server.tsx
```

Процесс слушает только `127.0.0.1`. `PORT` задаёт порт; на VDSina используется 18768. `ISSUER` — внешний HTTPS origin без пути. Все настройки описаны в `.env.example`. Для production задать `DATABASE_PATH` вне исходников, `SESSION_ENCRYPTION_KEY` (32 байта, 64 hex-символа), случайные `ADMIN_TOKEN` и `LOG_HASH_SALT`. Не публиковать `.env`, базу, ключи и резервные копии состояния.

## Первичная настройка

Telegram API ID/hash можно задать окружением или в защищённом браузерном потоке. Без них сервер запускается, но QR-вход не выполняется.

Задайте случайный `TG_SETUP_TOKEN` длиной от 32 символов и абсолютный `TG_CREDENTIALS_FILE` вне каталога исходников. Откройте `/setup#token=<ваш-ключ>`: fragment не передаётся в HTTP URL и удаляется после заполнения формы. Введите собственные API ID/hash из `my.telegram.org/apps`. Форма создаёт файл атомарно с правами `0600` и запрещает повторную запись. API-ключи, пароли и коды нельзя передавать в чат.

Затем откройте `/login`, подтвердите QR нужным Telegram-аккаунтом и при необходимости введите 2FA на той же HTTPS-странице. `/my/accounts` позволяет назвать первый аккаунт, добавить другие, переименовать и отключить каждый отдельно.

## MCP и аккаунты

Endpoint `/mcp` защищён OAuth authorization-code + PKCE с DCR. Discovery: `/.well-known/oauth-authorization-server` и `/.well-known/oauth-protected-resource`. Access token живёт 1 час; rotating refresh — 90 дней. Отключение одного OAuth-клиента сохраняет остальные подключения и Telegram-сессии.

Инструменты принимают `account` (ID/название/`primary`) или числовой `account_id`; одновременно оба поля запрещены. При нескольких аккаунтах запись требует явного отправителя. Поддерживаемое чтение с `account=all` возвращает подписанные результаты и отдельные ошибки. `telegram-accounts-switch` задаёт предпочтение чтения внутри конкретной MCP-сессии. Псевдонимы контактов предназначены для точного поиска; автоматической подмены адресата нет.

## VDSina

`deploy/install-release.py` рассчитан на текущий сервер с nginx SNI listener. Он создаёт отдельного Linux-пользователя, проверяет `/health` до изменения nginx и выполняет `nginx -t` перед reload. Для другого сервера адаптировать домен, сертификат и сетевую конфигурацию.

Этот снимок поставляется как backend на Bun с systemd/nginx. Dockerfile исходного проекта требует исключённый frontend-workspace и не входит в комплект; инструкции исторического upstream не являются готовым Docker-путём для @tg.

Версии: `/opt/tg-mcp/releases/<release>`, symlink `/opt/tg-mcp/current`. Состояние: `/var/lib/tg-mcp`. Закрытая конфигурация: `/etc/tg-mcp.env` (`0600`). Bun: `/opt/tg-runtime/bun`. Служба: `tg-mcp.service`. Обновления сохраняют ключи и состояние; каждый deploy требует нового release ID. Перед обновлением сохранять резервную копию SQLite и предыдущий release.

## Проверки

```sh
bun run typecheck
bun test src/__tests__/tg-account-routing.test.ts
python ../tools/build_releases.py
python ../tools/verify_package.py
```

Полный upstream-набор использует глобальные env/module mock fixtures: запускать каждый `src/__tests__/*.test.ts*` отдельным процессом `bun test <file>`. Реальные QR/2FA, чтение и отправка проверяются после входа владельца; заглушки этого не доказывают.

Все управляемые иконки должны быть утверждённым круглым знаком @tg, включая отдельную регистрацию App. Карта приёмки: `../ICON-COVERAGE.md`. Исторические документы upstream в `docs/` описывают исходный проект; актуальные настройки этого fork приведены здесь и в `.env.example`.
