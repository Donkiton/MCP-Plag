import base64
import hashlib
import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

TMP = tempfile.TemporaryDirectory()
os.environ['PUBLIC_BASE_URL']='https://mcp.example.test'
os.environ['DB_PATH']=os.path.join(TMP.name,'test.sqlite3')
os.environ['MAILRU_EMAIL']='example@mail.ru'
os.environ['MAILRU_APP_PASSWORD']='fake-not-a-secret'
os.environ['MCP_LOGIN_PASSWORD']='a-strong-independent-test-password'
from mailru_mcp import server, mailbox

class DummyMailbox:
    send_count=0
    def __init__(self): pass
    def send(self,to,subject,body,cc=''):
        DummyMailbox.send_count += 1
        return {'status':'sent','to':to,'subject':subject}
    def read(self, folder, uid): return {'folder':folder,'uid':uid,'subject':'Test'}
    def move(self,folder,uid,target): return {'status':'moved'}
    def trash(self,folder,uid): return {'status':'moved_to_trash'}

class ServerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        cls.web=server.BoundedHTTPServer(('127.0.0.1',0),server.Handler)
        cls.thread=threading.Thread(target=cls.web.serve_forever, daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.web.shutdown();cls.web.server_close();cls.thread.join()
    def setUp(self):
        with server.db() as c: c.execute('DELETE FROM attempts')
    def call(self, method, path, obj=None, headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.web.server_port,timeout=5)
        payload=None if obj is None else (obj if isinstance(obj,str) else json.dumps(obj))
        headers=dict(headers or {})
        if hasattr(self,'cookie'): headers.setdefault('Cookie',self.cookie)
        conn.request(method,path,body=payload,headers=headers)
        r=conn.getresponse(); raw=r.read(); result=(r.status,r.getheaders(),raw)
        if dict(r.getheaders()).get('Set-Cookie'): self.cookie=dict(r.getheaders())['Set-Cookie'].split(';')[0]
        conn.close(); return result
    def make_oauth(self,scope=None,auth_method='none'):
        redirect='https://chatgpt.com/connector/oauth/mailru-test'
        status,_,raw=self.call('POST','/register',{'redirect_uris':[redirect],'token_endpoint_auth_method':auth_method})
        self.assertEqual(status,201)
        registered=json.loads(raw); client=registered['client_id']
        self.client_secret=registered.get('client_secret','')
        verifier='X'*55
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        from urllib.parse import urlencode
        path='/authorize?'+urlencode(dict(client_id=client,redirect_uri=redirect,response_type='code',state='safe123',
                                           code_challenge=challenge,code_challenge_method='S256',resource=server.RESOURCE,scope=scope or server.SCOPE))
        status,_,html=self.call('GET',path)
        self.assertEqual(status,200)
        nonce=re.search(rb'name="nonce" value="([^"]+)"',html).group(1).decode()
        status,headers,_=self.call('POST','/authorize', 'nonce='+nonce+'&password='+os.environ['MCP_LOGIN_PASSWORD'],
                                   {'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(status,302)
        location=dict(headers)['Location']
        code=parse_qs(urlsplit(location).query)['code'][0]
        status,_,raw=self.call('POST','/token','grant_type=authorization_code&client_id='+client+'&code='+code+
                               '&redirect_uri='+redirect+'&code_verifier='+verifier+'&resource='+server.RESOURCE+'&client_secret='+self.client_secret,
                               {'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(status,200)
        token=json.loads(raw)
        self.last_exchange=dict(grant_type='authorization_code',client_id=client,code=code,redirect_uri=redirect,code_verifier=verifier,resource=server.RESOURCE,client_secret=self.client_secret)
        return client,token
    def test_auth_and_tool_list(self):
        self.assertEqual(self.call('POST','/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/list'})[0],401)
        self.assertEqual(self.call('POST','/register',{'redirect_uris':['https://attacker.example/callback']})[0],400)
        client,token=self.make_oauth()
        headers={'Authorization':'Bearer '+token['access_token']}
        status,_,raw=self.call('POST','/mcp',{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}},headers)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['result']['serverInfo']['name'],'mailru-private-mcp')
        self.assertNotIn('icons',json.loads(raw)['result']['serverInfo'])
        status,_,raw=self.call('POST','/mcp',{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},headers)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['result']['serverInfo']['icons'],[{'src':server.BASE_URL+'/icon.png','mimeType':'image/png','sizes':['512x512']}])
        status,_,raw=self.call('POST','/mcp',{'jsonrpc':'2.0','id':2,'method':'tools/list'},headers)
        self.assertEqual(status,200)
        self.assertEqual(len(json.loads(raw)['result']['tools']),20)
        self.assertEqual(self.call('POST','/mcp',{'jsonrpc':'2.0','id':3,'method':'tools/list'}, {'Authorization':'Bearer wrong'})[0],401)
        refresh=token['refresh_token']
        status,_,raw=self.call('POST','/token','grant_type=refresh_token&client_id='+client+'&refresh_token='+refresh+'&resource='+server.RESOURCE,
                               {'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(status,200)
        self.assertNotEqual(json.loads(raw)['access_token'],token['access_token'])
    @patch.object(server,'Mailbox',DummyMailbox)
    def test_send_executes_without_confirmation(self):
        DummyMailbox.send_count=0
        _,token=self.make_oauth()
        _,_,raw=self.call('POST','/mcp',{'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'prepare_send','arguments':{'to':'test@example.org','subject':'Hello','body':'Test body'}}},{'Authorization':'Bearer '+token['access_token']})
        result=json.loads(raw)['result']
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['status'],'sent')
        self.assertNotIn('confirmation_url',result['structuredContent'])
        self.assertEqual(DummyMailbox.send_count,1)

class MailValidationTests(unittest.TestCase):
    def test_header_injection_rejected(self):
        with self.assertRaises(mailbox.MailError): mailbox._new_message('x@example.org','hello\r\nBcc: bad@bad.test','body')
        with self.assertRaises(mailbox.MailError): mailbox._new_message('x@example.org\nBcc: bad@bad.test','hello','body')
    def test_mail_parsing(self):
        msg,_=mailbox._new_message('x@example.org','Привет','Body content')
        raw=msg.as_bytes()
        from email import message_from_bytes
        self.assertIn('Body content',mailbox._preview(message_from_bytes(raw,policy=mailbox.email.policy.default)))

if __name__=='__main__': unittest.main()
