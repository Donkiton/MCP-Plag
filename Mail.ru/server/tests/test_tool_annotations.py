import unittest
import test_server as base


class ToolAnnotationTests(unittest.TestCase):
    def test_fixed_mailbox_tools_are_closed_account_operations(self):
        for tool in base.server.TOOLS:
            self.assertEqual(tool['annotations']['openWorldHint'],
                             tool['name'] in ('prepare_send','unsubscribe_senders','send_draft','reply_message','forward_message'),tool['name'])

    def test_mutations_keep_write_scopes_and_destructive_labels(self):
        tools={tool['name']:tool for tool in base.server.TOOLS}
        for name in ('prepare_move','prepare_delete','trash_messages','trash_all_messages',
                     'delete_message_permanently','empty_folder','unsubscribe_senders','prepare_send'):
            self.assertFalse(tools[name]['annotations']['readOnlyHint'],name)
            self.assertTrue(tools[name]['annotations']['destructiveHint'],name)
            self.assertEqual(tools[name]['securitySchemes'][0]['scopes'],['mail.write'],name)
