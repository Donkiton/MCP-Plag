import json,threading,unittest
from unittest.mock import MagicMock,patch
import test_server as base
mailbox,server=base.mailbox,base.server


def expand(value):
    out=set()
    for part in value.split(','):
        bounds=part.split(':');first=int(bounds[0]);last=int(bounds[-1])
        out.update(str(i) for i in range(first,last+1))
    return out


class BulkBackendTests(unittest.TestCase):
    def setUp(self):
        self.box=mailbox.Mailbox('fixture@mail.ru','fake')
        self.conn=MagicMock();self.conn.capabilities=[b'MOVE',b'UIDPLUS']
        self.conn.select.return_value=('OK',[])
        self.conn.list.return_value=('OK',[b'() "/" "Sent"',b'(\\Trash) "/" "Trash"'])
        self.source=set();self.moved=set();self.arrivals=set();self.commands=[]
        def uid(name,*args):
            self.commands.append((name,args))
            if name=='SEARCH':return 'OK',[' '.join(sorted(self.source,key=int)).encode()]
            if name=='MOVE':
                selected=expand(args[0]);self.assertTrue(selected.issubset(self.source))
                self.source-=selected;self.moved|=selected;self.source|=self.arrivals
                return 'OK',[]
            if name=='FETCH':return 'OK',[(b'1 BODY[HEADER]',b'Subject: Fixture\r\n\r\n')]
            raise AssertionError(name)
        self.conn.uid.side_effect=uid
        self.connection=patch.object(self.box,'_with_imap',side_effect=lambda f:f(self.conn))
        self.connection.start();self.addCleanup(self.connection.stop)

    def test_10000_messages_one_request_with_internal_batches(self):
        self.source={str(i) for i in range(1,10001)};progress=[]
        result=self.box.trash_all('Sent',progress=progress.append)
        self.assertEqual(result['count'],10000);self.assertEqual(result['remaining_count'],0)
        self.assertTrue(result['recoverable']);self.assertEqual(len(self.moved),10000)
        self.assertEqual(sum(name=='MOVE' for name,args in self.commands),10)
        self.assertFalse(any(name=='FETCH' for name,args in self.commands))
        self.assertEqual(progress[-1]['moved_count'],10000)
        self.conn.expunge.assert_not_called()

    def test_snapshot_gaps_and_new_arrivals_never_targeted(self):
        self.source={'1','2','9','11'};self.arrivals={'12','20'}
        result=self.box.trash_all('Sent')
        self.assertEqual(self.moved,{'1','2','9','11'})
        self.assertEqual(self.source,{'12','20'});self.assertEqual(result['remaining_count'],2)
        self.assertEqual(next(args[0] for name,args in self.commands if name=='MOVE'),'1:2,9,11')

    def test_selected_large_subset_does_not_move_other_mail(self):
        self.source={str(i) for i in range(1,10001)}
        selected=[str(i) for i in range(1,10001,2)]
        result=self.box.trash_many('Sent',selected)
        self.assertEqual(result['count'],5000);self.assertEqual(self.moved,set(selected))
        self.assertEqual(len(self.source),5000)

    def test_missing_large_selected_uid_refuses_before_move(self):
        self.source={str(i) for i in range(1,60)}
        with self.assertRaises(mailbox.MailError):self.box.trash_many('Sent',[str(i) for i in range(1,61)])
        self.assertFalse(any(name=='MOVE' for name,args in self.commands))

    def test_empty_folder_completes_and_trash_itself_is_protected(self):
        self.assertEqual(self.box.trash_all('Sent')['count'],0)
        with self.assertRaises(mailbox.MailError):self.box.trash_all('Trash')
        self.assertFalse(self.moved)

    def test_server_ack_without_removing_source_is_not_claimed_as_verified(self):
        self.source={'1'}
        self.conn.uid.side_effect=lambda name,*args:('OK',[b'1']) if name=='SEARCH' else ('OK',[])
        result=self.box.trash_all('Sent')
        self.assertEqual(result['status'],'move_unverified');self.assertEqual(result['count'],0)

    def test_uid_chunks_keep_protocol_commands_bounded(self):
        source={str(i) for i in range(1,30000,2)}
        chunks=list(self.box._uid_chunks(source))
        self.assertTrue(all(len(value)<=4000 and count<=1000 for value,count in chunks))
        self.assertEqual(set().union(*(expand(value) for value,count in chunks)),source)


class BackgroundJobTests(unittest.TestCase):
    def setUp(self):
        server.init_db()
        with server.db() as c:c.execute('DELETE FROM pending')

    def test_job_deduplicates_and_preserves_account_and_progress(self):
        with patch.object(server,'spawn_mail_job') as thread:
            job=server.start_mail_job('trash_all_messages',{'folder':'Sent'},'selected-account')
            repeated=server.start_mail_job('trash_all_messages',{'folder':'Sent'},'selected-account')
        self.assertEqual(job['action_id'],repeated['action_id']);self.assertEqual(thread.call_count,1)
        box=MagicMock()
        def trash(folder,progress):
            self.assertEqual(folder,'Sent');progress({'captured_count':10000,'moved_count':1000})
            return {'status':'moved','count':10000}
        box.trash_all.side_effect=trash
        with patch.object(server,'account_mailbox',return_value=box) as selected:
            server.run_mail_job(job['action_id'],'trash_all_messages',{'folder':'Sent'},'selected-account')
        selected.assert_called_once_with('selected-account')
        with server.db() as c:row=c.execute('SELECT status,result FROM pending').fetchone()
        self.assertEqual(row['status'],'completed');self.assertEqual(json.loads(row['result'])['count'],10000)

    def test_failed_job_preserves_acknowledged_progress_and_never_retries(self):
        with patch.object(server,'spawn_mail_job'):job=server.start_mail_job('trash_all_messages',{'folder':'Sent'},'account')
        box=MagicMock()
        def fail(folder,progress):progress({'moved_count':1000,'current_chunk_count':1000});raise TimeoutError()
        box.trash_all.side_effect=fail
        with patch.object(server,'account_mailbox',return_value=box):server.run_mail_job(job['action_id'],'trash_all_messages',{'folder':'Sent'},'account')
        box.trash_all.assert_called_once()
        with server.db() as c:row=c.execute('SELECT status,result FROM pending').fetchone()
        self.assertEqual(row['status'],'failed_uncertain');self.assertEqual(json.loads(row['result'])['moved_count'],1000)

    def test_restart_marks_active_job_interrupted_without_execution(self):
        with patch.object(server,'spawn_mail_job'):job=server.start_mail_job('trash_all_messages',{'folder':'Sent'},'account')
        with patch.object(server,'account_mailbox') as box:server.interrupt_old_mail_jobs()
        box.assert_not_called()
        with server.db() as c:row=c.execute('SELECT status FROM pending').fetchone()
        self.assertEqual(row['status'],'interrupted')

    def test_workers_use_a_bounded_pool_instead_of_one_thread_per_account(self):
        with patch.object(server._JOB_EXECUTOR,'submit') as submit:
            server.spawn_mail_job('job','trash_all_messages',{'folder':'Sent'},'account')
        submit.assert_called_once_with(server.run_mail_job,'job','trash_all_messages',{'folder':'Sent'},'account')

    def test_worker_start_failure_does_not_leave_a_running_job(self):
        with patch.object(server,'spawn_mail_job',side_effect=RuntimeError('test')):
            with self.assertRaises(server.MailError):server.start_mail_job('trash_all_messages',{'folder':'Sent'},'account')
        with server.db() as c:row=c.execute('SELECT status,result FROM pending').fetchone()
        self.assertEqual(row['status'],'failed_uncertain');self.assertEqual(json.loads(row['result'])['status'],'not_started')

    def test_new_schemas_accept_large_uid_list_and_multiple_newsletters(self):
        tools={value['name']:value for value in server.TOOLS}
        self.assertTrue(server.valid_arguments(tools['trash_messages']['inputSchema'],{'folder':'Sent','uids':[str(i) for i in range(1,10001)]}))
        self.assertTrue(server.valid_arguments(tools['unsubscribe_senders']['inputSchema'],{'messages':[{'folder':'INBOX','uid':'1'},{'folder':'Trash','uid':'2'}]}))
        self.assertFalse(server.valid_arguments(tools['unsubscribe_senders']['inputSchema'],{'messages':[{'folder':'INBOX','uid':'1','url':'https://evil.test'}]}))
        for name in ['trash_all_messages','unsubscribe_senders']:
            self.assertFalse(tools[name]['annotations']['readOnlyHint']);self.assertTrue(tools[name]['annotations']['destructiveHint'])
            self.assertEqual(tools[name]['securitySchemes'][0]['scopes'],['mail.write'])
