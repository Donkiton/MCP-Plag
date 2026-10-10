"""No credentials, mailbox login, sending or server changes. Run on the VPS."""
import json, socket, ssl, sys
result={}
for host,port in [('imap.mail.ru',993),('smtp.mail.ru',465)]:
    try:
        with socket.create_connection((host,port),timeout=8) as raw:
            with ssl.create_default_context().wrap_socket(raw,server_hostname=host) as conn:
                result[host]={'tls_verified':True,'port':port,'banner_received':bool(conn.recv(2048))}
    except OSError as e:
        result[host]={'tls_verified':False,'port':port,'error_type':type(e).__name__}
print(json.dumps(result,indent=2))
sys.exit(0 if all(v['tls_verified'] for v in result.values()) else 1)
