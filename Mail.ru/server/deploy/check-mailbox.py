"""Future read-only mailbox acceptance. Read credentials through systemd's environment.
Print no message content, password or raw server exceptions. Never send a message.
"""
import json, smtplib, ssl, sys
sys.path.insert(0,'/opt/mailru-mcp')
from mailru_mcp.mailbox import Mailbox
report={}
mail=Mailbox()
try:
    folders=mail.folders()
    report['imap_login']=True
    report['folder_count']=len(folders)
    report['drafts_detected']=any('\\Drafts' in f['flags'] for f in folders)
    report['trash_detected']=any('\\Trash' in f['flags'] for f in folders)
    report['recent_header_count']=len(mail.recent('INBOX',1))
    report['safe_move_capability']=mail._with_imap(lambda c:any((v.decode() if isinstance(v,bytes) else str(v)).upper() in ('MOVE','UIDPLUS') for v in c.capabilities))
except Exception as e:
    report['imap_login']=False;report['imap_error_type']=type(e).__name__
try:
    with smtplib.SMTP_SSL('smtp.mail.ru',465,context=ssl.create_default_context(),timeout=10) as smtp:
        smtp.login(mail.username,mail.password)
        report['smtp_login']=True
except Exception as e:
    report['smtp_login']=False;report['smtp_error_type']=type(e).__name__
print(json.dumps(report,indent=2))
raise SystemExit(0 if report.get('imap_login') and report.get('smtp_login') else 1)
