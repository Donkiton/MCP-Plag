import unittest

from mailru_mcp.mailbox import Mailbox,MailError


class ConsumedResponsesTests(unittest.TestCase):
    def test_uidvalidity_response_is_consumed_and_cached_only_for_current_select(self):
        class Conn:
            values=[None]
            def response(self,name):
                result=self.values
                self.values=[None]
                return name,result
            def select(self,folder,readonly=True):
                self.values=[b'11' if 'INBOX' in folder else b'22']
                return 'OK',[b'1']
        conn=Conn()
        Mailbox._select(conn,'INBOX')
        self.assertEqual('11',Mailbox._uid_validity(conn))
        self.assertEqual('11',Mailbox._uid_validity(conn))
        Mailbox._select(conn,'Sent')
        self.assertEqual('22',Mailbox._uid_validity(conn))
        self.assertEqual('22',Mailbox._uid_validity(conn))
        conn.values=[b'33']
        self.assertEqual('33',Mailbox._uid_validity(conn))

    def test_reselect_cannot_reuse_old_validity_when_new_folder_omits_it(self):
        class Conn:
            values=[b'11']
            def response(self,name):
                result=self.values;self.values=[None]
                return name,result
            def select(self,folder,readonly=True):return 'OK',[b'1']
        conn=Conn()
        self.assertEqual('11',Mailbox._uid_validity(conn))
        Mailbox._select(conn,'Sent')
        with self.assertRaises(MailError):Mailbox._uid_validity(conn)


if __name__=='__main__':unittest.main()
