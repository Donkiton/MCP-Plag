"""Prepare, but never apply, the one-line SNI change on the VPS."""
import pathlib
source=pathlib.Path('/etc/nginx/stream.d/openai-relay.conf')
destination=pathlib.Path('/tmp/mailru-openai-relay.candidate.conf')
old=source.read_text()
host='mailru-77-238-249-144.sslip.io'
anchor='    77-238-249-144.sslip.io 127.0.0.1:8443;'
if host in old: raise SystemExit('Mail.ru SNI already present; review manually')
if old.count(anchor)!=1: raise SystemExit('Existing SNI layout changed; review manually')
new=old.replace(anchor,anchor+'\n    '+host+' 127.0.0.1:8443;',1)
destination.write_text(new)
print('Prepared /tmp/mailru-openai-relay.candidate.conf; no live file modified. Review the diff before applying.')
