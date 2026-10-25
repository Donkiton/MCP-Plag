"""Private direct MCP OAuth; music tools are reused without proxying MCP."""
import asyncio
from contextlib import contextmanager
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from pydantic import BaseModel, ConfigDict, Field

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, RegistrationError, AuthorizeError, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool

ORIGIN = 'https://77-238-249-144.sslip.io'
RESOURCE = ORIGIN + '/direct/mcp'
COOKIE = '__Host-ym-owner'


class FlowError(ValueError):
    """Only these intentionally safe messages may appear in the owner UI."""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def callback_allowed(value):
    u = urlsplit(str(value))
    return (u.scheme == 'https' and u.netloc == 'chatgpt.com' and not u.query and not u.fragment
            and (u.path == '/connector_platform_oauth_redirect'
                 or re.fullmatch(r'/connector/oauth/[A-Za-z0-9_-]{1,200}', u.path) is not None))


class Provider:
    def __init__(self, path, owner_uid, identity=None):
        self.path = str(path)
        self.owner_uid = str(owner_uid)
        self.identity = identity or YandexIdentity()
        self.pending = {}
        os.umask(0o077)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, data TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS codes(hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS families(id TEXT PRIMARY KEY, client TEXT NOT NULL, expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, subject TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, family TEXT NOT NULL, kind TEXT NOT NULL, expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS tokens_family ON tokens(family);
            ''')
            if 'subject' not in [row[1] for row in db.execute('PRAGMA table_info(families)')]:
                db.execute("ALTER TABLE families ADD COLUMN subject TEXT NOT NULL DEFAULT ''")
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level='IMMEDIATE')
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def cleanup(self, db):
        now = int(time.time())
        db.execute('DELETE FROM codes WHERE expires < ?', (now,))
        db.execute('DELETE FROM tokens WHERE family IN (SELECT id FROM families WHERE expires < ?)', (now,))
        db.execute('DELETE FROM families WHERE expires < ?', (now,))
        db.execute('DELETE FROM clients WHERE created < ? AND id NOT IN (SELECT client FROM families)', (now-7*86400,))

    async def get_client(self, client_id):
        with self.db() as db:
            row = db.execute('SELECT data FROM clients WHERE id=?', (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row['data']) if row else None

    async def register_client(self, client_info):
        if not client_info.redirect_uris or not all(callback_allowed(u) for u in client_info.redirect_uris):
            raise RegistrationError('invalid_redirect_uri', 'Only ChatGPT callback URLs are allowed.')
        if client_info.token_endpoint_auth_method not in ('none', 'client_secret_post', 'client_secret_basic'):
            raise RegistrationError('invalid_client_metadata', 'Unsupported client authentication.')
        if set(client_info.grant_types) != {'authorization_code', 'refresh_token'} or client_info.response_types != ['code']:
            raise RegistrationError('invalid_client_metadata', 'Authorization code and refresh token are required.')
        with self.db() as db:
            self.cleanup(db)
            if db.execute('SELECT count(*) FROM clients').fetchone()[0] >= 256:
                raise RegistrationError('invalid_client_metadata', 'Registration capacity reached.')
            db.execute('INSERT INTO clients VALUES(?,?,?)', (client_info.client_id, client_info.model_dump_json(), int(time.time())))

    async def authorize(self, client, params):
        if params.resource not in (None, RESOURCE):
            raise AuthorizeError('invalid_request', 'Wrong resource.')
        if (not callback_allowed(params.redirect_uri) or params.scopes not in (None, ['music'])
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', params.code_challenge)):
            raise AuthorizeError('invalid_request', 'Invalid authorization parameters.')
        now = time.time()
        self.pending = {k:v for k,v in self.pending.items() if v['expires'] > now}
        if len(self.pending) >= 32:
            raise AuthorizeError('temporarily_unavailable', 'Please retry later.')
        tx = secrets.token_urlsafe(32)
        self.pending[tx] = dict(client=client.client_id, params=params, expires=now+600,
                                browser=None, csrf=secrets.token_urlsafe(32), verified=False,
                                device=None, busy=False, next_poll=0)
        return ORIGIN + '/owner/login?' + urlencode({'tx':tx})

    async def load_authorization_code(self, client, authorization_code):
        with self.db() as db:
            row = db.execute('SELECT data FROM codes WHERE hash=?', (digest(authorization_code),)).fetchone()
        if not row:
            return None
        data = json.loads(row['data'])
        data['code'] = authorization_code
        return AuthorizationCode(**data)

    def issue(self, db, client_id, family=None):
        now = int(time.time())
        if family is None:
            family = secrets.token_urlsafe(24)
            db.execute('INSERT INTO families(id,client,expires,subject) VALUES(?,?,?,?)', (family, client_id, now+90*86400,self.owner_uid))
        row = db.execute('SELECT expires FROM families WHERE id=?', (family,)).fetchone()
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        db.execute('INSERT INTO tokens VALUES(?,?,?,?,0)', (digest(access), family, 'access', min(now+3600,row[0])))
        db.execute('INSERT INTO tokens VALUES(?,?,?,?,0)', (digest(refresh), family, 'refresh', row[0]))
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=min(3600,row[0]-now), scope='music')

    async def exchange_authorization_code(self, client, authorization_code):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('DELETE FROM codes WHERE hash=? AND expires >= ? RETURNING data',
                             (digest(authorization_code.code), int(time.time()))).fetchone()
            if (not row or json.loads(row[0])['client_id'] != client.client_id
                    or json.loads(row[0])['subject'] != self.owner_uid):
                raise TokenError('invalid_grant', 'Code expired or already used.')
            self.cleanup(db)
            return self.issue(db, client.client_id)

    def lookup(self, token, kind):
        with self.db() as db:
            row = db.execute('''SELECT t.*,f.client,f.revoked,f.subject,f.expires AS family_expires FROM tokens t
                JOIN families f ON f.id=t.family WHERE t.hash=? AND t.kind=?''', (digest(token),kind)).fetchone()
        return row

    async def load_refresh_token(self, client, refresh_token):
        row = self.lookup(refresh_token, 'refresh')
        if not row or row['revoked'] or row['expires'] <= time.time() or row['client'] != client.client_id or row['subject'] != self.owner_uid:
            return None
        if row['used']:
            with self.db() as db:
                db.execute('UPDATE families SET revoked=1 WHERE id=?', (row['family'],))
            return None
        return RefreshToken(token=refresh_token, client_id=row['client'], scopes=['music'],
                            expires_at=row['expires'],resource=RESOURCE,subject=self.owner_uid)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        failure = False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT t.*,f.client,f.revoked,f.subject FROM tokens t JOIN families f ON f.id=t.family
                WHERE t.hash=? AND t.kind='refresh' ''', (digest(refresh_token.token),)).fetchone()
            if (not row or row['revoked'] or row['used'] or row['expires'] <= time.time()
                    or row['client'] != client.client_id or scopes != ['music'] or row['subject'] != self.owner_uid):
                if row and row['client'] == client.client_id and row['used']:
                    db.execute('UPDATE families SET revoked=1 WHERE id=?', (row['family'],))
                failure = True
            else:
                db.execute('UPDATE tokens SET used=1 WHERE hash=?', (digest(refresh_token.token),))
                return self.issue(db, client.client_id, row['family'])
        if failure:
            raise TokenError('invalid_grant', 'Invalid refresh token.')

    async def load_access_token(self, token):
        row = self.lookup(token, 'access')
        if not row or row['revoked'] or row['expires'] <= time.time() or row['subject'] != self.owner_uid:
            return None
        return AccessToken(token=token, client_id=row['client'],scopes=['music'],expires_at=row['expires'],
                           resource=RESOURCE,subject=self.owner_uid)

    async def revoke_token(self, token):
        with self.db() as db:
            db.execute('UPDATE families SET revoked=1 WHERE id IN (SELECT family FROM tokens WHERE hash=?)', (digest(token.token),))

    async def revoke(self, request):
        # SDK 1.30 requires client_secret even for a public OAuth client.
        from mcp.server.auth.middleware.client_auth import ClientAuthenticator, AuthenticationError
        try:
            client = await ClientAuthenticator(self).authenticate_request(request)
        except AuthenticationError:
            return JSONResponse({'error':'invalid_client'},status_code=401)
        form = await request.form()
        raw = form.get('token')
        if not isinstance(raw,str) or not raw:
            return JSONResponse({'error':'invalid_request'},status_code=400)
        token = await self.load_access_token(raw) or await self.load_refresh_token(client,raw)
        if token and token.client_id == client.client_id:
            await self.revoke_token(token)
        from starlette.responses import Response
        return Response(status_code=200)

    def transaction(self, request, mutation=False):
        tx = request.query_params.get('tx','')
        p = self.pending.get(tx)
        if not p or p['expires'] <= time.time():
            raise FlowError('Сеанс истёк. Начните подключение заново из ChatGPT.')
        cookie = request.cookies.get(COOKIE,'')
        if p['browser'] is not None and not hmac.compare_digest(p['browser'],digest(cookie)):
            raise FlowError('Откройте вход в том браузере, где начато подключение.')
        if mutation and (p['browser'] is None or request.headers.get('origin') != ORIGIN):
            raise FlowError('Проверка браузера не пройдена. Начните подключение заново из ChatGPT.')
        return tx,p

    async def login(self, request):
        try:
            tx,p = self.transaction(request)
        except FlowError as exc:
            return page(str(exc),status=400)
        cookie = None
        if p['browser'] is None:
            cookie = secrets.token_urlsafe(32)
            p['browser'] = digest(cookie)
        hidden = '<input type="hidden" name="csrf" value="'+p['csrf']+'">'
        query = html.escape(urlencode({'tx':tx}),quote=True)
        if p['verified']:
            body = ('<p>Владелец музыкального аккаунта подтверждён.</p><p>ChatGPT получит доступ к 14 инструментам: поиск, рекомендации, история, '
                    'просмотр музыки и плейлистов, создание приватных плейлистов, добавление треков и удаление ваших плейлистов по запросу.</p>'
                    '<form method="post" action="/owner/action?'+query+'">'+hidden+
                    '<button name="action" value="approve">Подключить Яндекс к ChatGPT</button> '
                    '<button name="action" value="deny">Отмена</button></form>')
        elif p['device'] is not None:
            d = p['device']
            body = ('<p>Откройте Яндекс и введите код:</p><p class="code">'+html.escape(d.user_code)+'</p>'
                    '<p><a target="_blank" rel="noopener noreferrer" href="'+html.escape(d.verification_url,quote=True)+'">Войти в Яндекс</a></p>'
                    '<p>Используйте тот же аккаунт, что в действующем музыкальном плагине.</p>'
                    '<form method="post" action="/owner/action?'+query+'">'+hidden+
                    '<button name="action" value="poll">Я подтвердил вход — проверить</button></form>')
        else:
            body = ('<p>Это личный музыкальный плагин на VDSina. Вход разрешён только владельцу текущего аккаунта Яндекс Музыки.</p>'
                    '<form method="post" action="/owner/action?'+query+'">'+hidden+
                    '<button name="action" value="start">Подтвердить аккаунт через Яндекс</button></form>')
        body += '<p class="small">Адрес возврата: '+html.escape(str(p['params'].redirect_uri))+'</p>'
        response = page(body,raw=True)
        if cookie:
            response.set_cookie(COOKIE,cookie,max_age=600,secure=True,httponly=True,samesite='lax',path='/')
        return response

    async def action(self, request):
        try:
            tx,p = self.transaction(request,mutation=True)
            form = await request.form()
            if not hmac.compare_digest(str(form.get('csrf','')),p['csrf']):
                raise FlowError('Проверка формы не пройдена.')
            action = form.get('action')
            if p['busy']:
                raise FlowError('Дождитесь завершения предыдущей проверки.')
            if action == 'start' and not p['verified'] and p['device'] is None:
                p['busy'] = True
                try:
                    d = await run_in_threadpool(self.identity.start)
                    u = urlsplit(d.verification_url)
                    if u.scheme != 'https' or u.hostname not in ('ya.ru','yandex.ru','yandex.com','oauth.yandex.ru','oauth.yandex.com','id.yandex.ru','id.yandex.com'):
                        raise FlowError('Неожиданный адрес авторизации Яндекса.')
                    p['device'] = d
                    p['expires'] = min(p['expires'],time.time()+d.expires_in)
                    p['next_poll'] = time.time()+max(5,d.interval)
                finally:
                    p['busy'] = False
            elif action == 'poll' and p['device'] is not None and not p['verified']:
                if time.time() < p['next_poll']:
                    raise FlowError('Подождите несколько секунд перед следующей проверкой.')
                p['next_poll'] = time.time()+max(5,p['device'].interval)
                p['busy'] = True
                try:
                    uid = await run_in_threadpool(self.identity.poll, p['device'].device_code)
                    if uid is None:
                        raise FlowError('Яндекс ещё не подтвердил вход. Подтвердите код и повторите проверку.')
                    if not hmac.compare_digest(str(uid),self.owner_uid):
                        del self.pending[tx]
                        return page('Этот аккаунт не является владельцем плагина.',status=403)
                    p['verified'] = True
                    p['device'] = None
                finally:
                    p['busy'] = False
            elif action in ('approve','deny') and p['verified']:
                params = p['params']
                from mcp.server.auth.provider import construct_redirect_uri
                args = {'state':params.state,'iss':ORIGIN+'/'}
                if action == 'approve':
                    code = secrets.token_urlsafe(48)
                    data = dict(scopes=['music'],expires_at=time.time()+90,client_id=p['client'],
                                code_challenge=params.code_challenge,redirect_uri=str(params.redirect_uri),
                                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                                resource=RESOURCE,subject=self.owner_uid)
                    with self.db() as db:
                        db.execute('INSERT INTO codes VALUES(?,?,?)',(digest(code),json.dumps(data),int(data['expires_at'])))
                    args['code'] = code
                else:
                    args['error'] = 'access_denied'
                del self.pending[tx]
                response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri),**args),status_code=303)
                response.delete_cookie(COOKIE,secure=True,httponly=True,samesite='lax')
                return response
            else:
                raise FlowError('Недопустимый шаг подключения.')
        except FlowError as exc:
            return page(str(exc),status=400)
        except Exception:
            # Do not expose Yandex exceptions, device codes, account IDs or tokens.
            return page('Не удалось проверить вход в Яндекс. Начните подключение заново или повторите позже.',status=502)
        return RedirectResponse(ORIGIN+'/owner/login?'+urlencode({'tx':tx}),status_code=303)


class YandexIdentity:
    def start(self):
        from yandex_music import Client, DeviceCode
        from yandex_music._client.device_auth import _DEFAULT_CLIENT_ID
        client = Client()
        result = client._request.post('https://oauth.yandex.ru/device/code', {
            'client_id': _DEFAULT_CLIENT_ID, 'device_id': secrets.token_hex(16),
            'device_name': 'Яндекс — проверка владельца MCP', 'scope': 'login:info'})
        code = DeviceCode.de_json(result, client)
        if code is None:
            raise FlowError('Не удалось начать проверку владельца.')
        return code

    def poll(self, code):
        from yandex_music import Client
        token = Client().poll_device_token(code)
        if token is None:
            return None
        import httpx
        response = httpx.get('https://login.yandex.ru/info', params={'format':'json'},
                             headers={'Authorization':'OAuth '+token.access_token}, timeout=20)
        response.raise_for_status()
        uid = response.json().get('id')
        if not uid:
            raise FlowError('Яндекс не вернул идентификатор владельца.')
        return str(uid)


class Profile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, pattern=r'\S')
    name: str
    email: str
    nickname: str


class YandexProfile:
    """Fetch display fields for the existing music owner; never guess an email."""
    def __init__(self, owner_uid, token_reader):
        self.owner_uid = str(owner_uid)
        self.token_reader = token_reader

    def load(self):
        import httpx
        response = httpx.get('https://login.yandex.ru/info', params={'format':'json'},
                             headers={'Authorization':'OAuth '+self.token_reader()}, timeout=20)
        response.raise_for_status()
        data = response.json()
        if not hmac.compare_digest(str(data.get('id','')),self.owner_uid):
            raise FlowError('Профиль Яндекса не соответствует владельцу подключения.')
        email = data.get('default_email')
        nickname = data.get('display_name') or data.get('login')
        if not isinstance(email,str) or not email.strip() or not isinstance(nickname,str) or not nickname.strip():
            raise FlowError('Яндекс не вернул почту или имя профиля.')
        return Profile(id=self.owner_uid,name=email,email=email,nickname=nickname)


def page(body,raw=False,status=200):
    if not raw:
        body = '<p>'+html.escape(body)+'</p><p>Можно вернуться назад к подключению.</p>'
    return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<title>Яндекс — подключение к ChatGPT</title><style>body{font:18px system-ui;max-width:640px;margin:60px auto;padding:24px;line-height:1.5}'
                        'button{font:inherit;padding:12px;margin:6px 0;cursor:pointer}.code{font-size:32px;font-weight:700}.small{font-size:13px;overflow-wrap:anywhere;color:#555}</style>'
                        '<h1>Яндекс</h1>'+body+'</html>',status_code=status)


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope,receive,send)
        async def guarded(message):
            if message['type'] == 'http.response.start':
                if scope['path'] == '/authorize':
                    from urllib.parse import parse_qsl,urlunsplit
                    for i,(key,value) in enumerate(message['headers']):
                        if key.lower() == b'location':
                            u = urlsplit(value.decode())
                            if callback_allowed(urlunsplit((u.scheme,u.netloc,u.path,'',''))):
                                query = parse_qsl(u.query,keep_blank_values=True)
                                query.append(('iss',ORIGIN+'/'))
                                message['headers'][i] = (key,urlunsplit((u.scheme,u.netloc,u.path,urlencode(query),u.fragment)).encode())
                message['headers'] += [(b'cache-control',b'no-store'),(b'referrer-policy',b'same-origin'),
                                       (b'x-content-type-options',b'nosniff'),
                                       (b'content-security-policy',b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'; base-uri 'none'")]
            await send(message)
        # SDK v1.30 does not check the optional resource field on token requests.
        if scope['path'] == '/token' and scope['method'] == 'POST':
            from starlette.requests import Request
            request = Request(scope,receive)
            data = await request.body()
            if len(data) > 65536:
                return await JSONResponse({'error':'invalid_request'},status_code=413)(scope,receive,guarded)
            from urllib.parse import parse_qs
            fields = parse_qs(data.decode('utf-8',errors='replace'))
            if 'resource' in fields and fields['resource'] != [RESOURCE]:
                return await JSONResponse({'error':'invalid_target'},status_code=400)(scope,receive,guarded)
            used = False
            async def replay():
                nonlocal used
                if not used:
                    used = True
                    return {'type':'http.request','body':data,'more_body':False}
                return await receive()
            return await self.app(scope,replay,guarded)
        await self.app(scope,receive,guarded)


def build_app(provider, tools, profile_loader=None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import Icon
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.transport_security import TransportSecuritySettings
    mcp = FastMCP('yandex-music',tools=tools,auth_server_provider=provider,
        icons=[Icon(src=ORIGIN+'/direct/logo.png',mimeType='image/png',sizes=['256x256'])],
        auth=AuthSettings(issuer_url=ORIGIN,resource_server_url=RESOURCE,required_scopes=['music'],
                          validate_token_resource=True,
                          client_registration_options=ClientRegistrationOptions(enabled=True,default_scopes=['music'],valid_scopes=['music']),
                          revocation_options=RevocationOptions(enabled=True)),
        host='127.0.0.1',port=18767,streamable_http_path='/direct/mcp',stateless_http=True,json_response=True,
        max_request_body_size=65536,log_level='WARNING',
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=['127.0.0.1:*','77-238-249-144.sslip.io','77-238-249-144.sslip.io:443'],
            allowed_origins=[ORIGIN,'https://chatgpt.com']))
    if profile_loader is not None:
        async def get_profile() -> Profile:
            from mcp.server.auth.middleware.auth_context import get_access_token
            token = get_access_token()
            if token is None or not hmac.compare_digest(str(token.subject),provider.owner_uid):
                raise FlowError('Профиль недоступен для этого подключения.')
            try:
                return await run_in_threadpool(profile_loader.load)
            except Exception:
                raise FlowError('Не удалось получить профиль владельца из Яндекса.') from None
        from mcp.types import ToolAnnotations
        mcp.add_tool(get_profile,name='get_profile',
            description='Return the authenticated Yandex profile. The stable opaque ID survives token refresh and reconnection; email and nickname are display metadata.',
            annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False,idempotentHint=True),
            meta={'openai/profile':True,'securitySchemes':[{'type':'oauth2','scopes':['music']}]},
            structured_output=True)
        mcp._tool_manager.get_tool('get_profile').parameters = {'type':'object','properties':{},'additionalProperties':False}
    app = mcp.streamable_http_app()
    from mcp.server.auth.routes import cors_middleware
    app.routes[:] = [route for route in app.routes if route.path != '/revoke']
    app.routes.append(Route('/revoke',cors_middleware(provider.revoke,['POST','OPTIONS']),methods=['POST','OPTIONS']))
    from mcp.server.auth.routes import build_metadata
    metadata = build_metadata(mcp.settings.auth.issuer_url,None,mcp.settings.auth.client_registration_options,mcp.settings.auth.revocation_options).model_dump(mode='json',exclude_none=True)
    metadata['authorization_response_iss_parameter_supported'] = True
    metadata['token_endpoint_auth_methods_supported'] = ['client_secret_post','client_secret_basic','none']
    async def metadata_handler(request):
        return JSONResponse(metadata)
    app.routes[:] = [route for route in app.routes if route.path != '/.well-known/oauth-authorization-server']
    app.routes.append(Route('/.well-known/oauth-authorization-server',cors_middleware(metadata_handler,['GET','OPTIONS']),methods=['GET','OPTIONS']))
    app.routes.extend([Route('/owner/login',provider.login,methods=['GET']),Route('/owner/action',provider.action,methods=['POST'])])
    async def logo(request):
        return FileResponse(Path(__file__).with_name('yandex-original-logo.png'),media_type='image/png')
    app.routes.append(Route('/direct/logo.png',logo,methods=['GET']))
    return SecurityHeaders(app),mcp


def main():
    os.umask(0o077)
    import runtime
    import uvicorn
    from yandex_music import Client
    owner_uid = Client(runtime.exact_token()).init().me.account.uid
    provider = Provider(Path('/var/lib/yandex-music-mcp/direct-oauth.sqlite3'),owner_uid)
    app,_ = build_app(provider,runtime.upstream.mcp._tool_manager.list_tools(),YandexProfile(owner_uid,runtime.exact_token))
    uvicorn.run(app,host='127.0.0.1',port=18767,log_level='warning',access_log=False)


if __name__ == '__main__':
    main()
