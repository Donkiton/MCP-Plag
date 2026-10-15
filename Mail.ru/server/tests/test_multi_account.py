import base64
import hashlib
import json
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlencode,parse_qs,urlsplit
from cryptography.fernet import Fernet
import test_server as base
server=base.server

class AccountMailbox:
    sent=[]
    moved=[]
    def __init__(self,username,password): self.username=username;self.password=password
    def _connect(self):
        if self.password!='password-for-'+self.username: raise server.MailError('Bad login')
        return self
    def logout(self): pass
    def recent(self,folder,limit,**kwargs): return {'items':[{'mailbox':self.username}]} if kwargs.get('paged') else [{'mailbox':self.username}]
    def send(self,to,subject,body,cc='',bcc='',html_body='',attachments=None):
        self.sent.append(self.username)
        return {'status':'sent','mailbox':self.username}
    def message_identity(self,folder,uid):
        return {'folder':folder,'uid':uid,'uid_validity':'42','message_fingerprint':hashlib.sha256(self.username.encode()).hexdigest()}
    def read(self,folder,uid):
        return self.message_identity(folder,uid)
    def move(self,folder,uid,target,expected_identity=None):
        identity=self.message_identity(folder,uid)
        if expected_identity!={k:identity[k] for k in ('uid_validity','message_fingerprint')}:
            raise server.MailError('Message identity changed')
        self.moved.append((self.username,folder,uid,target))
        return {'status':'moved','mailbox':self.username,'destination':target}
    def trash(self,folder,uid,expected_identity=None):
        return self.move(folder,uid,'Trash',expected_identity)
    def trash_many(self,folder,uids):
        for uid in uids:self.move(folder,uid,'Trash',expected_identity={k:self.message_identity(folder,uid)[k] for k in ('uid_validity','message_fingerprint')})
        return {'status':'moved','mailbox':self.username,'uids':uids,'count':len(uids),'destination':'Trash','recoverable':True}

class MultipleAccounts(base.ServerIntegrationTests):
    test_auth_and_tool_list=None
    test_send_executes_without_confirmation=None
    def test_folder_management_stays_in_selected_account(self):
        _,first=self.connect('folder-first@mail.ru')
        _,second=self.connect('folder-second@bk.ru')
        def action(box,*args,**kwargs):return {'status':'ok','mailbox':box.username}
        for name,args in [('empty_folder',{'folder':'Trash'}),('create_folder',{'name':'New'}),
                          ('rename_folder',{'folder':'Custom','name':'New'}),('delete_folder',{'folder':'Custom'}),
                          ('delete_message_permanently',{'folder':'INBOX','uid':'7'})]:
            with patch.object(AccountMailbox,name,action,create=True):
                for token,address in [(first,'folder-first@mail.ru'),(second,'folder-second@bk.ru')]:
                    result=self.rpc(token['access_token'],name,args)
                    self.assertFalse(result['isError'])
                    self.assertEqual(result['structuredContent']['mailbox'],address)
    def setUp(self):
        super().setUp()
        self.old_mode=server.MULTI_ACCOUNT
        self.old_key=server.FERNET_KEY
        server.MULTI_ACCOUNT=True
        server.FERNET_KEY=Fernet.generate_key().decode()
        with server.db() as c:
            for table in ('accounts','families','tokens','pending'): c.execute('DELETE FROM '+table)
        self.mock=patch.object(server,'Mailbox',AccountMailbox);self.mock.start()
        AccountMailbox.moved=[]
        AccountMailbox.sent=[]
    def tearDown(self):
        self.mock.stop()
        server.MULTI_ACCOUNT=self.old_mode;server.FERNET_KEY=self.old_key
    def oauth_form(self):
        redirect='https://chatgpt.com/connector/oauth/multi-test'
        _,_,raw=self.call('POST','/register',{'redirect_uris':[redirect]})
        client=json.loads(raw)['client_id'];verifier='X'*55
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        path='/authorize?'+urlencode(dict(client_id=client,redirect_uri=redirect,response_type='code',state='state',code_challenge=challenge,code_challenge_method='S256'))
        _,_,html=self.call('GET',path)
        nonce=re.search(rb'name="nonce" value="([^"]+)"',html).group(1).decode()
        self.assertNotIn(b'access_code',html)
        self.assertNotIn('Код владельца'.encode(),html)
        return client,verifier,redirect,nonce
    def connect(self,email,headers=None,elapsed=0):
        client,verifier,redirect,nonce=self.oauth_form()
        form=urlencode({'nonce':nonce,'email':email,'app_password':'password-for-'+email})
        with patch.object(server,'now',return_value=server.now()+elapsed):
            status,headers,_=self.call('POST','/authorize',form,headers)
        self.assertEqual(status,302)
        code=parse_qs(urlsplit(dict(headers)['Location']).query)['code'][0]
        status,_,raw=self.call('POST','/token',urlencode(dict(grant_type='authorization_code',client_id=client,code=code,redirect_uri=redirect,code_verifier=verifier)))
        self.assertEqual(status,200)
        return client,json.loads(raw)
    def test_mobile_login_without_cookie_after_switching_apps(self):
        _,token=self.connect('mobile@mail.ru',{'Cookie':'','Origin':server.BASE_URL},elapsed=600)
        self.assertEqual(self.rpc(token['access_token'],'get_profile')['structuredContent']['email'],'mobile@mail.ru')
    def test_mobile_login_with_replaced_cookie(self):
        _,token=self.connect('mobile@mail.ru',{'Cookie':server.COOKIE+'=another-browser','Origin':server.BASE_URL})
        self.assertIsNotNone(server.get_token(token['access_token']))
    def test_bare_login_connects_to_mail_ru_and_retry_keeps_new_form(self):
        _,_,_,nonce=self.oauth_form()
        form=urlencode({'nonce':nonce,'email':'bare-login','app_password':'password-for-bare-login@mail.ru'})
        self.assertEqual(self.call('POST','/authorize',form)[0],302)
        with server.db() as c:self.assertIsNotNone(c.execute('SELECT id FROM accounts WHERE email=?',('bare-login@mail.ru',)).fetchone())
        _,_,_,nonce=self.oauth_form()
        status,_,page=self.call('POST','/authorize',urlencode({'nonce':nonce,'email':'wrong@mail.ru','app_password':'wrong-password'}))
        self.assertEqual(status,403)
        self.assertIn(('name="nonce" value="'+nonce+'"').encode(),page)
        self.assertIn(b'/login.js',page);self.assertIn(b'/login.css',page)
        self.assertNotIn(b'wrong-password',page)
    def test_cancel_preserves_oauth_state_and_consumes_only_login_nonce(self):
        self.connect('connected@mail.ru')
        _,_,redirect,nonce=self.oauth_form()
        with server.db() as c:before={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('accounts','tokens','families')}
        status,headers,_=self.call('GET','/cancel?'+urlencode({'nonce':nonce}))
        self.assertEqual(status,302)
        location=dict(headers)['Location'];self.assertTrue(location.startswith(redirect+'?'))
        self.assertEqual(parse_qs(urlsplit(location).query),{'error':['access_denied'],'state':['state']})
        self.assertEqual(self.call('GET','/cancel?'+urlencode({'nonce':nonce}))[0],410)
        self.assertEqual(self.call('POST','/authorize',urlencode({'nonce':nonce,'email':'cancelled@mail.ru','app_password':'password-for-cancelled@mail.ru'}))[0],403)
        with server.db() as c:self.assertEqual(before,{t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('accounts','tokens','families')})
    def test_login_assets_have_correct_types_and_policy(self):
        for path,ctype in [('/login.css','text/css'),('/login.js','application/javascript'),('/login-icon.png','image/png'),('/roboto.ttf','font/ttf')]:
            status,headers,body=self.call('GET',path)
            self.assertEqual(status,200);self.assertEqual(dict(headers)['Content-Type'],ctype);self.assertTrue(body)
        _,_,_,nonce=self.oauth_form()
        page=server.login_page(nonce,'<script>invalid</script>')
        self.assertIn('&lt;script&gt;invalid&lt;/script&gt;',page)
        self.assertEqual(self.call('GET','/login.html')[0],404)
    def test_failed_login_limit_is_per_address_and_success_resets_it(self):
        handler=server.Handler.__new__(server.Handler)
        handler.client_address=('127.0.0.1',123)
        handler.headers={}
        for _ in range(10):self.assertTrue(handler._login_attempt_allowed('test-probe@mail.ru'))
        self.assertFalse(handler._login_attempt_allowed('test-probe@mail.ru'))
        self.assertTrue(handler._login_attempt_allowed('real-user@mail.ru'))
        self.connect('real-user@mail.ru')
        with server.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM attempts WHERE ip=?',(handler._login_attempt_key('real-user@mail.ru'),)).fetchone()[0],0)
        self.assertFalse(handler._login_attempt_allowed('test-probe@mail.ru'))
    def test_failed_password_can_retry_same_form_and_success_is_one_use(self):
        _,_,_,nonce=self.oauth_form()
        headers={'Cookie':'','Origin':server.BASE_URL}
        bad=urlencode({'nonce':nonce,'email':'retry@mail.ru','app_password':'wrong-password'})
        status,_,html=self.call('POST','/authorize',bad,headers)
        self.assertEqual(status,403)
        self.assertIn("form-action 'self' https://chatgpt.com;",dict(self.call('POST','/authorize',bad,headers)[1])['Content-Security-Policy'])
        self.assertIn(('name="nonce" value="'+nonce+'"').encode(),html)
        self.assertNotIn(b'wrong-password',html)
        form=urlencode({'nonce':nonce,'email':'retry@mail.ru','app_password':'password-for-retry@mail.ru'})
        self.assertEqual(self.call('POST','/authorize',form,headers)[0],302)
        self.assertEqual(self.call('POST','/authorize',form,headers)[0],403)
        with server.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM codes WHERE account_id IN (SELECT id FROM accounts WHERE email=?)',('retry@mail.ru',)).fetchone()[0],1)
    def test_cookieless_login_requires_same_origin_and_form_still_expires(self):
        _,_,_,nonce=self.oauth_form()
        form=urlencode({'nonce':nonce,'email':'origin@mail.ru','app_password':'password-for-origin@mail.ru'})
        for origin in ('https://evil.test','null',None):
            headers={'Cookie':''}
            if origin is not None: headers['Origin']=origin
            self.assertEqual(self.call('POST','/authorize',form,headers)[0],403)
        with server.db() as c:
            row=c.execute('SELECT expires FROM login_nonce WHERE nonce_hash=?',(server.digest(nonce),)).fetchone()
            self.assertGreaterEqual(row['expires']-server.now(),1190)
            c.execute('UPDATE login_nonce SET expires=? WHERE nonce_hash=?',(server.now()-1,server.digest(nonce)))
        self.assertEqual(self.call('POST','/authorize',form,{'Cookie':'','Origin':server.BASE_URL})[0],403)
        with server.db() as c: self.assertEqual(c.execute('SELECT count(*) FROM accounts').fetchone()[0],0)
    def test_parallel_login_submissions_issue_only_one_code(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        _,_,_,nonce=self.oauth_form()
        # The form's response can arrive before its worker releases a slot.
        # Ensure both slots are free so this tests nonce concurrency, not overload.
        acquired=0
        try:
            for _ in range(2):
                self.assertTrue(self.web.slots.acquire(timeout=5))
                acquired+=1
        finally:
            for _ in range(acquired):self.web.slots.release()
        barrier=threading.Barrier(2)
        original=AccountMailbox._connect
        def simultaneous(mailbox):
            barrier.wait(timeout=5)
            return original(mailbox)
        form=urlencode({'nonce':nonce,'email':'parallel@mail.ru','app_password':'password-for-parallel@mail.ru'})
        headers={'Cookie':'','Origin':server.BASE_URL}
        with patch.object(AccountMailbox,'_connect',simultaneous),ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda _:self.call('POST','/authorize',form,headers)[0],range(2)))
        self.assertEqual(sorted(statuses),[302,403])
        with server.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM codes WHERE account_id IN (SELECT id FROM accounts WHERE email=?)',('parallel@mail.ru',)).fetchone()[0],1)
    def rpc(self,token,name,args={}):
        _,_,raw=self.call('POST','/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}},{'Authorization':'Bearer '+token})
        return json.loads(raw)['result']
    def test_profiles_and_mail_data_are_per_connection(self):
        _,a=self.connect('first@mail.ru');_,b=self.connect('second@bk.ru')
        self.assertEqual(self.rpc(a['access_token'],'get_profile')['structuredContent']['email'],'first@mail.ru')
        self.assertEqual(self.rpc(b['access_token'],'get_profile')['structuredContent']['email'],'second@bk.ru')
        self.assertEqual(self.rpc(a['access_token'],'list_messages')['structuredContent']['items'][0]['mailbox'],'first@mail.ru')
        self.assertEqual(self.rpc(b['access_token'],'list_messages')['structuredContent']['items'][0]['mailbox'],'second@bk.ru')
        with server.db() as c: raw=' '.join(x['secret'] for x in c.execute('SELECT secret FROM accounts'))
        self.assertNotIn('password-for-',raw)
    def test_refresh_preserves_mailbox_identity(self):
        client,a=self.connect('refresh@mail.ru')
        self.connect('other@mail.ru')
        status,_,raw=self.call('POST','/token',urlencode(dict(grant_type='refresh_token',client_id=client,refresh_token=a['refresh_token'])))
        self.assertEqual(status,200)
        self.assertEqual(self.rpc(json.loads(raw)['access_token'],'get_profile')['structuredContent']['email'],'refresh@mail.ru')
    def test_cross_account_action_status_is_private(self):
        _,a=self.connect('sender@mail.ru');_,b=self.connect('other@mail.ru')
        action=server.random_token()
        with server.db() as c:
            c.execute('INSERT INTO pending(id_hash,kind,args,created,expires,status,result,account_id) VALUES(?,?,?,?,?,?,?,?)',
                      (server.digest(action),'send','{}',server.now(),server.now()+900,'cancelled',None,server.get_token(a['access_token'])['account_id']))
        result=self.rpc(b['access_token'],'action_status',{'action_id':action})['structuredContent']
        self.assertEqual(result['status'],'not_found_or_expired')
    def test_move_and_delete_execute_directly_in_selected_mailbox(self):
        _,a=self.connect('first@mail.ru');_,b=self.connect('second@bk.ru')
        for token,name,args,address,target in [
            (a,'prepare_move',{'folder':'INBOX','uid':'7','target':'Archive'},'first@mail.ru','Archive'),
            (b,'prepare_delete',{'folder':'INBOX','uid':'7'},'second@bk.ru','Trash')]:
            result=self.rpc(token['access_token'],name,args)
            self.assertFalse(result.get('isError',False))
            self.assertEqual(result['structuredContent'],{'status':'moved','mailbox':address,'destination':target})
            self.assertNotIn('confirmation_url',result['structuredContent'])
        self.assertEqual(AccountMailbox.moved,[('first@mail.ru','INBOX','7','Archive'),('second@bk.ru','INBOX','7','Trash')])
        with server.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM pending').fetchone()[0],0)
    def test_move_and_delete_never_read_full_message(self):
        _,token=self.connect('large-mail@mail.ru')
        with patch.object(AccountMailbox,'read',side_effect=server.MailError('Message exceeds 12 MB download limit')):
            for name,args in [('prepare_move',{'folder':'INBOX','uid':'7','target':'Archive'}),('prepare_delete',{'folder':'INBOX','uid':'8'})]:
                result=self.rpc(token['access_token'],name,args)
                self.assertFalse(result.get('isError',False),result)
                self.assertEqual(result['structuredContent']['status'],'moved')
        self.assertEqual(len(AccountMailbox.moved),2)

    def test_direct_mail_actions_still_require_write_scope(self):
        _,a=self.connect('reader@mail.ru')
        with server.db() as c:c.execute('UPDATE tokens SET scope=? WHERE token_hash=?',('mail.read',server.digest(a['access_token'])))
        for name,args in [('prepare_send',{'to':'x@example.org','subject':'s','body':'b'}),('prepare_move',{'folder':'INBOX','uid':'7','target':'Archive'}),('prepare_delete',{'folder':'INBOX','uid':'7'}),('trash_messages',{'folder':'INBOX','uids':['7','8']}),('trash_all_messages',{'folder':'INBOX'}),('unsubscribe_senders',{'messages':[{'folder':'INBOX','uid':'7'}]})]:
            self.assertTrue(self.rpc(a['access_token'],name,args)['isError'])
        self.assertEqual(AccountMailbox.moved,[])
        self.assertEqual(AccountMailbox.sent,[])

    def test_new_background_operations_stay_in_selected_account(self):
        _,a=self.connect('jobs-first@mail.ru');_,b=self.connect('jobs-second@mail.ru')
        def trash(box,folder,progress=None):
            if progress:progress({'captured_count':120,'moved_count':120})
            return {'status':'moved','count':120,'mailbox':box.username}
        def unsubscribe(box,messages,progress=None):return {'status':'processed','mailbox':box.username,'results':[]}
        with patch.object(AccountMailbox,'trash_all',trash,create=True),patch.object(AccountMailbox,'unsubscribe_messages',unsubscribe,create=True),patch.object(server,'spawn_mail_job'):
            for token,address in [(a,'jobs-first@mail.ru'),(b,'jobs-second@mail.ru')]:
                for name,args in [('trash_all_messages',{'folder':'Sent'}),('unsubscribe_senders',{'messages':[{'folder':'INBOX','uid':'7'}]})]:
                    result=self.rpc(token['access_token'],name,args)['structuredContent']
                    action=result['action_id'];account_id=server.get_token(token['access_token'])['account_id']
                    server.run_mail_job(action,name,args,account_id)
                    state=self.rpc(token['access_token'],'action_status',{'action_id':action})['structuredContent']
                    self.assertEqual(state['status'],'completed');self.assertEqual(state['result']['mailbox'],address)
                    other=b if token is a else a
                    self.assertEqual(self.rpc(other['access_token'],'action_status',{'action_id':action})['structuredContent']['status'],'not_found_or_expired')

    def test_bulk_trash_uses_selected_account_and_validates_uid_list(self):
        _,token=self.connect('batch@mail.ru')
        result=self.rpc(token['access_token'],'trash_messages',{'folder':'INBOX','uids':['7','8','9']})
        self.assertFalse(result.get('isError',False),result)
        self.assertEqual(result['structuredContent']['count'],3)
        self.assertTrue(result['structuredContent']['recoverable'])
        self.assertEqual(AccountMailbox.moved,[('batch@mail.ru','INBOX',uid,'Trash') for uid in ['7','8','9']])
        for invalid in [[],['7','7'],['1:*'],['1,2'],[7],['7']*51,'7']:
            result=self.rpc(token['access_token'],'trash_messages',{'folder':'INBOX','uids':invalid})
            self.assertTrue(result['isError'],invalid)
        self.assertEqual(len(AccountMailbox.moved),3)
    def test_direct_mail_actions_refuse_changed_message_identity(self):
        _,a=self.connect('identity@mail.ru')
        original=AccountMailbox.message_identity
        for name,args in [('prepare_move',{'folder':'INBOX','uid':'7','target':'Archive'}),('prepare_delete',{'folder':'INBOX','uid':'7'})]:
            calls=[]
            def changed(mailbox,folder,uid):
                info=original(mailbox,folder,uid);calls.append(None)
                if len(calls)>1:info['uid_validity']='43'
                return info
            with patch.object(AccountMailbox,'message_identity',changed):
                self.assertTrue(self.rpc(a['access_token'],name,args)['isError'])
        self.assertEqual(AccountMailbox.moved,[])
    def test_old_confirmation_pages_cannot_execute(self):
        _,a=self.connect('old-link@mail.ru')
        account_id=server.get_token(a['access_token'])['account_id']
        for kind in ('send','move','delete'):
            action=server.random_token()
            with server.db() as c:
                c.execute('INSERT INTO pending(id_hash,kind,args,created,expires,status,result,account_id) VALUES(?,?,?,?,?,?,?,?)',
                          (server.digest(action),kind,'{}',server.now(),server.now()+900,'waiting',None,account_id))
            path='/confirm/'+action
            status,_,page=self.call('GET',path)
            self.assertEqual(status,410);self.assertNotIn(b'<form',page)
            self.assertEqual(self.call('POST',path,'')[0],410)
        self.assertEqual(AccountMailbox.moved,[])
    def test_send_uses_each_selected_connected_mailbox_without_password(self):
        _,a=self.connect('sender@mail.ru');_,b=self.connect('other@bk.ru')
        for token,address in [(a,'sender@mail.ru'),(b,'other@bk.ru')]:
            result=self.rpc(token['access_token'],'prepare_send',{'to':'x@example.org','subject':'s','body':'b'})
            self.assertFalse(result['isError'])
            self.assertEqual(result['structuredContent'],{'status':'sent','mailbox':address})
        self.assertEqual(AccountMailbox.sent,['sender@mail.ru','other@bk.ru'])
        with server.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM pending').fetchone()[0],0)
    def test_fresh_browser_connects_without_owner_code_and_bad_login_not_saved(self):
        self.connect('fresh-browser@mail.ru')
        with server.db() as c: count=c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        with self.assertRaises(server.MailError): server.register_mail_account('bad@mail.ru','wrong-password')
        with server.db() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0],count)
    def test_sender_header_uses_selected_mailbox(self):
        msg,_=base.mailbox._new_message('x@example.org','s','b',sender='selected@bk.ru')
        self.assertEqual(msg['From'],'selected@bk.ru')
        self.assertIn('@bk.ru>',msg['Message-ID'])
    def test_reconnection_keeps_profile_id_and_revocation_is_isolated(self):
        ca,a=self.connect('same@mail.ru');_,b=self.connect('other@mail.ru');_,again=self.connect('same@mail.ru')
        first=self.rpc(a['access_token'],'get_profile')['structuredContent']['id']
        self.assertEqual(first,self.rpc(again['access_token'],'get_profile')['structuredContent']['id'])
        self.assertNotEqual(first,self.rpc(b['access_token'],'get_profile')['structuredContent']['id'])
        self.assertEqual(self.call('POST','/revoke',urlencode({'client_id':ca,'token':a['refresh_token']}))[0],200)
        self.assertIsNone(server.get_token(a['access_token']))
        self.assertIsNotNone(server.get_token(b['access_token']))
        self.assertIsNotNone(server.get_token(again['access_token']))

if __name__=='__main__': unittest.main()
