"""Mail-derived unsubscribe requests. Never send mailbox credentials to websites."""
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
from email.utils import parseaddr
from html.parser import HTMLParser
from urllib.parse import parse_qs,unquote,urlencode,urljoin,urlsplit
from .mailbox import MailError,StripHtml


def safe_target(url):
    if not isinstance(url,str) or len(url)>8192 or any(ord(c)<33 or ord(c)==127 for c in url):
        raise MailError('Invalid unsubscribe URL')
    try:
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError()
        host=parsed.hostname.encode('idna').decode('ascii')
        addresses={item[4][0] for item in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):raise ValueError()
        return parsed,host,sorted(addresses)[0]
    except (ValueError,UnicodeError,socket.gaierror):
        raise MailError('Unsubscribe URL must use public HTTPS; local/private destinations are not allowed') from None


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self,host,address):
        super().__init__(host,443,timeout=8,context=ssl.create_default_context())
        self.address=address
    def connect(self):
        # Pin the checked address so a DNS change cannot redirect into the VPS.
        raw=socket.create_connection((self.address,443),timeout=self.timeout)
        try:self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
        except Exception:raw.close();raise


def request(url,method='GET',body=None):
    parsed,host,address=safe_target(url)
    conn=PublicHTTPSConnection(host,address)
    try:
        headers={'User-Agent':'MailMCP-Unsubscribe/0.3.19','Accept':'text/html, application/json'}
        if body is not None:headers['Content-Type']='application/x-www-form-urlencoded'
        conn.request(method,(parsed.path or '/')+('?' + parsed.query if parsed.query else ''),body=body,headers=headers)
        response=conn.getresponse()
        payload=response.read(131073)
        if len(payload)>131072:raise MailError('Unsubscribe response exceeds the page size limit')
        charset=response.headers.get_content_charset() or 'utf-8'
        try:text=payload.decode(charset,errors='replace')
        except LookupError:text=payload.decode('utf-8',errors='replace')
        return response.status,response.getheader('Location'),text
    finally:conn.close()


class LinksAndForms(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.forms=[];self.anchor=None;self.form=None;self.button=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='a':self.anchor={'url':attrs.get('href',''),'text':''}
        if tag=='form':self.form={'action':attrs.get('action',''),'method':attrs.get('method','GET').upper(),'fields':[],'buttons':[],'unsafe':False}
        if self.form is not None:
            if tag=='input':
                kind=attrs.get('type','text').lower()
                if kind=='hidden' and attrs.get('name'):self.form['fields'].append((attrs['name'],attrs.get('value','')))
                elif kind=='submit':self.form['buttons'].append((attrs.get('value',''),attrs.get('name',''),attrs.get('value','')))
                else:self.form['unsafe']=True
            if tag in ('select','textarea','iframe'):self.form['unsafe']=True
            if tag=='button':
                if attrs.get('formaction') or attrs.get('formmethod'):self.form['unsafe']=True
                self.button={'text':'','name':attrs.get('name',''),'value':attrs.get('value',''),'submit':attrs.get('type','submit')=='submit'}
    def handle_data(self,data):
        if self.anchor is not None:self.anchor['text']+=data
        if self.button is not None:self.button['text']+=data
    def handle_endtag(self,tag):
        if tag=='a' and self.anchor is not None:self.links.append(self.anchor);self.anchor=None
        if tag=='button' and self.button is not None:
            if self.form is not None and self.button['submit']:self.form['buttons'].append((self.button['text'],self.button['name'],self.button['value']))
            self.button=None
        if tag=='form' and self.form is not None:self.forms.append(self.form);self.form=None


def one_click_authenticated(msg):
    # Trust only the top Mail.ru receiver's authentication result, never an arbitrary injected one.
    results=msg.get_all('Authentication-Results',[])
    if not results or not re.match(r'^\s*mxs\.mail\.ru\s*;',str(results[0]),re.I):return False
    authenticated={d.lower() for d in re.findall(r'\bdkim=pass\s+header\.d=([a-z0-9.-]+)',str(results[0]),re.I)}
    for signature in msg.get_all('DKIM-Signature',[]):
        tags={key.lower():value.strip() for key,value in re.findall(r'(?:^|;)\s*([a-z]+)\s*=\s*([^;]+)',str(signature),re.I)}
        signed={header.strip().lower() for header in tags.get('h','').split(':')}
        if tags.get('d','').lower() in authenticated and {'list-unsubscribe','list-unsubscribe-post'}.issubset(signed):return True
    return False


def options(msg,meta):
    from .mailbox import _decode
    sender=parseaddr(_decode(msg.get('From')))[1].casefold()
    list_id=str(msg.get('List-ID','')).strip()
    urls=re.findall(r'<([^<>]+)>',str(msg.get('List-Unsubscribe','')))
    methods=[]
    one_click=(len(msg.get_all('List-Unsubscribe',[]))==1 and len(msg.get_all('List-Unsubscribe-Post',[]))==1
        and str(msg.get('List-Unsubscribe-Post','')).strip().casefold()=='list-unsubscribe=one-click'
        and one_click_authenticated(msg))
    for url in urls:
        if url.lower().startswith('https://'):methods.append({'method':'one_click' if one_click else 'web','url':url,'source':'List-Unsubscribe'})
        elif url.lower().startswith('mailto:'):methods.append({'method':'mailto','url':url,'source':'List-Unsubscribe'})
    if not methods:
        for part in msg.walk():
            if part.get_content_disposition()=='attachment':continue
            if part.get_content_type()=='text/html':
                payload=part.get_payload(decode=True) or b''
                try:text=payload.decode(part.get_content_charset() or 'utf-8',errors='replace')
                except LookupError:text=payload.decode('utf-8',errors='replace')
                parser=LinksAndForms();parser.feed(text[:200000])
                for link in parser.links:
                    if re.search(r'\bunsubscribe\b|отписаться|отписк',link['text'],re.I) and link['url'].startswith('https://'):
                        methods.append({'method':'web','url':link['url'],'source':'message_body'})
            elif part.get_content_type()=='text/plain':
                payload=part.get_payload(decode=True) or b''
                text=payload.decode('utf-8',errors='replace')
                for line in text.splitlines():
                    if re.search(r'\bunsubscribe\b|отписаться|отписк',line,re.I):
                        for url in re.findall(r'https://[^\s<>]+',line):methods.append({'method':'web','url':url.rstrip('.,)'), 'source':'message_body'})
    # Prefer RFC 8058, then a request email, then an interactive URL.
    methods.sort(key=lambda value:{'one_click':0,'mailto':1,'web':2}[value['method']])
    unique=[]
    for method in methods:
        if method not in unique and len(method['url'])<=8192:unique.append(method)
    key=hashlib.sha256((sender+'\n'+(list_id or '\n'.join(method['url'] for method in unique))).encode()).hexdigest()
    return {'folder':meta['folder'],'uid':meta['uid'],'sender':sender,'subject':meta['subject'],'list_id':list_id,
        'methods':unique,'dedupe_key':key,'one_click_authenticated':one_click,
        'note':'Select one representative email for each requested mailing list. URLs are only used after an explicit unsubscribe request. Reading these options makes no HTTP request or SMTP send.'}


def confirmed(text):
    parser=StripHtml();parser.feed(text)
    visible=' '.join(parser.parts)
    return bool(re.search(r'you (?:have been|are|were) (?:successfully )?unsubscribed|successfully unsubscribed|you(?:\x27|’)ve been unsubscribed|вы (?:успешно )?отписались|вы (?:были )?(?:успешно )?отписаны|подписка отменена',visible,re.I))


def web_unsubscribe(url):
    current=url
    for _ in range(4):
        status,location,text=request(current)
        if status in (301,302,303,307,308) and location:
            current=urljoin(current,location);continue
        break
    else:return {'status':'manual_confirmation_required','url':current,'note':'Too many redirects'}
    if status//100!=2:return {'status':'failed','http_status':status,'note':'Sender did not accept the unsubscribe page request'}
    if confirmed(text):return {'status':'unsubscribed','method':'web'}
    parser=LinksAndForms();parser.feed(text)
    safe=[]
    for form in parser.forms:
        target=urljoin(current,form['action'])
        if form['unsafe'] or form['method']!='POST' or urlsplit(target).netloc!=urlsplit(current).netloc:continue
        buttons=[button for button in form['buttons'] if re.fullmatch(r'unsubscribe(?: me| from (?:this|the) list)?|отписаться(?: от рассылки)?',button[0].strip(),re.I)]
        if len(buttons)!=1 or len(form['fields'])>50:continue
        label,name,value=buttons[0]
        fields=form['fields']+([(name,value)] if name else [])
        body=urlencode(fields).encode()
        if len(body)<=8192:safe.append((target,body))
    if len(safe)==1:
        target,body=safe[0];status,location,text=request(target,'POST',body)
        # POST is never automatically repeated. A 303 may lead to a read-only receipt page.
        if status==303 and location:
            current=urljoin(target,location);status,_,text=request(current)
        if status//100==2 and confirmed(text):return {'status':'unsubscribed','method':'web_form'}
        return {'status':'request_accepted' if status//100==2 else 'manual_confirmation_required','url':current,
            'note':'The form was submitted once. Subscription removal is not verified; do not retry automatically.'}
    return {'status':'manual_confirmation_required','url':current,'note':'The sender requires interactive confirmation, login, CAPTCHA or an unsupported page. No confirmation form was submitted.'}


def perform(info,send):
    result={key:info[key] for key in ('folder','uid','sender','list_id')}
    if not info['methods']:return {**result,'status':'unsupported','note':'This email has no unsubscribe header or identifiable unsubscribe link.'}
    item=info['methods'][0];url=item['url']
    if item['method']=='one_click':
        status,location,text=request(url,'POST',b'List-Unsubscribe=One-Click')
        if status//100==2:
            return {**result,'method':'one_click','status':'unsubscribed' if confirmed(text) else 'request_accepted',
                'note':'The sender accepted the one-click request. HTTP acceptance alone does not prove subscription removal.'}
        # A definite HTTP refusal may use the alternate mailto explicitly
        # supplied in the same authenticated List-Unsubscribe header. Never
        # retry a timeout/5xx/redirect or bypass an interactive web challenge.
        alternate=next((method for method in info['methods'] if method['method']=='mailto'
                        and method['source']=='List-Unsubscribe'),None)
        if status in (403,404,405,410) and alternate and info.get('one_click_authenticated'):
            fallback=perform({**info,'methods':[alternate]},send)
            return {**fallback,'fallback_from':'one_click','initial_http_status':status,
                'note':'The HTTPS endpoint explicitly rejected the request; used the alternate mailto from the authenticated newsletter header. '+fallback['note']}
        return {**result,'status':'failed_or_uncertain','http_status':status,
            'note':'One-click request was not accepted. No redirect, fallback send or automatic retry was performed.'}
    if item['method']=='mailto':
        parsed=urlsplit(url);params=parse_qs(parsed.query,keep_blank_values=True)
        to=unquote(parsed.path);subject=params.get('subject',['unsubscribe'])[0];body=params.get('body',['Please unsubscribe me from this mailing list.'])[0]
        if parsed.netloc or len(to)>320 or not re.fullmatch(r'[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+',to) or set(params)-{'subject','body'} or len(subject)>500 or len(body)>10000:
            raise MailError('Unsupported unsubscribe email request')
        if any(c in subject+to for c in ('\r','\n','\0')):raise MailError('Invalid unsubscribe email headers')
        sent=send(to,subject,body)
        return {**result,'method':'mailto','status':'request_sent' if sent['status']=='sent' else 'failed_or_uncertain',
            'note':'An unsubscribe request email was sent; removal from the sender list is not yet verified.'}
    return {**result,**web_unsubscribe(url)}
