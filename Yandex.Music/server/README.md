# Сервер Яндекс Музыки

[← Комплект и возможности](../README.md) · [Клиент SSH](client/README.md)

Снимок частной установки от **25 октября 2026 года**. Основные Python-файлы, wheel и unit-файлы сохранены без изменения байтов; их хеши перечислены в [`../provenance.json`](../provenance.json). Развёрнутая установка использует **Python 3.14.4**. Локальные проверки discovery также выполняются на Python 3.12.14; это не подтверждает совместимость всего OAuth/API-сервера с другой версией Python.

## Режимы работы

**HTTPS для ChatGPT.** `direct_oauth.py` слушает `127.0.0.1:18767`, предоставляет `/direct/mcp`, OAuth с PKCE и служебный профиль владельца. systemd unit — [`deploy/yandex-music-direct.service`](deploy/yandex-music-direct.service). Прокси публикует только предусмотренные маршруты через HTTPS.

**Выполнение по запросу через SSH.** Клиент [`client/adapter.py`](client/adapter.py) отвечает на discovery из локального `music_catalog.json`. Только `tools/call` запускает SSH к `deploy/yandex-music-mcp-once`; `oneshot.py` исполняет одну команду и завершается. В этом режиме нет постоянного SSH-соединения и автоматического повтора изменений.

В снимке также сохранены `lazy_stdio.py`, `idle_worker.py`, wrapper `yandex-music-mcp-ssh` и units сокета/исполнителя: это поддерживаемый серверный путь через приватный Unix-сокет с остановкой исполнителя после простоя. Для текущего локального адаптера используется отдельный одноразовый путь `oneshot.py`. Старый HTTP bearer-режим `runtime.py --http` требует отдельного `.mcp-http-secret`; он не нужен для OAuth HTTP и не описан как основной способ установки.

14 музыкальных инструментов берутся из `runtime.py` и `yandex_music_mcp/server.py`; HTTP добавляет `get_profile`. Обёртка ограничивает запись собственными плейлистами, создаёт их приватными и не предоставляет скачивание, лайки/снятие лайков и удаление отдельных треков из плейлиста. Полный список опубликованных инструментов хранится в [`music_catalog.json`](music_catalog.json).

## Подготовка собственной установки

Это инструкция по адаптации снимка, а не готовый установщик. Команды ниже рассчитаны на Ubuntu/Debian с уже установленным Python 3.14, модулем `venv`, systemd и nginx. Создайте отдельное приложение для ChatGPT и свой HTTPS-домен. Рабочие секреты существующей установки в комплект не входят.

1. Создайте системного пользователя `ymmusic`, каталог исходников `/opt/yandex-music-mcp` и закрытый каталог состояния `/var/lib/yandex-music-mcp`. Скопируйте **содержимое** этой папки `server/` в `/opt/yandex-music-mcp`.
2. Установите зависимости. В исходном `requirements.lock.txt` сохранена абсолютная ссылка на прилагаемый wheel. Она работает при указанном расположении; при другом пути замените только этот путь в своей копии lock-файла. Подробности сборки wheel — в [`dependency-provenance.json`](dependency-provenance.json); лицензия LGPLv3 сохранена внутри wheel.

```sh
sudo useradd --system --home-dir /var/lib/yandex-music-mcp --shell /usr/sbin/nologin ymmusic
sudo install -d -o ymmusic -g ymmusic -m 700 /var/lib/yandex-music-mcp
sudo install -d -o ymmusic -g ymmusic -m 700 /var/lib/yandex-music-mcp/.yandex-music-mcp
sudo python3.14 -m venv /opt/yandex-music-mcp/venv
sudo /opt/yandex-music-mcp/venv/bin/python -m pip install -r /opt/yandex-music-mcp/requirements.lock.txt
```

Создавать пользователя повторно не нужно, если он уже существует. Учтите, что `requirements.lock.txt` фиксирует версии конкретного окружения; установка зависимостей в чистом окружении при подготовке архива не повторялась.

3. Получите токен **своего** аккаунта через [`authorize.py`](authorize.py). Скрипт выводит ссылку Яндекса и код, ждёт входа пользователя и сохраняет токен только на сервере. Не отправляйте токен в чат. Он нужен и HTTPS-серверу, и SSH-исполнителю.

```sh
sudo -u ymmusic /opt/yandex-music-mcp/venv/bin/python /opt/yandex-music-mcp/authorize.py
```

Файлы `token` и `oauth.json` сохраняются в `/var/lib/yandex-music-mcp/.yandex-music-mcp/`, OAuth HTTP использует `direct-oauth.sqlite3`, а `runtime.py` — `created-playlists.sqlite3`. Пользователь `ymmusic` должен владеть этими файлами; у токена должны быть права `0600`. Значение токена хранится без шифрования на диске, доступ защищается правами ОС. Пустой каталог создайте до запуска авторизации.

## HTTPS и регистрация ChatGPT

Перед запуском измените в **своей копии** `direct_oauth.py`:

- `ORIGIN`: свой HTTPS origin; `RESOURCE` использует его вместе с `/direct/mcp`.
- `allowed_hosts` и `allowed_origins` в `build_app`: свой домен и HTTPS origin, сохранив нужные loopback и ChatGPT значения.
- При изменении портов и каталогов согласуйте пути во всех Python-файлах, wrappers, units и конфигурации прокси.

В [`deploy/nginx-site.example.conf`](deploy/nginx-site.example.conf) замените домен и пути сертификата. Это пример для отдельного HTTPS-host; выдача сертификатов и общая сеть сервера настраиваются отдельно. Полная конфигурация производственного сервера не копировалась.

После адаптации файлов и подготовки nginx:

```sh
sudo install -m 644 /opt/yandex-music-mcp/deploy/yandex-music-direct.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo nginx -t
sudo systemctl enable --now yandex-music-direct.service
sudo systemctl reload nginx
sudo systemctl status yandex-music-direct.service --no-pager
```

В своём приложении ChatGPT укажите `https://music.example.com/direct/mcp` с OAuth. Затем замените имя и App ID в `../plugin/plugin.json`, `../plugin/.codex-plugin/plugin.json` и `../plugin/.app.json` и соберите собственный пакет. Снимок сохраняет идентификаторы приложения владельца, а не универсальную регистрацию.

Этот сервер рассчитан на один заранее подключённый аккаунт. Авторизация проверяет владельца; вход другого пользователя не переключает исполнитель на его музыкальный аккаунт. Не публикуйте установку как многопользовательский сервис без отдельной переработки управления аккаунтами.

## SSH-клиент и серверные wrappers

Для одноразового режима установите `deploy/yandex-music-mcp-once` в `/usr/local/bin/` с правами `0755` и настройте SSH-доступ к своему серверу. Wrapper запускает изолированную transient systemd unit от `ymmusic`; SSH-пользователю нужны разрешения на такой запуск. Настройка ключей и полномочий остаётся задачей администратора. Снимок клиента использует SSH-пользователя `root`; прежде чем применять его, настройте подходящий для своей установки способ доступа.

Адаптируйте [`client/adapter.py`](client/adapter.py) по [инструкции клиента](client/README.md), включая host, имя пользователя и путь ключа. Для необязательного режима общего исполнителя установите `yandex-music-mcp-worker.service`, `yandex-music-mcp-worker.socket` и wrapper `yandex-music-mcp-ssh`; включается socket, а не постоянная service. Unix-сокет имеет права `0600`. Текущий on-demand клиент не требует включения этого сокета.

## Проверки без аккаунта

Для проверки полного комплекта нужен checkout репозитория: утилита упаковки и общие контрольные суммы не входят в серверный ZIP. Из корня репозитория:

```sh
python Yandex.Music/tools/verify_package.py
python -m unittest discover -s Yandex.Music/server/tests -v
```

Проверка пакета использует только стандартную библиотеку: JSON, синтаксис Python, PNG, содержимое wheel, исходные хеши, список 14 инструментов и побайтовое соответствие ZIP исходникам. Для повторной сборки исходного снимка:

```sh
python Yandex.Music/tools/build_releases.py
python Yandex.Music/tools/verify_package.py
```

Проверяющая утилита сверяет именно опубликованные App ID, версии и исходные хеши. После адаптации к своему домену или приложению эти ожидания и `provenance.json` нужно осознанно обновить для собственного выпуска; сборщик ZIP не меняет их автоматически. Из отдельно распакованного серверного ZIP offline-тесты можно запустить командой `python -m unittest discover -s tests -v`.

Существующие восемь offline-тестов проверяют discovery без пробуждения исполнителя, ошибки входных данных, отсутствие повторной записи после потери ответа, stdio и жизненный цикл Linux-исполнителя с фиктивными запросами. Три socket-теста пропускаются на Windows. Реальные API-команды, авторизация ChatGPT и установка зависимостей не входят в эти проверки. Производственные исходники тестами не изменяются; тестовый Unix-сокет создаётся во временном каталоге.
