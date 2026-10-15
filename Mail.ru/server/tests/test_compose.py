import base64
import copy
import email
import email.policy
import hashlib
import threading
from email.message import EmailMessage

import pytest

from mailru_mcp import compose
from mailru_mcp.compose import ComposeMixin
from mailru_mcp.mailbox import Mailbox, MailError


def message_bytes(*, subject="Draft", to="to@example.com", cc="", bcc="", attachment=True):
    msg = EmailMessage()
    msg["From"] = "me@example.com"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{subject.lower()}@example.com>"
    msg.set_content("plain body")
    msg.add_alternative("<b>html body</b>", subtype="html")
    if attachment:
        msg.add_attachment(b"\x00binary\xff", maintype="application", subtype="octet-stream", filename="data.bin")
    return msg.as_bytes(policy=email.policy.SMTP)


class FakeIMAP:
    def __init__(self, messages=None):
        self.messages = {"Drafts": {}, "INBOX": {}, "Sent": {}}
        for folder, values in (messages or {}).items():
            self.messages[folder] = {str(uid): raw for uid, raw in values.items()}
        self.selected = None
        self.capabilities = (b"IMAP4rev1", b"UIDPLUS")
        self.uidvalidity = {"Drafts": "9", "INBOX": "4", "Sent": "12"}
        self.next_uid = 100
        self.calls = []
        self.append_status = "OK"
        self.append_status_by_folder = {}
        self.store_status = "OK"
        self.expunge_status = "OK"
        self.appended = []
        self.last_append_uid = ""

    def list(self):
        return "OK", [b'(\\Drafts) "/" "Drafts"', b'(\\Inbox) "/" "INBOX"',
                      b'(\\Sent) "/" "Sent"']

    def select(self, folder, readonly=True):
        value = folder.strip('"')
        self.selected = value
        self.calls.append(("SELECT", value, readonly))
        return ("OK", [str(len(self.messages[value])).encode()]) if value in self.messages else ("NO", [])

    def response(self, name):
        if name == "UIDVALIDITY":
            return name, [self.uidvalidity[self.selected].encode()]
        if name == "APPENDUID" and self.last_append_uid:
            return name, [f"{self.uidvalidity[self.selected]} {self.last_append_uid}".encode()]
        return name, []

    def append(self, folder, flags, date_time, raw):
        folder = folder.strip('"')
        self.calls.append(("APPEND", folder, flags))
        append_status = self.append_status_by_folder.get(folder, self.append_status)
        if append_status != "OK":
            return append_status, [b"failed"]
        uid = str(self.next_uid)
        self.next_uid += 1
        self.last_append_uid = uid
        self.messages[folder][uid] = raw
        self.appended.append((folder, uid, raw))
        return "OK", [f"[APPENDUID {self.uidvalidity[folder]} {uid}] done".encode()]

    def uid(self, command, *args):
        command = command.upper()
        self.calls.append((command,) + args)
        if command == "FETCH":
            uid, selector = str(args[0]), args[1]
            raw = self.messages[self.selected].get(uid)
            if raw is None:
                return "OK", []
            if selector == "(RFC822.SIZE)":
                return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode()]
            return "OK", [(f"1 (UID {uid})".encode(), raw)]
        if command == "SEARCH":
            if "HEADER" in args:
                wanted = str(args[-1]).strip('"')
                found = []
                for uid, raw in self.messages[self.selected].items():
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    if str(msg.get("Message-ID", "")) == wanted:
                        found.append(uid)
                return "OK", [" ".join(found).encode()]
            return "OK", [" ".join(sorted(self.messages[self.selected], key=int)).encode()]
        if command == "STORE":
            if self.store_status == "OK":
                return "OK", [b""]
            return self.store_status, [b""]
        if command == "EXPUNGE":
            uid = str(args[0])
            if self.expunge_status == "OK":
                self.messages[self.selected].pop(uid, None)
                return "OK", [b""]
            return self.expunge_status, [b""]
        raise AssertionError((command, args))


class TestMailbox(Mailbox):
    __test__ = False

    def __init__(self, conn):
        self.username = "me@example.com"
        self.password = "secret"
        self.ssl_context = object()
        self.conn = conn

    def _with_imap(self, callback):
        return callback(self.conn)

    def _with_readonly(self, callback):
        return callback(self.conn, 10**12)


class FakeSMTP:
    outcome = {}
    captured = []

    def __init__(self, *args, **kwargs):
        pass

    def login(self, username, password):
        return (235, b"ok")

    def send_message(self, message, from_addr, to_addrs):
        self.captured.append((copy.deepcopy(message), from_addr, list(to_addrs)))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def quit(self):
        return (221, b"bye")


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.outcome = {}
    FakeSMTP.captured = []
    ComposeMixin._delivery_receipts = {}
    ComposeMixin._compose_locks = {}
    TestMailbox._delivery_receipts = {}
    TestMailbox._compose_locks = {}
    monkeypatch.setattr(compose.smtplib, "SMTP_SSL", FakeSMTP)


def test_create_draft_keeps_bcc_and_uploaded_attachment():
    conn = FakeIMAP()
    box = TestMailbox(conn)
    result = box.compose_create_draft(
        "to@example.com", "Subject", "hello", cc="copy@example.com", bcc="hidden@example.com",
        html_body="<b>hello</b>", attachments=[{
            "filename": "note.txt", "content_type": "text/plain",
            "content_base64": base64.b64encode(b"payload").decode(),
        }])
    assert result["status"] == "draft_created"
    assert result["uid"] == "100"
    saved = email.message_from_bytes(conn.appended[0][2], policy=email.policy.default)
    assert saved["Bcc"] == "hidden@example.com"
    assert next(p for p in saved.walk() if p.get_filename() == "note.txt").get_payload(decode=True) == b"payload"


def test_send_uses_bcc_envelope_but_strips_transport_header():
    conn = FakeIMAP()
    result = TestMailbox(conn).compose_send(
        "to@example.com", "Subject", "hello", bcc="hidden@example.com",
        html_body="<b>hello</b>", attachments=[{
            "filename": "sent.txt", "content_type": "text/plain",
            "content_base64": base64.b64encode(b"sent payload").decode(),
        }])
    transported, _, recipients = FakeSMTP.captured[0]
    assert transported.get("Bcc") is None
    assert recipients == ["to@example.com", "hidden@example.com"]
    assert result["status"] == "sent"
    assert result["sent_copy_saved"] is True
    assert result["sent_folder"] == "Sent"
    sent = email.message_from_bytes(conn.messages["Sent"][result["sent_uid"]], policy=email.policy.default)
    assert sent["Bcc"] == "hidden@example.com"
    assert sent["Message-ID"] == transported["Message-ID"]
    assert "<b>hello</b>" in next(p for p in sent.walk() if p.get_content_type() == "text/html").get_content()
    assert next(p for p in sent.walk() if p.get_filename() == "sent.txt").get_payload(decode=True) == b"sent payload"


def test_bcc_only_send_uses_private_envelope_and_sent_copy():
    conn = FakeIMAP()
    result = TestMailbox(conn).compose_send("", "Private", "hello", bcc="hidden@example.com")
    transported, _, recipients = FakeSMTP.captured[0]
    assert recipients == ["hidden@example.com"]
    assert transported.get("To") is None
    assert transported.get("Bcc") is None
    assert result["sent_copy_saved"] is True
    sent = email.message_from_bytes(conn.messages["Sent"][result["sent_uid"]], policy=email.policy.default)
    assert sent["To"] is None
    assert sent["Bcc"] == "hidden@example.com"


def test_partial_and_uncertain_smtp_results_are_explicit():
    box = TestMailbox(FakeIMAP())
    FakeSMTP.outcome = {"bad@example.com": (550, b"no")}
    partial = box.compose_send("ok@example.com, bad@example.com", "Subject", "hello")
    assert partial["status"] == "partially_sent"
    assert partial["accepted_recipients"] == ["ok@example.com"]
    assert partial["do_not_retry"] is True
    assert partial["sent_copy_saved"] is True

    FakeSMTP.outcome = RuntimeError("connection dropped after DATA")
    uncertain = box.compose_send("ok@example.com", "Subject", "hello")
    assert uncertain["status"] == "send_uncertain"
    assert uncertain["do_not_retry"] is True


def test_read_draft_returns_full_guard_and_attachment_reference():
    conn = FakeIMAP({"Drafts": {"7": message_bytes(cc="copy@example.com", bcc="hidden@example.com")}})
    draft = TestMailbox(conn).read_draft("Drafts", "7")
    assert draft["uid_validity"] == "9"
    assert len(draft["message_fingerprint"]) == 64
    assert draft["cc"] == "copy@example.com"
    assert draft["bcc"] == "hidden@example.com"
    assert draft["attachments"][0]["folder"] == "Drafts"
    assert draft["attachments"][0]["uid"] == "7"


def test_update_preserves_mime_files_and_expunge_targets_only_old_uid():
    conn = FakeIMAP({"Drafts": {"7": message_bytes(bcc="hidden@example.com")}})
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    result = box.update_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"],
                              subject="Changed")
    assert result["status"] == "draft_updated"
    assert result["uid"] == "100"
    saved = email.message_from_bytes(conn.messages["Drafts"]["100"], policy=email.policy.default)
    assert saved["Bcc"] == "hidden@example.com"
    file_part = next(p for p in saved.walk() if p.get_filename() == "data.bin")
    assert file_part.get_payload(decode=True) == b"\x00binary\xff"
    assert ("EXPUNGE", "7") in conn.calls
    assert "7" not in conn.messages["Drafts"]


def test_update_rejects_concurrent_content_change_before_append():
    conn = FakeIMAP({"Drafts": {"7": message_bytes()}})
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    conn.messages["Drafts"]["7"] = message_bytes(subject="Changed elsewhere")
    with pytest.raises(MailError, match="changed"):
        box.update_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"], body="new")
    assert conn.appended == []


def test_update_does_not_delete_reused_old_uid_after_folder_validity_changes():
    conn = FakeIMAP({"Drafts": {"7": message_bytes()}})
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    original_append = conn.append
    def append_with_reset(*args):
        result = original_append(*args)
        conn.uidvalidity['Drafts'] = '10'
        return result
    conn.append = append_with_reset
    result = box.update_draft('Drafts','7',guard['uid_validity'],guard['message_fingerprint'],subject='Changed')
    assert result['status'] == 'draft_replacement_unverified'
    assert not any(call[0] == 'STORE' for call in conn.calls)
    assert '7' in conn.messages['Drafts']


@pytest.mark.parametrize('changed',['message_id','body','file'])
def test_update_retains_original_when_stored_replacement_is_changed(changed):
    conn=FakeIMAP({'Drafts':{'7':message_bytes()}})
    box=TestMailbox(conn)
    guard=box.read_draft('Drafts','7')
    original_append=conn.append
    def damaged_append(folder,flags,date_time,raw):
        result=original_append(folder,flags,date_time,raw)
        new_uid=conn.last_append_uid
        msg=email.message_from_bytes(conn.messages['Drafts'][new_uid],policy=email.policy.default)
        if changed=='message_id':
            msg.replace_header('Message-ID','<different@example.com>')
        elif changed=='body':
            next(part for part in msg.walk() if part.get_content_type()=='text/plain').set_content('corrupted')
        else:
            next(part for part in msg.walk() if part.get_filename()=='data.bin').set_content(
                b'corrupted',maintype='application',subtype='octet-stream')
        conn.messages['Drafts'][new_uid]=msg.as_bytes(policy=email.policy.SMTP)
        return result
    conn.append=damaged_append
    result=box.update_draft('Drafts','7',guard['uid_validity'],guard['message_fingerprint'],subject='Changed')
    assert result['status']=='draft_replacement_unverified'
    assert '7' in conn.messages['Drafts']
    assert not any(call[0]=='STORE' for call in conn.calls)


def test_update_retains_original_changed_during_append():
    conn=FakeIMAP({'Drafts':{'7':message_bytes()}})
    box=TestMailbox(conn)
    guard=box.read_draft('Drafts','7')
    original_append=conn.append
    def append_while_original_changes(*args):
        result=original_append(*args)
        conn.messages['Drafts']['7']=message_bytes(subject='Changed elsewhere')
        return result
    conn.append=append_while_original_changes
    result=box.update_draft('Drafts','7',guard['uid_validity'],guard['message_fingerprint'],subject='Changed')
    assert result['status']=='draft_original_changed'
    assert '7' in conn.messages['Drafts']
    assert not any(call[0]=='STORE' for call in conn.calls)


def test_append_failure_retains_old_draft():
    conn = FakeIMAP({"Drafts": {"7": message_bytes()}})
    conn.append_status = "NO"
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    result = box.update_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"], body="new")
    assert result["status"] == "draft_append_failed"
    assert result["old_draft_retained"] is True
    assert "7" in conn.messages["Drafts"]
    assert not any(call[0] == "STORE" for call in conn.calls)


def test_send_draft_partial_retains_and_full_send_cleanup_failure_is_guarded():
    conn = FakeIMAP({"Drafts": {"7": message_bytes(to="ok@example.com, bad@example.com")}})
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    FakeSMTP.outcome = {"bad@example.com": (550, b"no")}
    partial = box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])
    assert partial["status"] == "partially_sent"
    assert partial["draft_retained"] is True
    assert not any(call[0] == "STORE" for call in conn.calls)
    with pytest.raises(MailError, match="already has"):
        box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])

    TestMailbox._delivery_receipts = {}
    FakeSMTP.outcome = {}
    conn.store_status = "NO"
    failed_cleanup = box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])
    assert failed_cleanup["status"] == "sent_cleanup_failed"
    assert failed_cleanup["draft_retained"] is True
    with pytest.raises(MailError, match="already has"):
        box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])


def test_send_draft_success_expunge_targets_only_selected_uid():
    conn = FakeIMAP({"Drafts": {"7": message_bytes(), "8": message_bytes(subject="Other")}})
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    result = box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])
    assert result["status"] == "sent"
    assert result["sent_copy_saved"] is True
    assert ("STORE", "7", "+FLAGS.SILENT", "(\\Deleted)") in conn.calls
    assert ("EXPUNGE", "7") in conn.calls
    assert "8" in conn.messages["Drafts"]


def test_sent_append_failure_retains_draft_and_blocks_duplicate_delivery():
    conn = FakeIMAP({"Drafts": {"7": message_bytes(bcc="hidden@example.com")}})
    conn.append_status_by_folder["Sent"] = "NO"
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    result = box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])
    assert result["status"] == "sent_copy_failed"
    assert result["sent_copy_saved"] is False
    assert result["draft_retained"] is True
    assert result["do_not_retry"] is True
    assert sum(call[0] == "APPEND" and call[1] == "Sent" for call in conn.calls) == 1
    assert not any(call[0] == "STORE" for call in conn.calls)
    assert "7" in conn.messages["Drafts"]
    with pytest.raises(MailError, match="already has"):
        box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])


def test_draft_uidvalidity_reset_while_saving_sent_copy_stops_cleanup():
    conn = FakeIMAP({"Drafts": {"7": message_bytes()}})
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    original_append = conn.append

    def append_with_drafts_reset(folder, flags, date_time, raw):
        result = original_append(folder, flags, date_time, raw)
        if folder.strip('"') == "Sent":
            conn.uidvalidity["Drafts"] = "10"
        return result

    conn.append = append_with_drafts_reset
    result = box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])
    assert result["status"] == "sent_cleanup_unverified"
    assert result["sent_copy_saved"] is True
    assert not any(call[0] == "STORE" for call in conn.calls)
    assert "7" in conn.messages["Drafts"]


def test_sent_verification_ignores_only_mailru_root_trace_headers():
    conn = FakeIMAP({"Drafts": {"7": message_bytes(bcc="hidden@example.com")}})
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    original_append = conn.append

    def append_with_trace_headers(folder, flags, date_time, raw):
        result = original_append(folder, flags, date_time, raw)
        folder_name = folder.strip('"')
        if folder_name == "Sent" and result[0] == "OK":
            uid = conn.last_append_uid
            stored = email.message_from_bytes(conn.messages[folder_name][uid], policy=email.policy.default)
            stored["Delivered-To"] = "me@example.com"
            stored["Received"] = "from smtp.mail.ru by store.mail.ru"
            stored["X-Mailru-Intl-Transport"] = "yes"
            stored["Delivered-To"] = "me@example.com"
            stored["Received"] = "from append.mail.ru by store.mail.ru"
            stored["X-Mailru-Intl-Transport"] = "append"
            conn.messages[folder_name][uid] = stored.as_bytes(policy=email.policy.SMTP)
        return result

    conn.append = append_with_trace_headers
    result = box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])
    assert result["status"] == "sent"
    assert result["sent_copy_saved"] is True
    stored = email.message_from_bytes(conn.messages["Sent"][result["sent_uid"]], policy=email.policy.default)
    assert len(stored.get_all("Received")) == 2
    assert stored["Bcc"] == "hidden@example.com"
    assert next(p for p in stored.walk() if p.get_content_type() == "text/plain").get_content().strip() == "plain body"
    assert next(p for p in stored.walk() if p.get_filename() == "data.bin").get_payload(decode=True) == b"\x00binary\xff"
    assert result["sent_message_fingerprint"] == hashlib.sha256(stored.as_bytes()).hexdigest()


@pytest.mark.parametrize("changed", ["bcc", "body", "file"])
def test_sent_semantic_change_fails_verification_and_retains_draft(changed):
    conn = FakeIMAP({"Drafts": {"7": message_bytes(bcc="hidden@example.com")}})
    box = TestMailbox(conn)
    guard = box.read_draft("Drafts", "7")
    original_append = conn.append

    def append_with_changed_authored_content(folder, flags, date_time, raw):
        result = original_append(folder, flags, date_time, raw)
        folder_name = folder.strip('"')
        if folder_name == "Sent" and result[0] == "OK":
            uid = conn.last_append_uid
            stored = email.message_from_bytes(conn.messages[folder_name][uid], policy=email.policy.default)
            if changed == "bcc":
                del stored["Bcc"]
                stored["Bcc"] = "different@example.com"
            elif changed == "body":
                part = next(part for part in stored.walk() if part.get_content_type() == "text/plain")
                part.set_content("changed body")
            else:
                part = next(part for part in stored.walk() if part.get_filename() == "data.bin")
                part.set_content(b"changed file", maintype="application", subtype="octet-stream")
            conn.messages[folder_name][uid] = stored.as_bytes(policy=email.policy.SMTP)
        return result

    conn.append = append_with_changed_authored_content
    result = box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])
    assert result["status"] == "sent_copy_uncertain"
    assert result["sent_copy_saved"] is False
    assert result["draft_retained"] is True
    assert not any(call[0] == "STORE" for call in conn.calls)
    assert "7" in conn.messages["Drafts"]
    with pytest.raises(MailError, match="already has"):
        box.send_draft("Drafts", "7", guard["uid_validity"], guard["message_fingerprint"])


def test_missing_source_uidvalidity_fails_before_fetch():
    conn = FakeIMAP({"INBOX": {"5": message_bytes()}})
    box = TestMailbox(conn)
    attachment = {"folder": "INBOX", "uid": "5", "index": 3}
    with pytest.raises(MailError, match="uid_validity"):
        box.compose_create_draft("to@example.com", "Draft", "body", attachments=[attachment])
    assert not any(call[0] == "FETCH" for call in conn.calls)

    conn.calls.clear()
    with pytest.raises(MailError, match="uid_validity"):
        box.reply_message("INBOX", "5", "reply", uid_validity="")
    assert not any(call[0] in {"SELECT", "FETCH"} for call in conn.calls)


def test_ambiguous_sent_folder_never_saves_to_arbitrary_target():
    conn = FakeIMAP()
    original_list = conn.list
    conn.list = lambda: ('OK',original_list()[1]+[b'(\\Sent) "/" "Other sent"'])
    result = TestMailbox(conn).compose_send('to@example.com','Subject','Body')
    assert result['status'] == 'sent_copy_failed'
    assert result['sent_copy_saved'] is False
    assert conn.appended == []


def test_reply_all_excludes_self_and_forward_preserves_binary_attachment():
    original = EmailMessage()
    original["From"] = "sender@example.com"
    original["Reply-To"] = "reply@example.com"
    original["To"] = "me@example.com, other@example.com"
    original["Cc"] = "OTHER@example.com, copy@example.com"
    original["Subject"] = "Topic"
    original["Message-ID"] = "<original@example.com>"
    original["References"] = "<older@example.com>"
    original.set_content("original")
    original.add_attachment(b"binary", maintype="application", subtype="octet-stream", filename="kept.bin")
    conn = FakeIMAP({"INBOX": {"5": original.as_bytes(policy=email.policy.SMTP)}})
    box = TestMailbox(conn)

    reply = box.reply_message("INBOX", "5", "answer", reply_all=True, send=False, uid_validity="4")
    reply_msg = email.message_from_bytes(conn.appended[-1][2], policy=email.policy.default)
    assert reply["status"] == "draft_created"
    assert reply_msg["To"] == "reply@example.com"
    assert {x.strip().casefold() for x in str(reply_msg["Cc"]).split(",")} == {
        "other@example.com", "copy@example.com"
    }
    assert reply_msg["In-Reply-To"] == "<original@example.com>"
    assert "<original@example.com>" in reply_msg["References"]

    forwarded = box.forward_message("INBOX", "5", "dest@example.com", send=False, uid_validity="4")
    forward_msg = email.message_from_bytes(conn.appended[-1][2], policy=email.policy.default)
    kept = next(p for p in forward_msg.walk() if p.get_filename() == "kept.bin")
    assert forwarded["preserved_file_count"] == 1
    assert kept.get_payload(decode=True) == b"binary"

    sent_reply = box.reply_message(
        "INBOX", "5", "answer now", send=True, bcc="hidden@example.com", uid_validity="4")
    assert sent_reply["status"] == "sent"
    assert sent_reply["sent_copy_saved"] is True
    reply_copy = email.message_from_bytes(
        conn.messages["Sent"][sent_reply["sent_uid"]], policy=email.policy.default)
    assert reply_copy["Bcc"] == "hidden@example.com"
    assert reply_copy["In-Reply-To"] == "<original@example.com>"

    sent_forward = box.forward_message(
        "INBOX", "5", "dest@example.com", send=True, uid_validity="4")
    assert sent_forward["status"] == "sent"
    assert sent_forward["sent_copy_saved"] is True
    forward_copy = email.message_from_bytes(
        conn.messages["Sent"][sent_forward["sent_uid"]], policy=email.policy.default)
    assert next(p for p in forward_copy.walk() if p.get_filename() == "kept.bin").get_payload(decode=True) == b"binary"

