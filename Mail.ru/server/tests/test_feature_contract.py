import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from mailru_mcp import server


class ServerContractTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.old_db=server.DB_PATH
        server.DB_PATH=os.path.join(self.directory.name,'state.sqlite3')
        server.init_db()
        self.tools={entry['name']:entry for entry in server.TOOLS}

    def tearDown(self):
        server.DB_PATH=self.old_db
        self.directory.cleanup()

    def valid(self,name,args):
        return server.valid_arguments(self.tools[name]['inputSchema'],args)

    def draft_args(self,**extra):
        return {**dict(folder='Drafts',uid='19',uid_validity='12',message_fingerprint='a'*64),**extra}

    def test_existing_tools_and_added_commands_present_once(self):
        old={'list_folders','list_messages','search_messages','read_message','read_attachment',
             'create_draft','prepare_send','prepare_move','prepare_delete','trash_messages',
             'empty_folder','delete_message_permanently','create_folder','rename_folder',
             'delete_folder','action_status','trash_all_messages','get_unsubscribe_options',
             'unsubscribe_senders','get_profile'}
        self.assertTrue(old.issubset(self.tools))
        self.assertEqual(len(self.tools),len(server.TOOLS))
        self.assertEqual(31,len(server.TOOLS))
        self.assertFalse(any('watch' in name or 'history' in name or 'label' in name for name in self.tools))

    def test_recipient_and_attachment_contract(self):
        args={'to':'self@example.com','subject':'test','body':'plain','bcc':'hidden@example.com',
              'html_body':'<p>plain</p>','attachments':[{'filename':'тест.txt','content_base64':'eA=='}]}
        self.assertTrue(self.valid('prepare_send',args))
        args['attachments']=[{'folder':'INBOX','uid':'12','index':2,'uid_validity':'1'}]
        self.assertTrue(self.valid('create_draft',args))
        args['attachments'][0]['path']='/etc/passwd'
        self.assertFalse(self.valid('prepare_send',args))

    def test_send_sources_require_current_folder_version(self):
        reply={'folder':'INBOX','uid':'1','body':'Reply'}
        self.assertFalse(self.valid('reply_message',reply))
        reply['uid_validity']='8'
        self.assertTrue(self.valid('reply_message',reply))
        forward={'folder':'INBOX','uid':'1','to':'self@example.com'}
        self.assertFalse(self.valid('forward_message',forward))
        forward['uid_validity']='8'
        self.assertTrue(self.valid('forward_message',forward))
        args={'to':'self@example.com','subject':'test','body':'text',
              'attachments':[{'folder':'INBOX','uid':'1','index':2}]}
        self.assertFalse(self.valid('prepare_send',args))
        args['attachments'][0]['uid_validity']='8'
        self.assertTrue(self.valid('prepare_send',args))
        args['attachments'][0]['filename']='mixed.txt'
        self.assertFalse(self.valid('prepare_send',args))

    def test_bool_is_not_int_and_write_guards_required(self):
        args={'folder':'INBOX','uids':['1'],'read':False,'uid_validity':'7'}
        self.assertTrue(self.valid('mark_messages_read',args))
        args['read']=0
        self.assertFalse(self.valid('mark_messages_read',args))
        del args['read']
        self.assertFalse(self.valid('mark_messages_read',args))
        self.assertFalse(self.valid('send_draft',{'folder':'Drafts','uid':'19'}))
        self.assertTrue(self.valid('send_draft',self.draft_args()))
        self.assertFalse(self.valid('send_draft',self.draft_args(message_fingerprint='bad')))

    def test_batch_uids_cannot_select_ranges_or_duplicates(self):
        args={'folder':'INBOX','uids':['1','2'],'target':'Archive','uid_validity':'7'}
        self.assertTrue(self.valid('move_messages',args))
        for bad in (['1:100'],['1','1'],['0'],['-1'],[],['1,2']):
            args['uids']=bad
            self.assertFalse(self.valid('move_messages',args),bad)

    def test_read_batch_and_legacy_schema_bounds(self):
        self.assertTrue(self.valid('read_messages',{'messages':[{'folder':'INBOX','uid':'1'}]}))
        self.assertFalse(self.valid('read_messages',{'messages':[{'folder':'INBOX','uid':'1'}]*21}))
        self.assertFalse(self.valid('list_messages',{'limit':True}))
        self.assertFalse(self.valid('list_messages',{'limit':51}))
        self.assertTrue(self.valid('unsubscribe_senders',{'messages':[{'folder':'INBOX','uid':'2'}]}))
        self.assertFalse(self.valid('unsubscribe_senders',{'messages':[{'folder':'INBOX','uid':'2','url':'https://example.com'}]}))

    def test_mail_write_scope_annotations_and_external_sending(self):
        for name in ('send_draft','reply_message','forward_message','mark_messages_read','move_messages','archive_messages','update_draft'):
            self.assertFalse(self.tools[name]['annotations']['readOnlyHint'])
            self.assertEqual(['mail.write'],self.tools[name]['securitySchemes'][0]['scopes'])
        for name in ('send_draft','reply_message','forward_message'):
            self.assertTrue(self.tools[name]['annotations']['openWorldHint'])
        for name in ('read_thread','read_messages','read_draft','list_drafts'):
            self.assertTrue(self.tools[name]['annotations']['readOnlyHint'])

    def test_draft_receipt_survives_db_reinitialization(self):
        class Box:
            username='self@example.com'
            calls=0
            def send_draft(self,**args):
                self.calls+=1
                return {'status':'sent','accepted_recipients':['self@example.com']}
        box=Box()
        first=server.send_saved_draft(box,self.draft_args(),'account-a')
        server.init_db()
        second=server.send_saved_draft(box,self.draft_args(),'account-a')
        self.assertEqual('sent',first['status'])
        self.assertEqual('delivery_already_attempted',second['status'])
        self.assertEqual(1,box.calls)
        server.send_saved_draft(box,self.draft_args(),'account-b')
        self.assertEqual(2,box.calls)

    def test_uncertain_transport_never_repeats_saved_version(self):
        class Box:
            username='self@example.com'
            calls=0
            def send_draft(self,**args):
                self.calls+=1
                raise TimeoutError('connection lost after DATA')
        box=Box()
        with self.assertRaises(server.MailError):
            server.send_saved_draft(box,self.draft_args(),'account-a')
        result=server.send_saved_draft(box,self.draft_args(),'account-a')
        self.assertEqual('failed_or_uncertain',result['delivery_status'])
        self.assertEqual(1,box.calls)

    def test_new_version_has_separate_guard(self):
        class Box:
            username='self@example.com'
            calls=0
            def send_draft(self,**args):
                self.calls+=1
                return {'status':'sent'}
        box=Box()
        server.send_saved_draft(box,self.draft_args(),'account-a')
        changed=self.draft_args()
        changed['message_fingerprint']='b'*64
        server.send_saved_draft(box,changed,'account-a')
        self.assertEqual(2,box.calls)

    def test_large_batch_dispatches_one_background_job(self):
        args={'folder':'INBOX','uids':[str(n) for n in range(1,102)],'target':'Archive','uid_validity':'1'}
        with patch.object(server,'account_mailbox'),patch.object(server,'start_mail_job',return_value={'status':'started'}) as job:
            self.assertEqual('started',server.execute_tool('move_messages',args,'owner')['status'])
            job.assert_called_once_with('move_messages',args,'owner')

    def test_restart_interrupts_new_job_kinds_only(self):
        with server.db() as c:
            for kind in server.JOB_KINDS | {'unrelated'}:
                c.execute('INSERT INTO pending(id_hash,kind,args,created,expires,status,result,account_id) VALUES(?,?,?,?,?,?,?,?)',
                          (kind,kind,'{}',1,2,'running','{}','owner'))
        server.interrupt_old_mail_jobs()
        with server.db() as c:
            rows={row['kind']:row['status'] for row in c.execute('SELECT kind,status FROM pending')}
        for kind in server.JOB_KINDS:self.assertEqual('interrupted',rows[kind])
        self.assertEqual('running',rows['unrelated'])

    def test_job_status_cannot_cross_accounts(self):
        with server.db() as c:
            c.execute('INSERT INTO pending(id_hash,kind,args,created,expires,status,result,account_id) VALUES(?,?,?,?,?,?,?,?)',
                      (server.digest('opaque'),'move_messages','{}',1,2,'completed','{}','owner'))
        with patch.object(server,'MULTI_ACCOUNT',True),patch.object(server,'account_mailbox'):
            self.assertEqual('not_found_or_expired',server.execute_tool('action_status',{'action_id':'opaque'},'other')['status'])

    def test_http_attachment_budget_does_not_expand_login_forms(self):
        handler=object.__new__(server.Handler)
        handler.headers={'Content-Length':'250001'}
        handler.rfile=io.BytesIO(b'x'*250001)
        handler.path='/authorize'
        with self.assertRaises(ValueError):handler._body()
        handler.path='/mcp'
        handler.rfile=io.BytesIO(b'x'*250001)
        self.assertEqual(250001,len(handler._body()))
        handler.headers={'Content-Length':'12000001'}
        with self.assertRaises(ValueError):handler._body()


if __name__=='__main__':unittest.main()
