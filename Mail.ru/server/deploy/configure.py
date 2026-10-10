"""Future interactive configuration on the VPS only; never prints passwords."""
import getpass, os, pathlib, re, tempfile
if os.getuid()!=0: raise SystemExit('Run as root on the VPS')
target=pathlib.Path('/etc/mailru-mcp.env')
if target.exists() and target.stat().st_size: raise SystemExit('Configuration already populated; refusing to overwrite')
address=input('Full Mail.ru mailbox address: ').strip()
if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',address): raise SystemExit('Invalid email address')
app=getpass.getpass('Mail.ru external application password (hidden): ')
login=getpass.getpass('Independent MCP login/confirmation password, 32+ characters (hidden): ')
repeat=getpass.getpass('Repeat independent MCP password (hidden): ')
if len(login)<32 or login!=repeat or login==app or not app: raise SystemExit('Password requirements not met')
values={'PUBLIC_BASE_URL':'https://mailru-77-238-249-144.sslip.io','MAILRU_EMAIL':address,
        'MAILRU_APP_PASSWORD':app,'MCP_LOGIN_PASSWORD':login,'PORT':'8765','BIND_HOST':'127.0.0.1'}
def quote(value):
    if any(c in value for c in '\r\n\0'): raise SystemExit('Control characters are not allowed')
    return '"'+value.replace('\\','\\\\').replace('"','\\"')+'"'
os.umask(0o077)
fd,name=tempfile.mkstemp(prefix='.mailru-mcp-',dir='/etc')
try:
    with os.fdopen(fd,'w') as stream:
        stream.write('\n'.join(key+'='+quote(value) for key,value in values.items())+'\n')
        stream.flush();os.fsync(stream.fileno())
    os.chmod(name,0o600);os.replace(name,target)
finally:
    if os.path.exists(name): os.unlink(name)
print('Configuration stored as root:root 0600. No service was enabled or started.')
