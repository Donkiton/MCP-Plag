import unittest
from unittest.mock import MagicMock, patch
import test_server as base
mailbox,server=base.mailbox,base.server


class FolderBackendTests(unittest.TestCase):
    def setUp(self):
        self.box=mailbox.Mailbox('test@mail.ru','unused')
        self.conn=MagicMock()
        self.conn.capabilities=[b'UIDPLUS']
        self.conn.select.return_value=('OK',[])
        self.conn.list.return_value=('OK',[b'() "/" "INBOX"',b'(\\Trash) "/" "Trash"',
            b'(\\Junk) "/" "Spam"',b'(\\Sent) "/" "Sent"',b'() "/" "Custom"'])
        self.conn.create.return_value=self.conn.rename.return_value=self.conn.delete.return_value=('OK',[])
        self.connect=patch.object(self.box,'_with_imap',side_effect=lambda action:action(self.conn))
        self.connect.start();self.addCleanup(self.connect.stop)

    def test_unicode_name_roundtrip_and_ampersand(self):
        for name in ['Работа и счета','A & B','Клиенты & partners','Folder with "quote"']:
            self.assertEqual(mailbox._display_folder(mailbox._encode_folder(name)),name)
        wire=mailbox._encode_folder('Работа и счета')
        result=self.box.create_folder('Работа и счета')
        self.assertEqual(result['folder'],wire)
        self.conn.create.assert_called_once_with(mailbox._folder_arg(wire))

    def test_invalid_and_reserved_names_never_mutate(self):
        for name in ['', ' ', ' leading', 'trailing ', 'a\r\nb', 'a\x00b', 'Parent/Child', 'INBOX', 'Корзина', 'Spam', 'Custom']:
            with self.subTest(name=name),self.assertRaises(mailbox.MailError):self.box.create_folder(name)
        self.conn.create.assert_not_called()

    def test_long_encoded_name_and_custom_delimiter_rejected(self):
        self.conn.list.return_value=('OK',[b'() "." "Custom"'])
        for name in ['a.b','Я'*255]:
            with self.assertRaises(mailbox.MailError):self.box.create_folder(name)
        self.conn.create.assert_not_called()

    def test_rename_custom_preserves_messages_without_fetch_or_delete(self):
        result=self.box.rename_folder('Custom','Новая папка')
        self.assertEqual(result['status'],'folder_renamed')
        self.conn.rename.assert_called_once_with('"Custom"',mailbox._folder_arg(mailbox._encode_folder('Новая папка')))
        self.conn.uid.assert_not_called();self.conn.delete.assert_not_called()

    def test_system_and_unknown_folders_cannot_be_renamed_or_deleted(self):
        for name in ['INBOX','Trash','Spam','Sent','NotFound']:
            for action in [lambda:self.box.rename_folder(name,'New'),lambda:self.box.delete_folder(name)]:
                with self.assertRaises(mailbox.MailError):action()
        self.conn.rename.assert_not_called();self.conn.delete.assert_not_called();self.conn.uid.assert_not_called()

    def test_rename_child_keeps_its_parent_and_checks_full_target_collision(self):
        self.conn.list.return_value=('OK',[b'() "/" "Parent/Child"',b'() "/" "New"',b'() "/" "Parent/Taken"'])
        result=self.box.rename_folder('Parent/Child','New')
        self.assertEqual(result['folder'],'Parent/New')
        self.conn.rename.assert_called_once_with('"Parent/Child"','"Parent/New"')
        with self.assertRaises(mailbox.MailError):self.box.rename_folder('Parent/Child','Taken')
        self.assertEqual(self.conn.rename.call_count,1)

    def test_subfolders_protect_against_implicit_subtree_changes(self):
        self.conn.list.return_value=('OK',[b'() "/" "Custom"',b'() "/" "Custom/Child"'])
        for action in [lambda:self.box.rename_folder('Custom','New'),lambda:self.box.delete_folder('Custom')]:
            with self.assertRaisesRegex(mailbox.MailError,'subfolders'):action()
        self.conn.rename.assert_not_called();self.conn.delete.assert_not_called()

    def test_delete_only_empty_custom_folder(self):
        self.conn.uid.return_value=('OK',[b''])
        self.assertEqual(self.box.delete_folder('Custom')['messages_deleted'],0)
        self.conn.delete.assert_called_once_with('"Custom"')
        self.conn.uid.assert_called_once_with('SEARCH',None,'ALL')

    def test_nonempty_or_uninspectable_folder_is_not_deleted(self):
        for response in [('OK',[b'1 2']),('NO',[]),('OK',[None])]:
            self.conn.uid.return_value=response
            with self.assertRaises(mailbox.MailError):self.box.delete_folder('Custom')
        self.conn.delete.assert_not_called()

    def test_folder_command_failure_is_not_success(self):
        self.conn.uid.return_value=('OK',[b''])
        for command,action in [('create',lambda:self.box.create_folder('New')),
                               ('rename',lambda:self.box.rename_folder('Custom','New')),
                               ('delete',lambda:self.box.delete_folder('Custom'))]:
            getattr(self.conn,command).return_value=('NO',[])
            with self.assertRaises(mailbox.MailError):action()

    def test_trash_and_spam_purge_exact_snapshot_and_keep_new_arrivals(self):
        for folder in ['Trash','Spam']:
            with self.subTest(folder=folder):
                self.conn.uid.reset_mock()
                self.conn.uid.side_effect=[('OK',[b'1 2']),('OK',[]),('OK',[]),('OK',[b'3'])]
                result=self.box.empty_folder(folder)
                self.assertEqual(result['removed_count'],2)
                self.assertEqual(result['remaining_count'],1)
                self.assertFalse(result['recoverable'])
                self.assertEqual([call.args for call in self.conn.uid.call_args_list],
                    [('SEARCH',None,'ALL'),('STORE','1,2','+FLAGS.SILENT','(\\Deleted)'),('EXPUNGE','1,2'),('SEARCH',None,'ALL')])
        self.conn.expunge.assert_not_called()

    def test_custom_and_other_system_folders_cannot_be_emptied(self):
        self.conn.list.return_value[1].append(b'() "/" "My spam archive"')
        for folder in ['INBOX','Sent','Custom','My spam archive','missing']:
            with self.assertRaises(mailbox.MailError):self.box.empty_folder(folder)
        self.conn.select.assert_not_called();self.conn.uid.assert_not_called()

    def test_mailru_special_folders_without_flags(self):
        self.conn.list.return_value=('OK',[b'() "/" "&BBoEPgRABDcEOAQ9BDA-"',b'() "/" "&BCEEPwQwBDw-"'])
        for folder in ['&BBoEPgRABDcEOAQ9BDA-','&BCEEPwQwBDw-']:
            self.conn.uid.side_effect=[('OK',[b'']),('OK',[b''])]
            self.assertEqual(self.box.empty_folder(folder)['removed_count'],0)

    def test_missing_uidplus_never_marks_deleted(self):
        self.conn.capabilities=[b'MOVE']
        self.conn.uid.return_value=('OK',[b'1'])
        with self.assertRaisesRegex(mailbox.MailError,'UIDPLUS'):self.box.empty_folder('Trash')
        self.conn.uid.assert_called_once_with('SEARCH',None,'ALL');self.conn.expunge.assert_not_called()

    def test_partial_purge_stops_without_automatic_retry(self):
        for responses in [[('OK',[b'1']),('NO',[])],[('OK',[b'1']),('OK',[]),('NO',[])]]:
            self.conn.uid.reset_mock();self.conn.uid.side_effect=responses
            with self.assertRaisesRegex(mailbox.MailError,'Cleanup stopped'):self.box.empty_folder('Spam')
            self.assertEqual(self.conn.uid.call_count,len(responses))
        self.conn.expunge.assert_not_called()

    def test_snapshot_is_split_into_bounded_exact_uid_sets(self):
        uids=[str(uid) for uid in range(1,502)]
        self.conn.uid.return_value=('OK',[])
        self.assertEqual(self.box._purge_selected(self.conn,uids),501)
        calls=self.conn.uid.call_args_list
        self.assertEqual([call.args[0] for call in calls],['STORE','EXPUNGE','STORE','EXPUNGE'])
        self.assertEqual(calls[2].args[1],'501')
        self.assertNotIn('*',calls[0].args[1])


class FolderToolIntegrationTests(base.ServerIntegrationTests):
    test_auth_and_tool_list=None
    test_send_executes_without_confirmation=None

    def test_new_tools_require_write_scope_and_destructive_annotations(self):
        import json
        _,token=self.make_oauth(scope='mail.read')
        for name,args in [('empty_folder',{'folder':'Trash'}),('create_folder',{'name':'New'}),
                          ('rename_folder',{'folder':'Custom','name':'New'}),('delete_folder',{'folder':'Custom'})]:
            _,_,raw=self.call('POST','/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}},
                             {'Authorization':'Bearer '+token['access_token']})
            result=json.loads(raw)['result']
            self.assertTrue(result['isError']);self.assertEqual(result['content'][0]['text'],'Insufficient OAuth scope')
            declared=next(tool for tool in server.TOOLS if tool['name']==name)
            self.assertFalse(declared['annotations']['readOnlyHint'])
            self.assertEqual(declared['annotations']['destructiveHint'],name!='create_folder')

    def test_tools_execute_with_saved_connection_without_confirmation_form(self):
        import json
        _,token=self.make_oauth()
        for name,args in [('empty_folder',{'folder':'Trash'}),('create_folder',{'name':'Русская папка'}),
                          ('rename_folder',{'folder':'Custom','name':'New'}),('delete_folder',{'folder':'Custom'})]:
            with patch.object(server.Mailbox,name,return_value={'status':'test_ok'}) as action:
                _,_,raw=self.call('POST','/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}},
                                 {'Authorization':'Bearer '+token['access_token']})
                self.assertFalse(json.loads(raw)['result']['isError'])
                action.assert_called_once_with(*args.values())


if __name__=='__main__':unittest.main()
