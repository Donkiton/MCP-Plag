import time
import unittest
from unittest.mock import MagicMock, patch
import test_server as base
mailbox,server=base.mailbox,base.server


class ReadBackendTests(unittest.TestCase):
    def setUp(self):
        self.box=mailbox.Mailbox('test@mail.ru','fake-password')
        self.conn=MagicMock()
        self.conn.select.return_value=('OK',[b'400'])
        self.conn.response.return_value=('UIDVALIDITY',[b'42'])
        self.conn.list.return_value=('OK',[b'() "/" "INBOX"',b'(\\Trash) "/" "Trash"'])
        self.folders={'INBOX':[str(i) for i in range(1,401)],'Trash':['1001','1002']}
        self.path='INBOX'; self.calls=[]; self.missing=set()
        def select(path,readonly=True):
            self.assertTrue(readonly)
            self.path=path.strip('"')
            return 'OK',[b'400']
        self.conn.select.side_effect=select
        def uid(command,*args):
            self.calls.append((command,args))
            if command=='SEARCH':
                uids=self.folders[self.path]
                if args[-1] in ('TEXT','SUBJECT','FROM','TO'):
                    self.assertEqual(self.conn.literal,'расчет "за\\август"'.encode())
                    if 'UID' in args:
                        boundary=int(args[args.index('UID')+1].split(':')[1])
                        uids=[v for v in uids if int(v)<=boundary]
                return 'OK',[' '.join(uids).encode()]
            self.assertEqual(command,'FETCH')
            self.assertEqual(args[1],'(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])')
            # Sequence numbers differ from UIDs; responses arrive out of requested order.
            return 'OK',[(f'7 (UID {v} BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] {{30}}'.encode(),
                f'Subject: Message {v}\r\nMessage-ID: <{v}@test>\r\n\r\n'.encode()) for v in reversed(args[0].split(',')) if v not in self.missing]+[b')']
        self.conn.uid.side_effect=uid
        self.connection=patch.object(self.box,'_connect',return_value=self.conn)
        self.connection.start();self.addCleanup(self.connection.stop)
        self.query='расчет "за\\август"'

    def test_listing_all_400_messages_pages_without_duplicates(self):
        found=[]; before=''
        while True:
            result=self.box.recent('INBOX',50,before,uid_validity='42',paged=True)
            self.assertEqual(result['total_count'],400)
            self.assertFalse(result['incomplete'])
            found += [item['uid'] for item in result['items']]
            if not result['has_more']:break
            before=result['next_before_uid']
        self.assertEqual(found,[str(i) for i in range(400,0,-1)])
        self.assertEqual(sum(c[0]=='FETCH' for c in self.calls),8)
        self.conn.shutdown.assert_called();self.conn.logout.assert_not_called()

    def test_listing_uidvalidity_changed_rejects_before_fetch(self):
        with self.assertRaisesRegex(mailbox.MailError,'UIDVALIDITY'):
            self.box.recent('INBOX',50,'300','41',paged=True)
        self.conn.uid.assert_not_called()

    def test_missing_headers_are_explicit(self):
        self.missing={'400'}
        result=self.box.recent('INBOX',50,paged=True)
        self.assertEqual(result['missing_count'],1);self.assertTrue(result['incomplete'])
        self.assertEqual(result['next_before_uid'],'351')

    def test_search_covers_old_messages_and_all_folders_with_cursor(self):
        cursor='';found=[]
        while True:
            result=self.box.search(self.query,limit=50,scan_per_folder=1,cursor=cursor)
            found += [(item['folder'],item['uid']) for item in result['messages']]
            if not result['has_more']:
                self.assertFalse(result['incomplete']);break
            self.assertTrue(result['incomplete']);cursor=result['next_cursor']
        self.assertEqual(len(found),402);self.assertEqual(len(set(found)),402)
        self.assertIn(('INBOX','1'),found);self.assertIn(('Trash','1001'),found)
        self.assertTrue(all(args[0:2]==('CHARSET','UTF-8') for command,args in self.calls if command=='SEARCH'))

    def test_search_exact_final_page_does_not_claim_incomplete(self):
        self.folders['INBOX']=['1','2']
        result=self.box.search(self.query,folder='INBOX',limit=2)
        self.assertFalse(result['has_more']);self.assertFalse(result['incomplete'])

    def test_cursor_bound_to_query_folder_and_account(self):
        cursor=self.box.search(self.query,folder='INBOX',limit=1)['next_cursor']
        for query,folder in [('other','INBOX'),(self.query,'Trash')]:
            with self.assertRaisesRegex(mailbox.MailError,'cursor'):
                self.box.search(query,folder,cursor=cursor)
        other=mailbox.Mailbox('other@mail.ru','fake-password')
        with self.assertRaisesRegex(mailbox.MailError,'cursor'):other.search(self.query,'INBOX',cursor=cursor)
        with self.assertRaisesRegex(mailbox.MailError,'cursor'):self.box.search(self.query,'INBOX',cursor=cursor[:-1]+'x')

    def test_search_cursor_uidvalidity_changed(self):
        cursor=self.box.search(self.query,folder='INBOX',limit=1)['next_cursor']
        self.conn.response.return_value=('UIDVALIDITY',[b'99'])
        with self.assertRaisesRegex(mailbox.MailError,'UIDVALIDITY'):self.box.search(self.query,'INBOX',cursor=cursor)

    def test_timeout_returns_continuation_without_false_no_match(self):
        original=self.conn.uid.side_effect
        self.conn.uid.side_effect=lambda command,*args: (_ for _ in ()).throw(TimeoutError()) if command=='SEARCH' else original(command,*args)
        result=self.box.search(self.query,folder='INBOX')
        self.assertTrue(result['interrupted']);self.assertTrue(result['incomplete']);self.assertTrue(result['next_cursor'])
        self.conn.uid.side_effect=original
        resumed=self.box.search(self.query,folder='INBOX',cursor=result['next_cursor'])
        self.assertEqual(resumed['messages'][0]['uid'],'400')

    def test_transport_error_after_success_keeps_next_folder_position(self):
        self.folders['INBOX']=['400']
        original=self.conn.uid.side_effect
        self.conn.uid.side_effect=lambda command,*args: (_ for _ in ()).throw(OSError()) if self.path=='Trash' else original(command,*args)
        result=self.box.search(self.query)
        self.assertEqual(len(result['messages']),1);self.assertTrue(result['interrupted'])
        self.conn.uid.side_effect=original
        resumed=self.box.search(self.query,cursor=result['next_cursor'])
        self.assertEqual([item['folder'] for item in resumed['messages']],['Trash','Trash'])

    def test_empty_folder_and_no_match_are_complete(self):
        self.folders['INBOX']=[]
        self.assertEqual(self.box.recent('INBOX',paged=True)['total_count'],0)
        result=self.box.search(self.query,'INBOX')
        self.assertEqual(result['messages'],[]);self.assertFalse(result['incomplete'])

    def test_before_uid_one_never_searches_reversed_range(self):
        self.assertEqual(self.box._search_uids(self.conn,self.query,'1'),[])
        self.conn.uid.assert_not_called()

    def test_unicode_search_unions_explicit_headers_when_text_omits_html_mail(self):
        matches={'TEXT':b'5','SUBJECT':b'9','FROM':b'10','TO':b'5'}
        self.conn.uid.side_effect=lambda command,*args:('OK',[matches[args[-1]]])
        result=self.box._search_uids(self.conn,self.query)
        self.assertEqual(result,['10','9','5'])
        self.assertEqual(self.conn.uid.call_count,4)
        self.assertEqual(self.conn.literal,self.query.encode('utf-8'))

    def test_ascii_header_body_or_preserves_quotes_and_bounds(self):
        self.conn.uid.side_effect=None;self.conn.uid.return_value=('OK',[b'8 9'])
        result=self.box._search_uids(self.conn,'hello "world" \\',before='10')
        quoted=b'"hello \\"world\\" \\\\"'
        self.conn.uid.assert_called_once_with('SEARCH','CHARSET','UTF-8','UID','1:9','OR','SUBJECT',quoted,'OR','FROM',quoted,'OR','TO',quoted,'TEXT',quoted)
        self.assertEqual(result,['9','8'])

    def test_deadline_sets_socket_timeout_or_exits(self):
        self.box._read_budget(self.conn,time.monotonic()+3)
        self.assertLessEqual(self.conn.sock.settimeout.call_args.args[0],3)
        with self.assertRaises(TimeoutError):self.box._read_budget(self.conn,time.monotonic()-1)

    def test_unsupported_utf8_search_is_explicit_failure(self):
        self.conn.uid.return_value=('NO',[b'unsupported'])
        self.conn.uid.side_effect=None
        with self.assertRaisesRegex(mailbox.MailError,'UTF-8'):self.box.search(self.query,'INBOX')

    def test_invalid_before_uid_rejected_without_connecting(self):
        for value in ['0','1:*','1,2','-2']:
            with self.assertRaises(mailbox.MailError):self.box.recent(before_uid=value)
        self.box._connect.assert_not_called()


class ReadSchemaTests(unittest.TestCase):
    def test_pagination_schemas_reject_wrong_cursor_and_uid(self):
        schemas={tool['name']:tool['inputSchema'] for tool in server.TOOLS}
        self.assertTrue(server.valid_arguments(schemas['list_messages'],{'folder':'Trash','before_uid':'42','uid_validity':'7'}))
        self.assertFalse(server.valid_arguments(schemas['list_messages'],{'before_uid':'1:*'}))
        self.assertFalse(server.valid_arguments(schemas['search_messages'],{'query':'test','cursor':'x'*65537}))
