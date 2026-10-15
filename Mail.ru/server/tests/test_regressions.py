import base64
import hashlib
import json
import re
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import urlencode, urlsplit, parse_qs
import test_server as base
DummyMailbox,mailbox,server=base.DummyMailbox,base.mailbox,base.server


class RegressionTests(base.ServerIntegrationTests):
    # Baseline cases run on ServerIntegrationTests; do not run inherited tests twice.
    test_auth_and_tool_list=None
    test_send_executes_without_confirmation=None

    def rpc(self,token,name,args):
        _,_,raw=self.call('POST','/mcp',{'jsonrpc':'2.0','id':99,'method':'tools/call','params':{'name':name,'arguments':args}},
                          {'Authorization':'Bearer '+token})
        return json.loads(raw)['result']

    def refresh(self,client,refresh,**extra):
        return self.call('POST','/token',urlencode(dict(grant_type='refresh_token',client_id=client,refresh_token=refresh,resource=server.RESOURCE,**extra)))

    def test_oauth_discovery_does_not_advertise_unsupported_oidc(self):
        status,_,raw=self.call('GET','/.well-known/oauth-authorization-server')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['issuer'],server.BASE_URL)
        self.assertEqual(self.call('GET','/.well-known/openid-configuration')[0],404)

    def test_refresh_replay_revokes_family(self):
        client,token=self.make_oauth()
        status,_,raw=self.refresh(client,token['refresh_token']); self.assertEqual(status,200)
        second=json.loads(raw)
        self.assertEqual(self.refresh(client,token['refresh_token'])[0],400)
        self.assertIsNone(server.get_token(second['access_token']))
        self.assertIsNone(server.get_token(token['access_token']))
        self.assertEqual(self.refresh(client,second['refresh_token'])[0],400)

    def test_revocation(self):
        client,token=self.make_oauth()
        self.assertEqual(self.call('POST','/revoke',urlencode({'client_id':client,'token':token['refresh_token']}))[0],200)
        self.assertIsNone(server.get_token(token['access_token']))
        self.assertEqual(self.refresh(client,token['refresh_token'])[0],400)

    def test_scope_is_enforced_and_cannot_expand(self):
        client,token=self.make_oauth(scope='mail.read')
        with patch.object(server,'Mailbox',DummyMailbox):
            result=self.rpc(token['access_token'],'create_draft',{'to':'test@example.org','subject':'S','body':'B'})
        self.assertTrue(result['isError'])
        self.assertEqual(self.refresh(client,token['refresh_token'],scope=server.SCOPE)[0],400)
        self.assertEqual(self.refresh(client,token['refresh_token'])[0],200)

    def test_client_secret_checked_before_consumption(self):
        client,token=self.make_oauth(auth_method='client_secret_post')
        self.assertEqual(self.refresh(client,token['refresh_token'],client_secret='wrong')[0],401)
        self.assertEqual(self.refresh(client,token['refresh_token'],client_secret=self.client_secret)[0],200)

    def test_client_and_resource_binding(self):
        client,token=self.make_oauth()
        other,_=self.make_oauth()
        self.assertEqual(self.refresh(other,token['refresh_token'])[0],400)
        self.assertEqual(self.call('POST','/token',urlencode(dict(grant_type='refresh_token',client_id=client,refresh_token=token['refresh_token'],resource='https://attacker.test/mcp')))[0],400)
        self.assertEqual(self.refresh(client,token['refresh_token'])[0],200)

    def test_callback_allowlist(self):
        for value in ['https://chatgpt.com/a/oauth/callback','https://chatgpt.com/evil','https://chatgpt.com/connector/oauth/a?redirect=https://evil.test','https://chatgpt.com.evil.test/connector/oauth/a','https://user@chatgpt.com/connector/oauth/a','https://chat.openai.com/callback']:
            self.assertEqual(self.call('POST','/register',{'redirect_uris':[value]})[0],400,value)
        self.assertTrue(server.valid_redirect('https://chatgpt.com/connector_platform_oauth_redirect'))

    def test_successful_code_cannot_be_reused(self):
        self.make_oauth()
        self.assertEqual(self.call('POST','/token',urlencode(self.last_exchange))[0],400)

    def test_invalid_message_headers_are_refused_before_smtp(self):
        _,token=self.make_oauth()
        with patch.object(mailbox.smtplib,'SMTP_SSL') as smtp:
            result=self.rpc(token['access_token'],'prepare_send',{'to':'x@example.org','subject':'s\r\nBcc: bad@example.org','body':'b'})
        self.assertTrue(result['isError']);smtp.assert_not_called()

    def test_uncertain_smtp_delivery_is_not_retried(self):
        import smtplib
        _,token=self.make_oauth()
        with patch.object(server,'Mailbox',DummyMailbox),patch.object(DummyMailbox,'send',side_effect=smtplib.SMTPServerDisconnected('Test disconnect')) as send:
            result=self.rpc(token['access_token'],'prepare_send',{'to':'x@example.org','subject':'s','body':'b'})
        self.assertTrue(result['isError']);send.assert_called_once()
        self.assertIn('Do not retry automatically',result['content'][0]['text'])

    def test_read_transport_error_returns_mcp_error_then_connection_works(self):
        _,token=self.make_oauth()
        with patch.object(server,'execute_tool',side_effect=TimeoutError('fake timeout')) as action:
            result=self.rpc(token['access_token'],'list_messages',{'folder':'Trash'})
        self.assertTrue(result['isError']);action.assert_called_once()
        self.assertIn('timed out',result['content'][0]['text'])
        self.assertFalse(self.rpc(token['access_token'],'get_profile',{})['isError'])

    def test_disconnect_during_response_does_not_attempt_recursive_500(self):
        import io
        handler=object.__new__(server.Handler)
        handler.request_version='HTTP/1.1';handler.command='POST';handler.requestline='POST /mcp HTTP/1.1'
        handler.wfile=MagicMock(spec=io.BytesIO)
        handler.wfile.write.side_effect=BrokenPipeError()
        handler.send(200,{'status':'ok'})
        self.assertTrue(handler.close_connection)

    def test_oauth_browser_binding_pkce_and_code_one_use(self):
        redirect='https://chatgpt.com/connector/oauth/code-test'
        _,_,raw=self.call('POST','/register',{'redirect_uris':[redirect]}); client=json.loads(raw)['client_id']
        verifier='A'*55; challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params=dict(client_id=client,redirect_uri=redirect,response_type='code',state='original',code_challenge=challenge,code_challenge_method='S256')
        _,_,page=self.call('GET','/authorize?'+urlencode(params))
        nonce=re.search(rb'name="nonce" value="([^"]+)"',page).group(1).decode()
        payload=urlencode({'nonce':nonce,'password':server.LOGIN_PASSWORD})
        self.assertEqual(self.call('POST','/authorize',payload,{'Cookie':''})[0],403)
        status,headers,_=self.call('POST','/authorize',payload); self.assertEqual(status,302)
        query=parse_qs(urlsplit(dict(headers)['Location']).query)
        self.assertEqual(query['state'],['original'])
        form=dict(grant_type='authorization_code',client_id=client,code=query['code'][0],redirect_uri=redirect,code_verifier='B'*55)
        self.assertEqual(self.call('POST','/token',urlencode(form))[0],400)
        form['code_verifier']=verifier
        self.assertEqual(self.call('POST','/token',urlencode(form))[0],400)

    def test_direct_send_preserves_complete_body_and_cc(self):
        _,token=self.make_oauth()
        args={'to':'t@example.org','cc':'copy@example.org','subject':'Review','body':'x'*22000+'END OF FULL BODY'}
        with patch.object(server,'Mailbox',DummyMailbox),patch.object(DummyMailbox,'send',return_value={'status':'sent'}) as send:
            result=self.rpc(token['access_token'],'prepare_send',args)
        self.assertFalse(result['isError'])
        send.assert_called_once_with(args['to'],args['subject'],args['body'],args['cc'],bcc='',html_body='',attachments=None)

    def test_malformed_arguments_and_profile(self):
        _,token=self.make_oauth()
        for name,args in [('list_messages',{'limit':True}),('list_messages',{'limit':51}),('read_message',{'folder':['INBOX'],'uid':'1'}),('get_profile',[]),('read_attachment',{'folder':'INBOX','uid':'1','index':-1})]:
            self.assertTrue(self.rpc(token['access_token'],name,args)['isError'])
        profile=self.rpc(token['access_token'],'get_profile',{})['structuredContent']
        self.assertEqual(profile['email'],'example@mail.ru')
        headers={'Authorization':'Bearer '+token['access_token']}
        self.assertEqual(self.call('POST','/mcp',{'jsonrpc':'2.0','method':'notifications/initialized'},headers)[0],202)
        self.assertEqual(self.call('POST','/mcp',{'jsonrpc':'2.0','id':3,'method':'tools/call','params':[]},headers)[0],400)

    def test_host_and_origin_protection(self):
        self.assertEqual(self.call('GET','/health',headers={'Host':'evil.test'})[0],403)
        self.assertEqual(self.call('GET','/health',headers={'Origin':'https://evil.test'})[0],403)

    def test_form_policy_preserves_origin_and_same_origin_post_is_accepted(self):
        redirect='https://chatgpt.com/connector/oauth/form-origin-test'
        _,_,raw=self.call('POST','/register',{'redirect_uris':[redirect]})
        client=json.loads(raw)['client_id']
        challenge=base64.urlsafe_b64encode(hashlib.sha256(b'X'*55).digest()).rstrip(b'=').decode()
        query=urlencode(dict(client_id=client,redirect_uri=redirect,response_type='code',state='form',code_challenge=challenge,code_challenge_method='S256'))
        status,headers,html=self.call('GET','/authorize?'+query)
        self.assertEqual(status,200)
        self.assertEqual(dict(headers)['Referrer-Policy'],'same-origin')
        policy=dict(headers)['Content-Security-Policy']
        self.assertIn("form-action 'self' https://chatgpt.com;",policy)
        self.assertNotIn('https:',policy.split('form-action ',1)[1].split(';',1)[0].replace('https://chatgpt.com',''))
        _,home_headers,_=self.call('GET','/')
        self.assertIn("form-action 'self';",dict(home_headers)['Content-Security-Policy'])
        nonce=re.search(rb'name="nonce" value="([^"]+)"',html).group(1).decode()
        form=urlencode({'nonce':nonce,'password':server.LOGIN_PASSWORD})
        self.assertEqual(self.call('POST','/authorize',form,{'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.call('POST','/authorize',form,{'Origin':'null'})[0],403)
        status,redirect_headers,_=self.call('POST','/authorize',form,{'Origin':server.BASE_URL})
        self.assertEqual(status,302)
        self.assertIn("form-action 'self' https://chatgpt.com;",dict(redirect_headers)['Content-Security-Policy'])

    def test_family_expiry_and_hashed_storage(self):
        _,token=self.make_oauth()
        with server.db() as c:
            rows=c.execute('SELECT token_hash,family FROM tokens WHERE token_hash=?',(server.digest(token['access_token']),)).fetchall()
            self.assertEqual(len(rows),1);self.assertNotEqual(rows[0]['token_hash'],token['access_token'])
            c.execute('UPDATE families SET expires=? WHERE id=?',(server.now()-1,rows[0]['family']))
        self.assertIsNone(server.get_token(token['access_token']))
        server.cleanup()


class BackendRegressionTests(unittest.TestCase):
    def test_bulk_trash_is_one_uid_move_and_preflights_headers(self):
        conn=MagicMock()
        conn.select.return_value=('OK',[])
        conn.capabilities=[b'MOVE']
        conn.list.return_value=('OK',[b'() "/" "INBOX"',b'(\\Trash) "/" "Trash"'])
        def command(name,uids,*args):
            if name=='FETCH':
                self.assertEqual(args,('(BODY.PEEK[HEADER])',))
                return 'OK',[(b'1 BODY[HEADER]',b'Subject: Batch\r\n\r\n')]
            if name=='SEARCH':return 'OK',[b'']
            self.assertEqual((name,uids,args),('MOVE','1:3',('"Trash"',)))
            return 'OK',[]
        conn.uid.side_effect=command
        box=mailbox.Mailbox('test@mail.ru','unused')
        with patch.object(box,'_with_imap',side_effect=lambda action:action(conn)):
            result=box.trash_many('INBOX',['1','2','3'])
        self.assertEqual(result['count'],3)
        self.assertEqual([call.args[0] for call in conn.uid.call_args_list],['FETCH','FETCH','FETCH','MOVE','SEARCH'])
        conn.select.assert_called_once_with('"INBOX"',readonly=False)

    def test_bulk_trash_missing_message_or_missing_trash_refuses_mutation(self):
        for reason in ('missing_uid','missing_trash','already_in_trash'):
            with self.subTest(reason=reason):
                conn=MagicMock()
                conn.select.return_value=('OK',[])
                conn.capabilities=[b'MOVE']
                conn.list.return_value=('OK',[] if reason=='missing_trash' else [b'() "/" "INBOX"',b'(\\Trash) "/" "Trash"'])
                conn.uid.side_effect=[('OK',[(b'1 BODY[HEADER]',b'Subject: Batch\r\n\r\n')]),('OK',[None])]
                box=mailbox.Mailbox('test@mail.ru','unused')
                with patch.object(box,'_with_imap',side_effect=lambda action:action(conn)):
                    with self.assertRaises(mailbox.MailError):box.trash_many('Trash' if reason=='already_in_trash' else 'INBOX',['1','2'])
                self.assertTrue(all(call.args[0]=='FETCH' for call in conn.uid.call_args_list))
                conn.expunge.assert_not_called()

    def test_bulk_trash_rejects_unsafe_uid_sets_before_connection(self):
        box=mailbox.Mailbox('test@mail.ru','unused')
        with patch.object(box,'_with_imap') as connect:
            for invalid in [[],['1']*51,['1','1'],['1:*'],['1,2'],[1],'1']:
                with self.assertRaises(mailbox.MailError):box.trash_many('INBOX',invalid)
        connect.assert_not_called()

    def test_large_message_move_and_trash_fetch_only_headers(self):
        for operation in ('move','trash'):
            with self.subTest(operation=operation):
                conn=MagicMock()
                conn.select.return_value=('OK',[])
                conn.response.return_value=('UIDVALIDITY',[b'42'])
                conn.capabilities=[b'IMAP4rev1',b'MOVE']
                conn.list.return_value=('OK',[b'(\\Trash) "/" "Trash"',b'() "/" "Archive"'])
                def command(name,uid,*args):
                    if name=='FETCH':
                        self.assertEqual(args,('(BODY.PEEK[HEADER])',))
                        return 'OK',[(b'1 (RFC822.SIZE 50000000 BODY[HEADER]',b'Subject: Large attachment\r\nMessage-ID: <large@example.org>\r\n\r\n')]
                    self.assertEqual(name,'MOVE')
                    return 'OK',[]
                conn.uid.side_effect=command
                box=mailbox.Mailbox('test@mail.ru','unused')
                with patch.object(box,'_with_imap',side_effect=lambda action:action(conn)):
                    identity=box.message_identity('INBOX','1')
                    if operation=='move': result=box.move('INBOX','1','Archive',identity)
                    else: result=box.trash('INBOX','1',identity)
                self.assertEqual(result['status'],'moved')
                self.assertEqual(result['destination'],'Archive' if operation=='move' else 'Trash')
                self.assertEqual([call.args[0] for call in conn.uid.call_args_list],['FETCH','FETCH','MOVE'])

    def test_header_identity_change_prevents_move(self):
        for changed in ('mailbox','message'):
            with self.subTest(changed=changed):
                conn=MagicMock()
                conn.select.return_value=('OK',[])
                conn.response.return_value=('UIDVALIDITY',[b'42'])
                conn.capabilities=[b'MOVE']
                conn.list.return_value=('OK',[b'() "/" "Archive"'])
                conn.uid.return_value=('OK',[(b'1 BODY[HEADER]',b'Subject: First\r\n\r\n')])
                box=mailbox.Mailbox('test@mail.ru','unused')
                with patch.object(box,'_with_imap',side_effect=lambda action:action(conn)):
                    identity=box.message_identity('INBOX','1')
                    if changed=='mailbox':conn.response.return_value=('UIDVALIDITY',[b'43'])
                    else:conn.uid.return_value=('OK',[(b'1 BODY[HEADER]',b'Subject: Changed\r\n\r\n')])
                    with self.assertRaises(mailbox.MailError):box.move('INBOX','1','Archive',identity)
                self.assertEqual([call.args[0] for call in conn.uid.call_args_list],['FETCH','FETCH'])

    def test_peek_and_size_limit(self):
        conn=MagicMock()
        conn.uid.side_effect=[('OK',[b'1 (RFC822.SIZE 20)']),('OK',[(b'1 BODY[]',b'Subject: Hello\r\n\r\nBody')])]
        self.assertEqual(mailbox.Mailbox._fetch(conn,'1')['Subject'],'Hello')
        self.assertEqual(conn.uid.call_args.args[2],'(BODY.PEEK[])')
        conn.uid.side_effect=[('OK',[b'1 (RFC822.SIZE 12000001)'])]
        with self.assertRaises(mailbox.MailError): mailbox.Mailbox._fetch(conn,'1')

    def test_missing_uid_is_not_reported_as_oversized_message(self):
        conn=MagicMock();conn.uid.return_value=('OK',[None])
        with self.assertRaisesRegex(mailbox.MailError,'Message not found') as error:
            mailbox.Mailbox._fetch(conn,'2174')
        self.assertNotIn('12 MB',str(error.exception))
        self.assertEqual(conn.uid.call_count,1)

    def test_folder_roundtrip_and_uid_validation(self):
        self.assertEqual(mailbox._folder_arg('Folder with "quote"'),'"Folder with \\"quote\\""')
        self.assertEqual(mailbox._display_folder('&BBoEPgRABDcEOAQ9BDA-'),'Корзина')
        conn=MagicMock()
        for uid in ['1:*','1,2','1\r\nSTORE']:
            with self.assertRaises(mailbox.MailError): mailbox.Mailbox._move(conn,'INBOX',uid,'Trash')
        conn.uid.assert_not_called()

    def test_no_mailbox_wide_expunge(self):
        conn=MagicMock();conn.select.return_value=('OK',[]);conn.capabilities=[b'IMAP4rev1']
        with self.assertRaises(mailbox.MailError): mailbox.Mailbox._move(conn,'INBOX','1','Trash')
        conn.expunge.assert_not_called();conn.uid.assert_not_called()
        conn.capabilities=[b'UIDPLUS'];conn.uid.return_value=('OK',[])
        mailbox.Mailbox._move(conn,'INBOX','1','Trash')
        self.assertEqual([x.args[0] for x in conn.uid.call_args_list],['COPY','STORE','EXPUNGE'])
        self.assertEqual(conn.uid.call_args.args,('EXPUNGE','1'))

    def test_mailbox_identity_change_refused(self):
        conn=MagicMock();conn.response.return_value=('UIDVALIDITY',[b'2'])
        msg=mailbox.email.message_from_bytes(b'Subject: s\r\n\r\nb',policy=mailbox.email.policy.default)
        with self.assertRaises(mailbox.MailError): mailbox.Mailbox._check_identity(conn,msg,{'uid_validity':'1','message_fingerprint':hashlib.sha256(msg.as_bytes()).hexdigest()})

    def test_partial_smtp_delivery_not_reported_as_complete(self):
        with patch('mailru_mcp.compose.smtplib.SMTP_SSL') as smtp, \
                patch.object(mailbox.Mailbox,'_with_imap',side_effect=mailbox.MailError('Sent unavailable')):
            smtp.return_value.send_message.return_value={'copy@example.org':(550,b'Rejected')}
            result=mailbox.Mailbox().send('t@example.org','s','b','copy@example.org')
        self.assertEqual(result['status'],'partially_sent')
        self.assertEqual(result['accepted_recipients'],['t@example.org'])
        self.assertEqual(result['refused_recipients'],['copy@example.org'])
        self.assertEqual(result['sent_copy_status'],'failed')
        self.assertTrue(result['do_not_retry'])


if __name__=='__main__': unittest.main()
