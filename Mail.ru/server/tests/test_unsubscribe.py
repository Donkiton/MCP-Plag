import email,unittest
from unittest.mock import MagicMock,patch
import test_server as base
from mailru_mcp import unsubscribe as u
mailbox=base.mailbox


def message(extra='',body=''):
    return email.message_from_bytes(('From: News <hello@buffer.com>\nSubject: Newsletter\n'+extra+'\n'+body).encode('utf-8'))
def info(msg):return u.options(msg,{'folder':'INBOX','uid':'1','subject':'Newsletter'})
AUTH='Authentication-Results: mxs.mail.ru; dkim=pass header.d=cio.buffer.com\nDKIM-Signature: v=1; d=cio.buffer.com; h=from:subject:list-unsubscribe:list-unsubscribe-post; b=fake\n'
HEADERS='List-Unsubscribe: <https://email-links.buffer.com/unsubscribe/token>, <mailto:unsubscribe@example.org?subject=stop>\nList-Unsubscribe-Post: List-Unsubscribe=One-Click\n'


class UnsubscribeTests(unittest.TestCase):
    def test_buffer_authenticated_one_click_is_preferred_without_contact(self):
        with patch.object(u,'request') as network:
            value=info(message(AUTH+HEADERS))
        network.assert_not_called()
        self.assertTrue(value['one_click_authenticated']);self.assertEqual(value['methods'][0]['method'],'one_click')

    def test_unsigned_or_wrong_receiver_headers_never_offer_one_click(self):
        for auth in ['',AUTH.replace('mxs.mail.ru','attacker.example'),AUTH.replace('dkim=pass','dkim=fail'),AUTH.replace(':list-unsubscribe-post','')]:
            value=info(message(auth+HEADERS))
            self.assertFalse(value['one_click_authenticated']);self.assertNotEqual(value['methods'][0]['method'],'one_click')

    def test_one_click_exact_post_without_get_or_credentials(self):
        send=MagicMock()
        with patch.object(u,'request',return_value=(200,None,'')) as network:
            result=u.perform(info(message(AUTH+HEADERS)),send)
        network.assert_called_once_with('https://email-links.buffer.com/unsubscribe/token','POST',b'List-Unsubscribe=One-Click')
        self.assertEqual(result['status'],'request_accepted');send.assert_not_called()

    def test_one_click_redirect_does_not_fallback_or_repeat(self):
        send=MagicMock()
        with patch.object(u,'request',return_value=(302,'https://other.example/', '')) as network:
            result=u.perform(info(message(AUTH+HEADERS)),send)
        self.assertEqual(result['status'],'failed_or_uncertain');self.assertEqual(network.call_count,1);send.assert_not_called()

    def test_definite_https_refusal_uses_signed_alternate_mailto_once(self):
        for status in (403,404,405,410):
            send=MagicMock(return_value={'status':'sent'})
            with patch.object(u,'request',return_value=(status,None,'Denied')) as network:
                result=u.perform(info(message(AUTH+HEADERS)),send)
            self.assertEqual(result['status'],'request_sent')
            self.assertEqual(result['method'],'mailto')
            self.assertEqual(result['initial_http_status'],status)
            network.assert_called_once()
            send.assert_called_once_with('unsubscribe@example.org','stop','Please unsubscribe me from this mailing list.')

    def test_uncertain_https_outcomes_do_not_send_alternate_email(self):
        for status in (429,500,502,503):
            send=MagicMock()
            with patch.object(u,'request',return_value=(status,None,'')) as network:
                result=u.perform(info(message(AUTH+HEADERS)),send)
            self.assertEqual(result['status'],'failed_or_uncertain');send.assert_not_called();network.assert_called_once()
        send=MagicMock()
        with patch.object(u,'request',side_effect=TimeoutError()),self.assertRaises(TimeoutError):
            u.perform(info(message(AUTH+HEADERS)),send)
        send.assert_not_called()

    def test_refusal_without_alternate_does_not_guess_an_email_or_url(self):
        value=info(message(AUTH+HEADERS.replace(', <mailto:unsubscribe@example.org?subject=stop>','')))
        send=MagicMock()
        with patch.object(u,'request',return_value=(403,None,'Denied')):
            result=u.perform(value,send)
        self.assertEqual(result['status'],'failed_or_uncertain');send.assert_not_called()

    def test_mailto_uses_connected_sender_and_only_prescribed_headers(self):
        value=info(message('List-Unsubscribe: <mailto:unsub@example.org?subject=unsubscribe&body=Stop+mail>\n'))
        send=MagicMock(return_value={'status':'sent'})
        result=u.perform(value,send)
        send.assert_called_once_with('unsub@example.org','unsubscribe','Stop mail')
        self.assertEqual(result['status'],'request_sent')

    def test_mailto_header_injection_and_extra_recipients_refused(self):
        for url in ['mailto:unsub@example.org?subject=x%0d%0aBcc:evil@example.org','mailto:unsub@example.org?bcc=evil@example.org','mailto:a@example.org,b@example.org']:
            send=MagicMock()
            with self.assertRaises(mailbox.MailError):u.perform(info(message('List-Unsubscribe: <'+url+'>\n')),send)
            send.assert_not_called()

    def test_legacy_body_link_can_be_found_without_following_it(self):
        value=info(message('Content-Type: text/html; charset=utf-8\n','<a href="https://track.customer.io/unsubscribe/token">Отписаться от рассылки</a><a href="https://other.example/">other</a>'))
        self.assertEqual(len(value['methods']),1);self.assertEqual(value['methods'][0]['source'],'message_body')

    def test_simple_web_form_submits_only_hidden_fields_and_unsubscribe_button(self):
        page='<form method="POST" action="/stop"><input type="hidden" name="token" value="fixture"><button name="action" value="stop">Unsubscribe</button></form>'
        with patch.object(u,'request',side_effect=[(200,None,page),(200,None,'<h1>You have been unsubscribed.</h1>')]) as network:
            result=u.web_unsubscribe('https://sender.example/unsubscribe')
        self.assertEqual(result['status'],'unsubscribed')
        self.assertEqual(network.call_args_list[1].args,('https://sender.example/stop','POST',b'token=fixture&action=stop'))

    def test_login_interactive_cross_origin_or_multiple_forms_require_manual(self):
        for page in ['<form method="post"><input type="password"><button>Unsubscribe</button></form>',
                     '<form method="post" action="https://evil.example/stop"><button>Unsubscribe</button></form>',
                     '<form method="post"><button>Unsubscribe</button></form>'*2,
                     '<form method="post"><select name="reason"></select><button>Unsubscribe</button></form>']:
            with patch.object(u,'request',return_value=(200,None,page)) as network:
                result=u.web_unsubscribe('https://sender.example/unsubscribe')
            self.assertEqual(result['status'],'manual_confirmation_required');self.assertEqual(network.call_count,1)

    def test_plain_200_is_not_reported_as_subscription_removal(self):
        with patch.object(u,'request',return_value=(200,None,'Welcome')):
            self.assertEqual(u.web_unsubscribe('https://sender.example/unsubscribe')['status'],'manual_confirmation_required')
        self.assertFalse(u.confirmed('<script>You have been unsubscribed</script><p>Confirm below</p>'))

    def test_private_mixed_dns_or_non_https_destinations_blocked(self):
        for address in ['127.0.0.1','10.0.0.1','169.254.169.254','::1','100.64.0.1']:
            with patch.object(u.socket,'getaddrinfo',return_value=[(0,0,0,'',(address,443))]),self.assertRaises(mailbox.MailError):u.safe_target('https://sender.example/unsubscribe')
        with patch.object(u.socket,'getaddrinfo',return_value=[(0,0,0,'',('8.8.8.8',443)),(0,0,0,'',('127.0.0.1',443))]),self.assertRaises(mailbox.MailError):u.safe_target('https://sender.example/unsubscribe')
        for url in ['http://sender.example/x','https://user:password@sender.example/x','https://sender.example:8443/x','https://sender.example/\r\nx']:
            with self.assertRaises(mailbox.MailError):u.safe_target(url)

    def test_http_transport_pins_host_validates_tls_and_sends_no_cookies_auth(self):
        conn=MagicMock();conn.getresponse.return_value.status=200
        conn.getresponse.return_value.read.return_value=b'accepted'
        conn.getresponse.return_value.headers.get_content_charset.return_value='utf-8'
        with patch.object(u,'safe_target',return_value=(u.urlsplit('https://sender.example/stop?token=fixture'),'sender.example','8.8.8.8')),patch.object(u,'PublicHTTPSConnection',return_value=conn) as factory:
            u.request('https://sender.example/stop?token=fixture','POST',b'List-Unsubscribe=One-Click')
        factory.assert_called_once_with('sender.example','8.8.8.8')
        headers=conn.request.call_args.kwargs['headers']
        self.assertNotIn('Cookie',headers);self.assertNotIn('Authorization',headers);self.assertNotIn('Referer',headers)
        conn.close.assert_called_once()

    def test_multiple_lists_deduplicate_identical_list_and_keep_individual_status(self):
        box=mailbox.Mailbox('test@mail.ru','fake')
        a=info(message(AUTH+HEADERS+'List-ID: Buffer Newsletter <news.buffer.com>\n'))
        b={**a,'uid':'2'};c={**a,'uid':'3','dedupe_key':'other-list'}
        with patch.object(box,'unsubscribe_options',side_effect=[a,b,c]),patch.object(u,'perform',side_effect=[{'status':'request_accepted'},{'status':'manual_confirmation_required'}]) as perform:
            result=box.unsubscribe_messages([{'folder':'INBOX','uid':str(i)} for i in range(1,4)])
        self.assertEqual(perform.call_count,2)
        self.assertEqual([value['status'] for value in result['results']],['request_accepted','duplicate','manual_confirmation_required'])

    def test_failed_sender_does_not_prevent_processing_the_others(self):
        box=mailbox.Mailbox('test@mail.ru','fake')
        value=info(message(AUTH+HEADERS))
        with patch.object(box,'unsubscribe_options',side_effect=[mailbox.MailError('missing'),value]),patch.object(u,'perform',return_value={'status':'request_accepted'}):
            result=box.unsubscribe_messages([{'folder':'INBOX','uid':'1'},{'folder':'INBOX','uid':'2'}])
        self.assertEqual(result['processed_count'],2);self.assertEqual(result['results'][0]['status'],'failed_or_uncertain')
