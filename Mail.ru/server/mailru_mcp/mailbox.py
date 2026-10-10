"""Mail.ru IMAP/SMTP backend. Credentials are read only from environment."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import time
import email
import email.policy
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import getaddresses, formatdate, make_msgid
from html.parser import HTMLParser
import imaplib
import io
import os
import re
import smtplib
import ssl
import zipfile
from xml.etree import ElementTree as ET


class MailError(Exception):
    pass


class StripHtml(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.muted = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.muted += 1
        if tag in ('br', 'p', 'div', 'li'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.muted = max(0, self.muted-1)
    def handle_data(self, data):
        if not self.muted: self.parts.append(data)


def _decode(v):
    if not v: return ''
    try: return str(make_header(decode_header(str(v))))
    except Exception: return str(v)


def _text(body):
    if isinstance(body, str): return body
    for enc in ('utf-8', 'windows-1251', 'latin-1'):
        try: return body.decode(enc)
        except UnicodeDecodeError: continue
    return body.decode('utf-8', errors='replace')


def _preview(msg, limit=12000):
    if msg.is_multipart():
        parts = list(msg.walk())
    else: parts = [msg]
    for wanted in ('text/plain', 'text/html'):
        for p in parts:
            if p.get_content_type() == wanted and p.get_content_disposition() != 'attachment':
                val = p.get_payload(decode=True) or b''
                charset = p.get_content_charset() or 'utf-8'
                try: txt = val.decode(charset, errors='replace')
                except LookupError: txt = _text(val)
                if wanted == 'text/html':
                    h = StripHtml(); h.feed(txt); txt = ' '.join(h.parts)
                return txt[:limit]
    return ''


def _safe_address(value):
    if not isinstance(value, str) or '\r' in value or '\n' in value: raise MailError('Invalid email address')
    items = getaddresses([value]); valid = []
    for name, addr in items:
        if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', addr): raise MailError('Invalid email address')
        valid.append(addr)
    if not valid: raise MailError('Email address required')
    return valid


def _new_message(to, subject, body, cc=None, sender=None):
    recips = _safe_address(to)
    cc_recips = _safe_address(cc) if cc else []
    if len(subject) > 500 or '\r' in subject or '\n' in subject: raise MailError('Invalid subject')
    if len(body) > 200000: raise MailError('Message exceeds 200000 characters')
    msg = EmailMessage()
    sender = sender or os.environ['MAILRU_EMAIL']
    msg['From'] = sender
    msg['To'] = ', '.join(recips)
    if cc_recips: msg['Cc'] = ', '.join(cc_recips)
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=False)
    msg['Message-ID'] = make_msgid(domain=sender.split('@')[-1])
    msg.set_content(body)
    return msg, recips + cc_recips


def _folder_arg(folder):
    if not isinstance(folder,str) or not folder or len(folder)>255 or any(x in folder for x in ('\r','\n','\0')):
        raise MailError('Invalid folder')
    if not folder.isascii(): raise MailError('Use the exact wire folder value returned by list_folders')
    return '"'+folder.replace('\\','\\\\').replace('"','\\"')+'"'


def _display_folder(name):
    def decode(match):
        chunk=match.group(1)
        if not chunk: return '&'
        try: return base64.b64decode(chunk.replace(',','/')+'='*((-len(chunk))%4)).decode('utf-16-be')
        except Exception: return match.group(0)
    return re.sub(r'&([^-]*)-',decode,name)


def _folder_label(name):
    # Exact Mail.ru smart-folder paths have localized labels in the web UI.
    # Keep the UTF-7 codec separate and never translate similarly named custom folders.
    labels={'INBOX':'Входящие','INBOX/ToMyself':'Письма себе','INBOX/Receipts':'Чеки'}
    return labels.get(name,_display_folder(name))


def _encode_folder(name):
    """Encode a human-readable folder name using IMAP modified UTF-7."""
    if (not isinstance(name,str) or not name.strip() or name!=name.strip()
            or len(name)>255 or any(ord(ch)<32 or ord(ch)==127 or 0xd800<=ord(ch)<=0xdfff for ch in name)):
        raise MailError('Invalid folder name')
    def encode(match):
        return '&'+base64.b64encode(match.group().encode('utf-16-be')).decode().rstrip('=').replace('/',',')+'-'
    wire=re.sub(r'[^\x20-\x7e]+',encode,name.replace('&','&-'))
    _folder_arg(wire)
    return wire


class Mailbox:
    def __init__(self, username=None, password=None):
        self.username = username if username is not None else os.environ['MAILRU_EMAIL']
        self.password = password if password is not None else os.environ['MAILRU_APP_PASSWORD']
        self.ssl_context = ssl.create_default_context()

    def _connect(self, timeout=20):
        conn = imaplib.IMAP4_SSL('imap.mail.ru', 993, ssl_context=self.ssl_context, timeout=timeout)
        try: conn.login(self.username, self.password)
        except Exception:
            try: conn.shutdown()
            except OSError: pass
            raise
        return conn

    def _with_imap(self, f):
        conn = self._connect()
        try: return f(conn)
        finally:
            try: conn.logout()
            except Exception: pass

    def _with_readonly(self, f):
        # Bound each command and the entire page, including connection setup.
        deadline=time.monotonic()+20
        conn=self._connect(timeout=8)
        try: return f(conn,deadline)
        finally:
            # LOGOUT is another network round trip and can itself time out.
            try: conn.shutdown()
            except OSError: pass

    @staticmethod
    def _read_budget(conn,deadline):
        remaining=deadline-time.monotonic()
        if remaining<=0: raise TimeoutError('Read page time budget exhausted')
        conn.sock.settimeout(min(8,remaining))

    @staticmethod
    def _uid_validity(conn):
        values=conn.response('UIDVALIDITY')[1] or [b'']
        value=values[0].decode('ascii')
        if not re.fullmatch(r'[1-9][0-9]*',value): raise MailError('UIDVALIDITY unavailable; refresh folder')
        return value

    def _search_cursor(self,query,folder,state=None,cursor=''):
        # Account/query binding prevents continuing in a different mailbox or query.
        binding=hashlib.sha256(json.dumps([self.username,query,folder],ensure_ascii=False).encode()).hexdigest()
        key=hashlib.sha256(('mail-search-cursor:'+self.password).encode()).digest()
        if state is not None:
            raw=json.dumps({'v':1,'binding':binding,**state},separators=(',',':')).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+hmac.new(key,raw,hashlib.sha256).hexdigest()
        try:
            encoded,signature=cursor.split('.')
            if len(cursor)>65536: raise ValueError()
            raw=base64.b64decode(encoded+'='*(-len(encoded)%4),altchars=b'-_',validate=True)
            if not hmac.compare_digest(signature,hmac.new(key,raw,hashlib.sha256).hexdigest()): raise ValueError()
            value=json.loads(raw)
            if value['v']!=1 or value['binding']!=binding: raise ValueError()
            paths=value['paths']; index=value['index']; before=value['before']
            if (not isinstance(paths,list) or not 1<=len(paths)<=100
                    or type(index)!=int or not 0<=index<len(paths)
                    or not isinstance(before,str) or (before and not re.fullmatch(r'[1-9][0-9]{0,19}',before))): raise ValueError()
            for path in paths: _folder_arg(path)
            return value
        except (ValueError,KeyError,TypeError):
            raise MailError('Invalid search cursor for this account/query; start a fresh search') from None

    def _fetch_headers(self,conn,folder,uids):
        if not uids: return []
        typ,rows=conn.uid('FETCH',','.join(uids),'(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])')
        if typ!='OK': raise MailError('Header batch fetch failed')
        messages={}; requested=set(uids)
        for row in rows or []:
            if not isinstance(row,tuple) or not isinstance(row[0],bytes) or not isinstance(row[1],bytes): continue
            match=re.search(rb'\bUID\s+([1-9][0-9]*)\b',row[0])
            if not match: raise MailError('Header response missing UID; refresh folder')
            uid=match[1].decode('ascii')
            if uid in requested:
                messages[uid]=self._meta(folder,uid,email.message_from_bytes(row[1],policy=email.policy.default))
        return [messages[uid] for uid in uids if uid in messages]

    @staticmethod
    def _search_uids(conn,query,before='',deadline=None):
        if before=='1': return []
        criteria=['CHARSET','UTF-8']
        if before: criteria+=['UID','1:'+str(int(before)-1)]
        # Mail.ru TEXT omits some HTML-only header matches. Include explicit
        # header searches rather than assuming TEXT implements every header.
        if query.isascii() and not any(ch in query for ch in ('\r','\n','\0')):
            quoted=b'"'+query.encode().replace(b'\\',b'\\\\').replace(b'"',b'\\"')+b'"'
            searches=[('OR','SUBJECT',quoted,'OR','FROM',quoted,'OR','TO',quoted,'TEXT',quoted)]
        else:searches=[(criterion,) for criterion in ('TEXT','SUBJECT','FROM','TO')]
        found=set()
        for search in searches:
            if deadline is not None:Mailbox._read_budget(conn,deadline)
            # UTF-8/non-ASCII uses a literal, preserving all quote/backslash characters.
            if len(search)==1:conn.literal=query.encode('utf-8')
            typ,rows=conn.uid('SEARCH',*criteria,*search)
            if typ!='OK' or not rows or not isinstance(rows[0],bytes):
                raise MailError('Server-side UTF-8 search failed; no complete search result is available')
            uids=[uid.decode('ascii') for uid in rows[0].split()]
            if any(not re.fullmatch(r'[1-9][0-9]{0,19}',uid) for uid in uids) or len(set(uids))!=len(uids):
                raise MailError('Invalid search UID response')
            found.update(uids)
        return sorted(found,key=int,reverse=True)

    def _folders(self, conn):
        # IMAP modified UTF-7 handling belongs to server; use IMAP LIST raw name for round trip.
        typ, rows = conn.list()
        if typ != 'OK': raise MailError('Could not list folders')
        out = []
        for item in rows or []:
            s = item.decode('utf-8', 'replace') if isinstance(item, bytes) else str(item)
            m = re.match(r'\((.*?)\)\s+("[^"]*"|NIL)\s+(.+)$', s)
            if not m: continue
            flags, delimiter, raw = m.groups()
            name = raw.strip()
            if name.startswith('"') and name.endswith('"'):
                name=re.sub(r'\\([\\"])',r'\1',name[1:-1])
            out.append({'folder': name, 'display_name':_folder_label(name), 'flags': flags,
                        'delimiter':None if delimiter=='NIL' else delimiter[1:-1]})
        return out

    def folders(self): return self._with_imap(lambda c: self._folders(c))

    @staticmethod
    def _system_kind(item):
        flags=set(item['flags'].casefold().split())
        for flag,kind in [('\\trash','trash'),('\\junk','spam'),('\\spam','spam'),
                          ('\\sent','sent'),('\\drafts','drafts'),('\\archive','archive'),('\\all','all'),
                          ('\\flagged','flagged'),('\\important','important')]:
            if flag in flags:return kind
        # Mail.ru may omit SPECIAL-USE flags. Match whole names, never substrings.
        name=item['display_name'].casefold()
        for kind,names in {'inbox':{'inbox','входящие'},'trash':{'trash','корзина','deleted messages'},
                           'spam':{'spam','junk','спам','junk e-mail'},'sent':{'sent','sent messages','отправленные'},
                           'drafts':{'drafts','черновики'},'archive':{'archive','архив'},'all':{'all mail','вся почта'}}.items():
            if name in names:return kind
        return None

    def _custom_folder(self,folders,folder):
        _folder_arg(folder)
        item=next((item for item in folders if item['folder']==folder),None)
        if not item:raise MailError('Folder does not exist; refresh list_folders')
        if self._system_kind(item):raise MailError('System folders cannot be renamed or deleted')
        delimiter=item.get('delimiter')
        if delimiter and any(other['folder'].startswith(folder+delimiter) for other in folders):
            raise MailError('Folder has subfolders; manage the subfolders separately first')
        return item

    def _new_folder_name(self,folders,name,parent=''):
        wire=_encode_folder(name)
        delimiters={item.get('delimiter') for item in folders if item.get('delimiter')}
        if any(delimiter in name for delimiter in delimiters | {'/'}):
            raise MailError('Use a single folder name without hierarchy separators')
        if self._system_kind({'flags':'','display_name':name}):raise MailError('Reserved system folder name')
        wire=parent+wire
        _folder_arg(wire)
        if any(item['folder'].casefold()==wire.casefold() for item in folders):raise MailError('Folder already exists')
        return wire

    def create_folder(self,name):
        def run(c):
            wire=self._new_folder_name(self._folders(c),name)
            if c.create(_folder_arg(wire))[0]!='OK':raise MailError('CREATE failed; refresh list_folders before retrying')
            return {'status':'folder_created','folder':wire,'display_name':name}
        return self._with_imap(run)

    def rename_folder(self,folder,name):
        def run(c):
            folders=self._folders(c)
            current=self._custom_folder(folders,folder)
            delimiter=current.get('delimiter')
            parent=folder.rsplit(delimiter,1)[0]+delimiter if delimiter and delimiter in folder else ''
            wire=self._new_folder_name(folders,name,parent)
            if c.rename(_folder_arg(folder),_folder_arg(wire))[0]!='OK':
                raise MailError('RENAME failed; refresh list_folders before retrying')
            return {'status':'folder_renamed','old_folder':folder,'folder':wire,'display_name':_display_folder(wire)}
        return self._with_imap(run)

    @staticmethod
    def _all_uids(c):
        typ,rows=c.uid('SEARCH',None,'ALL')
        if typ!='OK' or not rows or not isinstance(rows[0],bytes):raise MailError('Could not inspect folder contents')
        uids=[uid.decode('ascii') for uid in rows[0].split()]
        if any(not re.fullmatch(r'[0-9]{1,20}',uid) for uid in uids) or len(set(uids))!=len(uids):
            raise MailError('Invalid UID list returned by server')
        return uids

    def delete_folder(self,folder):
        def run(c):
            self._custom_folder(self._folders(c),folder)
            self._select(c,folder)
            if self._all_uids(c):raise MailError('Folder is not empty. Move its messages to Trash or another folder before deleting it.')
            if c.delete(_folder_arg(folder))[0]!='OK':raise MailError('DELETE failed; refresh list_folders before retrying')
            return {'status':'folder_deleted','folder':folder,'messages_deleted':0}
        return self._with_imap(run)

    @staticmethod
    def _purge_selected(c,uids):
        """Permanently remove only the captured UIDs, never newly arriving mail."""
        capabilities={value.decode().upper() if isinstance(value,bytes) else str(value).upper() for value in c.capabilities}
        if 'UIDPLUS' not in capabilities:raise MailError('Server lacks UIDPLUS; refusing unsafe mailbox-wide EXPUNGE')
        removed=0
        for start in range(0,len(uids),500):
            chunk=uids[start:start+500];uid_set=','.join(chunk)
            if c.uid('STORE',uid_set,'+FLAGS.SILENT','(\\Deleted)')[0]!='OK':
                raise MailError(f'Cleanup stopped after {removed} captured UIDs; marking failed. Inspect folder before another explicit request.')
            if c.uid('EXPUNGE',uid_set)[0]!='OK':
                raise MailError(f'Cleanup stopped after {removed} captured UIDs; expunge result uncertain. Do not retry automatically; inspect folder.')
            removed+=len(chunk)
        return removed

    def empty_folder(self,folder):
        def run(c):
            item=next((item for item in self._folders(c) if item['folder']==folder),None)
            if not item or self._system_kind(item) not in {'trash','spam'}:
                raise MailError('Only the actual Trash or Spam folder may be emptied')
            self._select(c,folder,readonly=False)
            uids=self._all_uids(c)
            if len(uids)>100000:raise MailError('Folder exceeds the 100000-message cleanup limit; no changes made')
            removed=self._purge_selected(c,uids) if uids else 0
            try:remaining=len(self._all_uids(c))
            except MailError:remaining=None
            return {'status':'folder_emptied','folder':folder,'kind':self._system_kind(item),
                    'captured_count':len(uids),'removed_count':removed,'remaining_count':remaining,
                    'recoverable':False,'note':'Captured messages were permanently removed. New arrivals after the snapshot are retained. Do not retry automatically.'}
        return self._with_imap(run)

    def delete_message_permanently(self,folder,uid,expected_identity=None):
        if not isinstance(uid,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',uid):
            raise MailError('Provide exactly one individual positive UID')
        _folder_arg(folder)
        def run(c):
            self._select(c,folder,readonly=False)
            headers=self._fetch(c,uid,headers_only=True)
            self._check_identity(c,headers,expected_identity)
            self._purge_selected(c,[uid])
            typ,rows=c.uid('SEARCH',None,'UID',uid)
            verified=(typ=='OK' and rows and isinstance(rows[0],bytes) and uid.encode() not in rows[0].split())
            return {'status':'permanently_deleted' if verified else 'deletion_unverified',
                    'folder':folder,'uid':uid,'recoverable':False,
                    'note':'Only this UID was targeted. Never retry automatically; inspect current state if deletion is unverified.'}
        return self._with_imap(run)

    @staticmethod
    def _select(conn, folder, readonly=True):
        typ, _ = conn.select(_folder_arg(folder), readonly=readonly)
        if typ != 'OK': raise MailError('Folder inaccessible')

    @staticmethod
    def _uid_rows(conn, limit=20, query=None):
        if query and query.isascii():
            # ASCII search can narrow candidate messages efficiently.
            q = query.replace('"', '').replace('\\', '')[:100]
            typ, dat = conn.uid('SEARCH', None, 'TEXT', '"'+q+'"')
        else:
            typ, dat = conn.uid('SEARCH', None, 'ALL')
        if typ != 'OK': raise MailError('IMAP SEARCH failed')
        return [x.decode('ascii') for x in (dat[0] or b'').split()][-limit:][::-1]

    @staticmethod
    def _fetch(conn, uid, headers_only=False, search_preview=False):
        if not re.fullmatch(r'[0-9]{1,20}', str(uid)): raise MailError('Invalid UID')
        if not headers_only and not search_preview:
            typ,size_rows=conn.uid('FETCH',str(uid),'(RFC822.SIZE)')
            if typ!='OK': raise MailError('Could not inspect message size')
            sizes=re.findall(rb'RFC822.SIZE\s+(\d+)',b' '.join(v if isinstance(v,bytes) else v[0] for v in size_rows if isinstance(v,(bytes,tuple))))
            if not sizes: raise MailError('Message not found or size metadata unavailable; refresh the folder and UID')
            if int(sizes[0])>12_000_000: raise MailError('Message exceeds 12 MB download limit; headers remain available')
        selector='(BODY.PEEK[HEADER])' if headers_only else '(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.65536>)' if search_preview else '(BODY.PEEK[])'
        typ, data = conn.uid('FETCH', str(uid), selector)
        if typ != 'OK': raise MailError('Fetch failed')
        literals=[v[1] for v in data if isinstance(v,tuple) and isinstance(v[1],bytes)]
        raw=b''.join(literals)
        if not raw: raise MailError('Message not found')
        return email.message_from_bytes(raw, policy=email.policy.default)

    @staticmethod
    def _meta(folder, uid, msg):
        return {'folder': folder, 'folder_display_name':_folder_label(folder), 'uid': str(uid), 'from': _decode(msg.get('From')),
                'to': _decode(msg.get('To')), 'subject': _decode(msg.get('Subject')),
                'date': str(msg.get('Date', '')), 'message_id': str(msg.get('Message-ID',''))}

    def recent(self, folder='INBOX', limit=20, before_uid='', uid_validity='', paged=False):
        limit = max(1, min(int(limit), 50))
        if before_uid and not re.fullmatch(r'[1-9][0-9]{0,19}',before_uid): raise MailError('Invalid before_uid')
        def run(c,deadline):
            self._read_budget(c,deadline)
            self._select(c, folder)
            validity=self._uid_validity(c)
            if uid_validity and validity!=uid_validity: raise MailError('Folder UIDVALIDITY changed; restart listing')
            self._read_budget(c,deadline)
            all_uids=sorted(self._all_uids(c),key=int,reverse=True)
            candidates=[uid for uid in all_uids if not before_uid or int(uid)<int(before_uid)]
            selected=candidates[:limit]
            self._read_budget(c,deadline)
            out=self._fetch_headers(c,folder,selected)
            more=len(candidates)>len(selected)
            return {'items':out,'folder':folder,'folder_display_name':_folder_label(folder),'total_count':len(all_uids),'returned_count':len(out),
                    'has_more':more,'next_before_uid':selected[-1] if more else None,'uid_validity':validity,
                    'incomplete':len(out)!=len(selected),'missing_count':len(selected)-len(out),
                    'note':'Continue with next_before_uid and uid_validity until has_more is false. Missing headers indicate messages changed during reading.'} if paged else out
        return self._with_readonly(run)

    def search(self, query, folder='', limit=20, scan_per_folder=300, cursor=''):
        if not query or len(query) > 200: raise MailError('Search query required, max 200 chars')
        limit = max(1, min(int(limit), 50))
        resumed=self._search_cursor(query,folder,cursor=cursor) if cursor else None
        def run(c,deadline):
            self._read_budget(c,deadline)
            paths=resumed['paths'] if resumed else ([folder] if folder else [x['folder'] for x in self._folders(c) if '\\noselect' not in x['flags'].casefold().split()])
            omitted=resumed.get('omitted',0) if resumed else max(0,len(paths)-100)
            paths=paths[:100]; index=resumed['index'] if resumed else 0
            before=resumed['before'] if resumed else ''; validity=resumed.get('validity','') if resumed else ''
            out=[]; missing=0; matches=0; completed=0; interrupted=False
            while index<len(paths):
                path=paths[index]
                try:
                    self._read_budget(c,deadline); self._select(c,path)
                    current_validity=self._uid_validity(c)
                    if validity and current_validity!=validity: raise MailError('Folder UIDVALIDITY changed; restart search')
                    validity=current_validity
                    self._read_budget(c,deadline); uids=self._search_uids(c,query,before,deadline)
                    selected=uids[:limit-len(out)]
                    self._read_budget(c,deadline); page=self._fetch_headers(c,path,selected)
                    out+=page; missing+=len(selected)-len(page); matches+=len(selected)
                    if len(uids)>len(selected):
                        before=selected[-1]
                        break
                    index+=1; completed+=1; before=''; validity=''
                    if len(out)>=limit: break
                except (TimeoutError,OSError,imaplib.IMAP4.abort):
                    # Preserve position, never turn an interrupted search into "no matches".
                    interrupted=True; break
            next_cursor=self._search_cursor(query,folder,state={'paths':paths,'index':index,'before':before,'validity':validity,'omitted':omitted}) if index<len(paths) else None
            return {'messages':out,'returned_count':len(out),'limit_reached':len(out)>=limit,
                    'matched_uids_processed':matches,'folders_completed_this_page':completed,'folder_count':len(paths),
                    'has_more':bool(next_cursor),'next_cursor':next_cursor,
                    'incomplete':bool(next_cursor) or bool(omitted) or bool(missing),
                    'interrupted':interrupted,'missing_count':missing,'omitted_folder_count':omitted,
                    'note':'Server-side UTF-8 TEXT search covers all messages in each searched folder. Continue with the same query/folder and next_cursor until has_more is false. An incomplete result must never be reported as no matches or a complete mailbox search.'}
        return self._with_readonly(run)

    def message_identity(self, folder, uid):
        """Identify an IMAP message for moving without downloading its body."""
        def run(c):
            self._select(c,folder)
            headers=self._fetch(c,uid,headers_only=True)
            return {'uid_validity':str((c.response('UIDVALIDITY')[1] or [b''])[0].decode('ascii')),
                    'message_fingerprint':hashlib.sha256(headers.as_bytes()).hexdigest()}
        return self._with_imap(run)

    def read(self, folder, uid):
        def run(c):
            self._select(c,folder)
            m=self._fetch(c,uid)
            files=[]
            for i,p in enumerate(m.walk()):
                if p.get_content_disposition() == 'attachment' or p.get_filename():
                    blob=p.get_payload(decode=True) or b''
                    files.append({'index':i,'filename':_decode(p.get_filename() or 'attachment'),
                                  'content_type':p.get_content_type(),'size':len(blob)})
            return {**self._meta(folder,uid,m), 'body':_preview(m), 'attachments':files,
                    'uid_validity':str((c.response('UIDVALIDITY')[1] or [b''])[0].decode('ascii')),
                    'message_fingerprint':hashlib.sha256(m.as_bytes()).hexdigest(),
                    'body_truncated_at':12000}
        return self._with_imap(run)

    def attachment(self, folder, uid, index):
        def run(c):
            self._select(c,folder); msg=self._fetch(c,uid)
            parts=list(msg.walk()); n=int(index)
            if n<0 or n>=len(parts): raise MailError('Invalid attachment index')
            p=parts[n]
            if not (p.get_content_disposition()=='attachment' or p.get_filename()):
                raise MailError('Part is not an attachment')
            blob=p.get_payload(decode=True) or b''
            if len(blob)>2_000_000: raise MailError('Attachment exceeds 2 MB limit')
            result={'filename':_decode(p.get_filename() or 'attachment'), 'size':len(blob),
                    'content_type':p.get_content_type()}
            kind=p.get_content_type()
            if kind.startswith('text/') or kind in ('application/json','application/xml'):
                result['text']=_text(blob)[:60000]
            elif kind=='application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                try:
                    with zipfile.ZipFile(io.BytesIO(blob)) as z:
                        entry=z.getinfo('word/document.xml')
                        if entry.file_size>2_000_000: raise MailError('DOCX decompressed XML exceeds 2 MB')
                        doc=ET.fromstring(z.read(entry))
                    result['text']=' '.join(t.text for t in doc.iter() if t.tag.endswith('}t') and t.text)[:60000]
                except MailError: raise
                except Exception: result['base64']=base64.b64encode(blob).decode()
            elif kind=='application/pdf':
                try:
                    from pypdf import PdfReader
                    reader=PdfReader(io.BytesIO(blob))
                    result['text']='\n'.join(page.extract_text() or '' for page in reader.pages)[:60000]
                except Exception:
                    result['note']='PDF text extraction unavailable; base64 content provided'
                    result['base64']=base64.b64encode(blob).decode()
            else: result['base64']=base64.b64encode(blob).decode()
            return result
        return self._with_imap(run)

    def create_draft(self, to, subject, body, cc=''):
        msg,_ = _new_message(to,subject,body,cc,sender=self.username)
        def run(c):
            folders=self._folders(c)
            drafts=next((x['folder'] for x in folders if '\\Drafts' in x['flags']), None)
            if not drafts:
                drafts=next((x['folder'] for x in folders if any(k in x['folder'].casefold() for k in ('draft','чернов'))),None)
            if not drafts: raise MailError('Drafts folder not found; refusing to save elsewhere')
            typ,_=c.append(_folder_arg(drafts),'(\\Draft)',None,msg.as_bytes(policy=email.policy.SMTP))
            if typ!='OK': raise MailError('IMAP APPEND failed')
            return {'status':'draft_created','folder':drafts,'to':msg['To'],'subject':subject}
        return self._with_imap(run)

    def send(self,to,subject,body,cc=''):
        msg,recipients=_new_message(to,subject,body,cc,sender=self.username)
        with smtplib.SMTP_SSL('smtp.mail.ru',465,context=self.ssl_context,timeout=20) as s:
            s.login(self.username,self.password)
            refused=s.send_message(msg,from_addr=self.username,to_addrs=recipients)
        return {'status':'partially_sent' if refused else 'sent','accepted_recipients':[r for r in recipients if r not in refused],
                'refused_recipients':list(refused),'to':msg['To'],'cc':cc,'subject':subject,
                'note':'Do not automatically retry; SMTP may have accepted some recipients.'}

    @staticmethod
    def _move(c, folder, uid, target,expected_identity=None):
        if not re.fullmatch(r'[0-9]{1,20}',str(uid)): raise MailError('Invalid UID')
        if folder==target: raise MailError('Source and destination folders are the same')
        Mailbox._select(c,folder,readonly=False)
        if expected_identity is not None: Mailbox._check_identity(c,Mailbox._fetch(c,uid,headers_only=True),expected_identity)
        Mailbox._move_selected(c,str(uid),target)
        return {'status':'moved','folder':folder,'uid':str(uid),'destination':target}

    @staticmethod
    def _move_selected(c, uid_set, target):
        # UID MOVE is atomic. Fallback must not accidentally expunge unrelated messages.
        capabilities={v.decode().upper() if isinstance(v,bytes) else str(v).upper() for v in c.capabilities}
        if 'MOVE' in capabilities:
            typ,_=c.uid('MOVE',uid_set,_folder_arg(target))
            if typ!='OK': raise MailError('MOVE failed')
        elif 'UIDPLUS' in capabilities:
            typ,_=c.uid('COPY',uid_set,_folder_arg(target))
            if typ!='OK': raise MailError('COPY failed; source unchanged')
            typ,_=c.uid('STORE',uid_set,'+FLAGS.SILENT','(\\Deleted)')
            if typ!='OK': raise MailError('COPY succeeded but marking original failed')
            typ,_=c.uid('EXPUNGE',uid_set)
            if typ!='OK': raise MailError('COPY succeeded but UID EXPUNGE failed')
        else: raise MailError('Server does not support safe MOVE or UIDPLUS')

    @staticmethod
    def _check_identity(c,msg,expected):
        if expected is None: return
        actual={'uid_validity':str((c.response('UIDVALIDITY')[1] or [b''])[0].decode('ascii')),
                'message_fingerprint':hashlib.sha256(msg.as_bytes()).hexdigest()}
        if not actual['uid_validity'] or actual!=expected: raise MailError('Message or mailbox identity changed; prepare a new action')

    def move(self,folder,uid,target,expected_identity=None):
        def run(c):
            available={x['folder'] for x in self._folders(c)}
            if target not in available: raise MailError('Target folder does not exist')
            return self._move(c,folder,uid,target,expected_identity)
        return self._with_imap(run)

    def trash(self,folder,uid,expected_identity=None):
        def run(c):
            fs=self._folders(c)
            trash=next((x['folder'] for x in fs if '\\Trash' in x['flags']),None)
            if not trash:
                trash=next((x['folder'] for x in fs if any(k in x['folder'].casefold() for k in ('trash','корзин'))),None)
            if not trash: raise MailError('Trash folder not found; refusing permanent deletion')
            return self._move(c,folder,uid,trash,expected_identity)
        return self._with_imap(run)

    def trash_many(self,folder,uids,progress=None):
        if (not isinstance(uids,list) or not uids
                or any(not isinstance(uid,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',uid) for uid in uids)
                or len(set(uids))!=len(uids)):
            raise MailError('Provide unique individual positive UIDs')
        def run(c):
            trash=self._trash_target(c,folder)
            self._select(c,folder,readonly=False)
            if len(uids)<=50:
                # Retain the exact single-message preflight for existing small calls.
                for uid in uids:self._fetch(c,uid,headers_only=True)
            elif not set(uids).issubset(set(self._all_uids(c))):
                raise MailError('Some selected UIDs no longer exist; refresh before requesting a new action')
            result=self._trash_snapshot(c,folder,uids,trash,progress)
            if len(uids)<=50:result['uids']=uids
            return result
        return self._with_imap(run)

    def _trash_target(self,c,folder):
        folders=self._folders(c)
        trash=next((item['folder'] for item in folders if self._system_kind(item)=='trash'),None)
        if not trash:raise MailError('Trash folder not found; refusing permanent deletion')
        if folder==trash:raise MailError('Already in Trash; ordinary deletion never empties Trash')
        if folder not in {item['folder'] for item in folders}:raise MailError('Source folder not found')
        return trash

    @staticmethod
    def _uid_chunks(uids,max_bytes=4000,max_count=1000):
        """Compact exact contiguous UID ranges, never use * or bridge snapshot gaps."""
        values=sorted({int(uid) for uid in uids})
        ranges=[]
        for value in values:
            if ranges and value==ranges[-1][1]+1:ranges[-1][1]=value
            else:ranges.append([value,value])
        chunk=[];count=0;size=0
        bounded=[]
        for first,last in ranges:
            while first<=last:
                end=min(last,first+max_count-1);bounded.append((first,end));first=end+1
        for first,last in bounded:
            item=str(first) if first==last else f'{first}:{last}'
            if chunk and (size+len(item)+1>max_bytes or count+last-first+1>max_count):
                yield ','.join(chunk),count
                chunk=[];count=0;size=0
            chunk.append(item);count+=last-first+1;size+=len(item)+1
        if chunk:yield ','.join(chunk),count

    def _trash_snapshot(self,c,folder,uids,trash,progress=None):
        total=len(uids);moved=0
        report=lambda **extra:{'folder':folder,'destination':trash,'captured_count':total,
            'moved_count':moved,'recoverable':True,**extra}
        if progress:progress(report(phase='snapshot_captured'))
        for uid_set,count in self._uid_chunks(uids):
            if progress:progress(report(phase='moving',current_chunk_count=count))
            self._move_selected(c,uid_set,trash)
            moved+=count
            if progress:progress(report(phase='chunk_acknowledged',current_chunk_count=0))
        remaining=set(self._all_uids(c))
        still_present=len(remaining.intersection(uids))
        return report(status='moved' if not still_present else 'move_unverified',count=total-still_present,
            captured_remaining_count=still_present,remaining_count=len(remaining),
            note='Only the captured UIDs were targeted. Later arrivals are retained. No message bodies were downloaded. Never automatically retry an uncertain mutation.')

    def trash_all(self,folder,progress=None):
        def run(c):
            trash=self._trash_target(c,folder)
            self._select(c,folder,readonly=False)
            uids=self._all_uids(c)
            return self._trash_snapshot(c,folder,uids,trash,progress)
        return self._with_imap(run)

    def unsubscribe_options(self,folder,uid):
        from .unsubscribe import options
        def run(c):
            self._select(c,folder)
            msg=self._fetch(c,uid,headers_only=True)
            # Legacy newsletters sometimes expose only a link in the body.
            if not msg.get('List-Unsubscribe'):
                msg=self._fetch(c,uid)
            return options(msg,self._meta(folder,uid,msg))
        return self._with_imap(run)

    def unsubscribe_messages(self,messages,progress=None):
        from .unsubscribe import perform
        results=[];seen={}
        for ref in messages:
            try:
                info=self.unsubscribe_options(ref['folder'],ref['uid'])
                if info['dedupe_key'] in seen:
                    result={'folder':ref['folder'],'uid':ref['uid'],'sender':info['sender'],
                            'status':'duplicate','duplicate_of':seen[info['dedupe_key']]}
                else:
                    result=perform(info,self.send)
                    seen[info['dedupe_key']]=len(results)
            except Exception:
                result={'folder':ref['folder'],'uid':ref['uid'],'status':'failed_or_uncertain',
                        'note':'Could not complete this unsubscribe request. Do not retry automatically; inspect before another explicit request.'}
            results.append(result)
            if progress:progress({'phase':'unsubscribing','processed_count':len(results),'total_count':len(messages),'results':results})
        return {'status':'processed','processed_count':len(results),'results':results,
            'note':'Report the per-list result. Request accepted/sent is not proof that the sender removed the subscription. Other lists from the same brand may require their own representative messages.'}
