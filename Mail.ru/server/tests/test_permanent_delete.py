import hashlib,json,unittest
from unittest.mock import MagicMock,patch
import test_server as base
mailbox,server=base.mailbox,base.server


class PermanentDeleteBackendTests(unittest.TestCase):
    def setUp(self):
        self.box=mailbox.Mailbox('test@mail.ru','unused')
        self.conn=MagicMock();self.conn.select.return_value=('OK',[])
        self.conn.capabilities=[b'UIDPLUS']
        self.headers=b'Subject: Large test\r\nMessage-ID: <unique@test.invalid>\r\n\r\n'
        self.fetch=('OK',[(b'1 (RFC822.SIZE 50000000 BODY[HEADER]',self.headers)])
        self.connect=patch.object(self.box,'_with_imap',side_effect=lambda action:action(self.conn))
        self.connect.start();self.addCleanup(self.connect.stop)

    def test_any_folder_only_one_uid_and_headers_without_move(self):
        for folder in ['INBOX','Trash','Spam','Custom']:
            self.conn.uid.reset_mock();self.conn.uid.side_effect=[self.fetch,('OK',[]),('OK',[]),('OK',[b''])]
            result=self.box.delete_message_permanently(folder,'7')
            self.assertEqual(result['status'],'permanently_deleted');self.assertFalse(result['recoverable'])
            self.assertEqual([call.args for call in self.conn.uid.call_args_list],
                [('FETCH','7','(BODY.PEEK[HEADER])'),('STORE','7','+FLAGS.SILENT','(\\Deleted)'),
                 ('EXPUNGE','7'),('SEARCH',None,'UID','7')])
        self.conn.expunge.assert_not_called()

    def test_invalid_uid_rejected_before_connection(self):
        with patch.object(self.box,'_with_imap') as connect:
            for uid in ['0','01','1:*','1,2','1\r\nSTORE','',7,True,'9'*21]:
                with self.assertRaises(mailbox.MailError):self.box.delete_message_permanently('INBOX',uid)
        connect.assert_not_called()

    def test_missing_message_never_mutates(self):
        self.conn.uid.return_value=('OK',[None])
        with self.assertRaisesRegex(mailbox.MailError,'Message not found'):self.box.delete_message_permanently('INBOX','7')
        self.assertEqual(self.conn.uid.call_count,1)

    def test_changed_message_or_uidvalidity_never_mutates(self):
        expected={'uid_validity':'42','message_fingerprint':hashlib.sha256(mailbox.email.message_from_bytes(self.headers,policy=mailbox.email.policy.default).as_bytes()).hexdigest()}
        for changed in ['message','mailbox']:
            self.conn.uid.reset_mock();self.conn.uid.return_value=self.fetch
            self.conn.response.return_value=('UIDVALIDITY',[b'43' if changed=='mailbox' else b'42'])
            identity=dict(expected)
            if changed=='message':identity['message_fingerprint']='old-message'
            with self.assertRaisesRegex(mailbox.MailError,'identity changed'):self.box.delete_message_permanently('INBOX','7',identity)
            self.assertEqual(self.conn.uid.call_count,1)

    def test_server_without_uidplus_does_not_mark_or_expunge(self):
        self.conn.capabilities=[b'MOVE'];self.conn.uid.return_value=self.fetch
        with self.assertRaisesRegex(mailbox.MailError,'UIDPLUS'):self.box.delete_message_permanently('INBOX','7')
        self.assertEqual(self.conn.uid.call_count,1);self.conn.expunge.assert_not_called()

    def test_failed_mutation_is_not_retried(self):
        for responses in [[self.fetch,('NO',[])],[self.fetch,('OK',[]),('NO',[])]]:
            self.conn.uid.reset_mock();self.conn.uid.side_effect=responses
            with self.assertRaises(mailbox.MailError):self.box.delete_message_permanently('Trash','7')
            self.assertEqual(self.conn.uid.call_count,len(responses))

    def test_unverified_result_is_not_reported_as_deleted(self):
        for verification in [('OK',[b'7']),('NO',[]),('OK',[None])]:
            self.conn.uid.side_effect=[self.fetch,('OK',[]),('OK',[]),verification]
            self.assertEqual(self.box.delete_message_permanently('Spam','7')['status'],'deletion_unverified')


class PermanentDeleteToolTests(base.ServerIntegrationTests):
    test_auth_and_tool_list=None
    test_send_executes_without_confirmation=None
    def rpc(self,token,args):
        _,_,raw=self.call('POST','/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call',
             'params':{'name':'delete_message_permanently','arguments':args}}, {'Authorization':'Bearer '+token})
        return json.loads(raw)['result']

    def test_scope_and_single_uid_schema(self):
        _,readonly=self.make_oauth(scope='mail.read')
        self.assertEqual(self.rpc(readonly['access_token'],{'folder':'INBOX','uid':'7'})['content'][0]['text'],'Insufficient OAuth scope')
        _,writable=self.make_oauth()
        for uid in ['1:*','1,2','0','01',7]:
            self.assertEqual(self.rpc(writable['access_token'],{'folder':'INBOX','uid':uid})['content'][0]['text'],'Invalid arguments')
        tool=next(item for item in server.TOOLS if item['name']=='delete_message_permanently')
        self.assertTrue(tool['annotations']['destructiveHint']);self.assertFalse(tool['annotations']['readOnlyHint'])

    def test_direct_execution_uses_identity_and_no_confirmation_form(self):
        _,token=self.make_oauth()
        identity={'uid_validity':'42','message_fingerprint':'test-header-identity'}
        with patch.object(server.Mailbox,'message_identity',return_value=identity),patch.object(server.Mailbox,'delete_message_permanently',return_value={'status':'permanently_deleted','recoverable':False}) as remove:
            result=self.rpc(token['access_token'],{'folder':'INBOX','uid':'7'})
        self.assertFalse(result['isError']);self.assertNotIn('confirmation_url',result['structuredContent'])
        remove.assert_called_once_with('INBOX','7',expected_identity=identity)


if __name__=='__main__':unittest.main()
