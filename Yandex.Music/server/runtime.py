"""Private deployment adapter around the audited, unchanged upstream server."""
import hmac
import os
from pathlib import Path
import sqlite3
import sys

os.umask(0o077)
from yandex_music_mcp import server as upstream

HOME = Path('/var/lib/yandex-music-mcp')
TOKEN = HOME / '.yandex-music-mcp/token'
REGISTRY = HOME / 'created-playlists.sqlite3'


def exact_token() -> str:
    if not TOKEN.is_file():
        raise RuntimeError('Сначала подтвердите вход в Яндекс Музыку.')
    info = TOKEN.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Неверные права на файл токена.')
    value = TOKEN.read_text(encoding='utf-8').strip()
    if not value:
        raise RuntimeError('Файл токена пуст.')
    return value


upstream._load_token = exact_token
for tool in ['export_tracks_to_file', 'like_tracks', 'unlike_tracks', 'remove_tracks_from_playlist', 'create_playlist', 'add_tracks_to_playlist']:
    upstream.mcp.remove_tool(tool)

with sqlite3.connect(REGISTRY) as connection:
    connection.execute('CREATE TABLE IF NOT EXISTS created (uid INTEGER NOT NULL, kind INTEGER NOT NULL, PRIMARY KEY(uid, kind))')


@upstream.mcp.tool(annotations=upstream.WRITE)
def create_playlist(title: str) -> dict:
    """Создать новый приватный плейлист."""
    if not title.strip() or len(title) > 200:
        raise ValueError('Название должно содержать от 1 до 200 символов.')
    playlist = upstream.create_playlist(title=title, visibility='private')
    uid = upstream._uid()
    with sqlite3.connect(REGISTRY) as connection:
        connection.execute('INSERT OR IGNORE INTO created(uid, kind) VALUES(?, ?)', (uid, int(playlist['kind'])))
    return playlist


@upstream.mcp.tool(annotations=upstream.WRITE)
def add_tracks_to_playlist(kind: int, track_ids: list[str], at_start: bool = False) -> dict:
    """Добавить треки в любой собственный плейлист текущего аккаунта, новый или существующий. До 100 треков за вызов."""
    if not 1 <= len(track_ids) <= 100:
        raise ValueError('За один вызов допускается от 1 до 100 треков.')
    uid = upstream._uid()
    playlist = upstream.client().users_playlists(kind, uid)
    if playlist is None or playlist.owner is None or int(playlist.owner.uid) != int(uid):
        raise ValueError('Плейлист не найден или принадлежит другому аккаунту.')
    return upstream.add_tracks_to_playlist(kind=kind, track_ids=track_ids, at_start=at_start)


@upstream.mcp.tool(annotations=upstream.DELETE)
def delete_playlist(kind: int) -> dict:
    """Удалить указанный плейлист текущего аккаунта по явному запросу пользователя. Любой собственный плейлист, включая «Понравилось на Радио», допускается к удалению."""
    uid = upstream._uid()
    api = upstream.client()
    playlist = api.users_playlists(kind, uid)
    if playlist is None or playlist.owner is None or int(playlist.owner.uid) != int(uid):
        raise ValueError('Плейлист не найден или принадлежит другому аккаунту.')
    title = playlist.title
    if not api.users_playlists_delete(kind, uid):
        raise RuntimeError('Яндекс Музыка не подтвердила удаление плейлиста.')
    with sqlite3.connect(REGISTRY) as connection:
        connection.execute('DELETE FROM created WHERE uid=? AND kind=?', (uid, kind))
    return {'deleted': True, 'kind': kind, 'title': title}


class PrivateBearer:
    def __init__(self, app, secret: str):
        self.app = app
        self.expected = ('Bearer ' + secret).encode('ascii')

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope.get('headers', []))
            if not hmac.compare_digest(headers.get(b'authorization', b''), self.expected):
                from starlette.responses import PlainTextResponse
                response = PlainTextResponse('Authentication required', status_code=401, headers={'WWW-Authenticate': 'Bearer realm="yandex-music-private"'})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main():
    if '--http' in sys.argv:
        import uvicorn
        settings = upstream.mcp.settings
        settings.host = '127.0.0.1'
        settings.port = 18765
        settings.stateless_http = True
        settings.json_response = True
        settings.log_level = 'WARNING'
        settings.max_request_body_size = 65536
        app = upstream.mcp.streamable_http_app()
        secret = (HOME / '.mcp-http-secret').read_text().strip()
        if len(secret) < 40:
            raise RuntimeError('HTTP secret missing or invalid')
        uvicorn.run(PrivateBearer(app, secret), host='127.0.0.1', port=18765, log_level='warning', access_log=False)
    else:
        upstream.mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
