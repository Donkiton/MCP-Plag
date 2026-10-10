import email,unittest
from unittest.mock import MagicMock,patch
import test_server as base
mailbox=base.mailbox


class FolderLabelTests(unittest.TestCase):
    def test_smart_labels_keep_exact_imap_identifiers_and_delimiters(self):
        conn=MagicMock();conn.list.return_value=('OK',[
            b'(\\Inbox) "/" "INBOX"',b'() "/" "INBOX/ToMyself"',b'() "/" "INBOX/Receipts"'])
        result=mailbox.Mailbox('test@mail.ru','unused')._folders(conn)
        self.assertEqual([(item['folder'],item['display_name'],item['delimiter']) for item in result],
                         [('INBOX','Входящие','/'),('INBOX/ToMyself','Письма себе','/'),('INBOX/Receipts','Чеки','/')])

    def test_similarly_named_custom_folders_are_not_translated(self):
        for path in ('ToMyself','Receipts','Work/Receipts','INBOX/Receipts archive','INBOX/ToMyself/Notes','INBOX/tomyself'):
            self.assertEqual(mailbox._folder_label(path),path)
        name='Чеки работы & квитанции'
        self.assertEqual(mailbox._folder_label(mailbox._encode_folder(name)),name)

    def test_utf7_codec_stays_reversible_for_user_created_names(self):
        for name in ('INBOX','INBOX/ToMyself','INBOX/Receipts','Письма себе','A & B'):
            self.assertEqual(mailbox._display_folder(mailbox._encode_folder(name)),name)

    def test_read_and_search_message_metadata_has_user_label(self):
        message=email.message_from_string('Subject: Example\n\nBody')
        result=mailbox.Mailbox._meta('INBOX/Receipts','42',message)
        self.assertEqual(result['folder'],'INBOX/Receipts')
        self.assertEqual(result['folder_display_name'],'Чеки')

    def test_empty_paged_folder_has_label_without_mutating_it(self):
        box=mailbox.Mailbox('test@mail.ru','unused');conn=MagicMock()
        with patch.object(box,'_with_readonly',side_effect=lambda f:f(conn,100)),patch.object(box,'_read_budget'),patch.object(box,'_select') as select,patch.object(box,'_uid_validity',return_value='1'),patch.object(box,'_all_uids',return_value=[]):
            result=box.recent('INBOX/ToMyself',paged=True)
        self.assertEqual(result['folder_display_name'],'Письма себе')
        self.assertEqual(result['folder'],'INBOX/ToMyself')
        select.assert_called_once_with(conn,'INBOX/ToMyself')
        conn.create.assert_not_called();conn.rename.assert_not_called();conn.delete.assert_not_called()
