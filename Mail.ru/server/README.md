# Mail.ru MCP 0.3.21

Python HTTP MCP-сервер с OAuth/PKCE, IMAP/SMTP Mail.ru и интерфейсом подключения ящиков. Исходники `mailru_mcp/` совпадают с работающей установкой по SHA-256. Установка на VDSina использует Python 3.14.4; тестовый комплект также проверяется на Python 3.12.14. Для многопользовательской конфигурации требуется `cryptography`, версия развёрнутого сервера указана в `requirements.txt`.

## Локальные тесты

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

На Windows используйте `.venv/Scripts/python.exe`. Тесты поднимают локальный HTTP-сервер, создают временные базы и подменяют почтовые операции; реальные пароли и подключение ящиков для них не нужны.

## Новая установка на Ubuntu/Debian

Скопируйте содержимое этой папки в `/tmp/mailru-mcp`. `deploy/install.sh` устанавливает исходники и systemd unit, но **не создаёт Python-окружение и не запускает службу**. Он отказывается перезаписывать существующую установку. Выполните от root:

```sh
bash /tmp/mailru-mcp/deploy/install.sh
python3 -m venv /opt/mailru-mcp/venv
/opt/mailru-mcp/venv/bin/python -m pip install -r /tmp/mailru-mcp/requirements.txt
```

Настройте `/etc/mailru-mcp.env` по `.env.example`, права `root:root 0600`. Для текущего режима оставьте `MULTI_ACCOUNT=1`, задайте публичный HTTPS origin и сгенерируйте `MAIL_CREDENTIAL_KEY` непосредственно на сервере:

```sh
/opt/mailru-mcp/venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Сохраните ключ только в закрытом файле настроек. Он нужен для расшифровки существующих подключений; при переносе рабочей установки его нельзя менять без отдельной миграции. Ящики подключаются через веб-интерфейс с отдельным паролем приложения Mail.ru.

`deploy/configure.py` — прежний интерактивный помощник для **одноящичного** режима: он задаёт `MAILRU_EMAIL`, `MAILRU_APP_PASSWORD` и `MCP_LOGIN_PASSWORD`. Для него нужно использовать `MULTI_ACCOUNT=0`; текущую конфигурацию с несколькими ящиками он не заменяет.

## HTTPS и ресурсы интерфейса

- `deploy/nginx-vdsina.conf` — снимок действующего vhost. В этой установке публичный 443 занят stream-маршрутизатором, а HTTP TLS vhost слушает `127.0.0.1:8443`.
- `deploy/prepare-sni.py` — помощник для конкретной существующей схемы SNI; изучите его перед использованием.
- `deploy/nginx-site.example.conf` и `deploy/Caddyfile.example` — альтернативные шаблоны для отдельного HTTPS-прокси.
- `deploy/cert-renew-hook.sh` — обработчик обновления сертификатов.

При переносе замените домен, пути сертификатов и порты согласно инфраструктуре. Закрытых ключей сертификатов в репозитории нет. Не запускайте второй прокси поверх уже занятого порта.

Важно сохранить правила nginx для `/login.css`, `/login.js`, `/login-icon.png`, `/roboto.ttf`, а также `/cancel` и `/icon.png`. Приложение хранит все эти ресурсы в `mailru_mcp/`, включая `roboto-OFL.txt` с лицензией шрифта.

После настройки HTTPS и закрытого файла окружения:

```sh
systemctl enable --now mailru-mcp
curl --fail http://127.0.0.1:8765/health
```

Ожидается `{"status":"ok"}`. Endpoint MCP — `/mcp`; OAuth discovery — `/.well-known/oauth-authorization-server`. Для подключения используется HTTPS, сервер слушает только loopback. `deploy/preflight.py` проверяет доступность TLS IMAP/SMTP без входа. `deploy/check-mailbox.py` предназначен для отдельной проверки прежнего одноящичного режима и требует настроенного окружения.

## Существующая установка и данные

Эта папка — исходники и шаблоны, а не полная резервная копия подключённых аккаунтов. Рабочий `/etc/mailru-mcp.env` и `/var/lib/mailru-mcp/state.sqlite3` сохраняются отдельно в защищённом хранилище. Обновление существующей службы требует отдельной резервной копии и процедуры; `install.sh` для обновлений не предназначен.

Подготовка данного снимка не изменяла и не перезапускала рабочую службу.
