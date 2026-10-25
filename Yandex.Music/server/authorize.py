"""Device login: only verification URL/code are printed, never tokens."""
import datetime
import json
import logging
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse
from yandex_music import Client

os.umask(0o077)
logging.disable(logging.CRITICAL)
directory = Path('/var/lib/yandex-music-mcp/.yandex-music-mcp')


def on_code(code):
    url = urlparse(code.verification_url)
    if url.scheme != 'https' or url.hostname not in {'ya.ru', 'yandex.ru', 'yandex.com', 'oauth.yandex.ru', 'oauth.yandex.com', 'id.yandex.ru', 'id.yandex.com'}:
        raise RuntimeError('Unexpected verification domain; login stopped')
    print(json.dumps({'verification_url': code.verification_url, 'user_code': code.user_code, 'expires_in': code.expires_in}), flush=True)


def atomic_write(name, value):
    fd, temporary = tempfile.mkstemp(dir=directory, prefix='.pending-')
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        file.write(value)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, directory / name)


if __name__ == '__main__':
    try:
        client = Client()
        token = client.device_auth(on_code=on_code, timeout=900, device_name='Personal Yandex Music MCP (VDSina NL)')
        client.init()
        if not getattr(client.me.account, 'uid', None):
            raise RuntimeError('No account uid')
        atomic_write('oauth.json', json.dumps({'access_token': token.access_token, 'refresh_token': token.refresh_token, 'expires_in': token.expires_in, 'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}))
        atomic_write('token', token.access_token)
        print('YANDEX_AUTH_OK: account verified; credentials saved privately on VPS', flush=True)
    except Exception as error:
        print('YANDEX_AUTH_FAILED: ' + type(error).__name__, flush=True)
        raise SystemExit(1)
