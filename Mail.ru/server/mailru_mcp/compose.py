"""Safe draft, send, reply and forward operations for :mod:`mailru_mcp.mailbox`.

The mailbox module imports ``ComposeMixin``.  Imports of mailbox helpers are
therefore deliberately local to functions in this module.
"""
from __future__ import annotations

import base64
import binascii
import copy
import email
import email.policy
import hashlib
import re
import smtplib
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid


_MISSING = object()
_UID_RE = re.compile(r"[1-9][0-9]{0,19}")
_CONTENT_TYPE_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")
_MAX_BODY = 200_000
_MAX_FILE = 5_000_000
_MAX_ATTACHMENTS = 20
_MAX_ATTACHMENTS_TOTAL = 8_000_000
_MAX_MESSAGE = 12_000_000


def _mailbox_api():
    # Lazy by design: mailbox.py imports this mixin while it is being defined.
    from .mailbox import MailError, _decode, _folder_arg, _safe_address
    return MailError, _decode, _folder_arg, _safe_address


def _fail(message):
    MailError, _, _, _ = _mailbox_api()
    raise MailError(message)


def _addresses(value, *, required=False):
    _, _, _, safe_address = _mailbox_api()
    if value is None:
        value = ""
    if not isinstance(value, str):
        _fail("Email address list must be a string")
    if not value:
        if required:
            _fail("At least one recipient is required")
        return []
    return safe_address(value)


def _subject(value):
    if not isinstance(value, str) or len(value) > 500 or "\r" in value or "\n" in value:
        _fail("Invalid subject")
    return value


def _content(value, name):
    if not isinstance(value, str) or len(value) > _MAX_BODY:
        _fail(f"{name} exceeds 200000 characters")
    return value


def _uid(value):
    value = str(value)
    if not _UID_RE.fullmatch(value):
        _fail("Invalid UID")
    return value


def _fingerprint(message):
    return hashlib.sha256(message.as_bytes()).hexdigest()


def _parsed_fingerprint(raw):
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    return _fingerprint(parsed)


_SENT_TRACE_HEADERS = ("Received", "Delivered-To", "X-Mailru-Intl-Transport")


def _sent_semantic_fingerprint(message):
    """Fingerprint authored MIME while excluding only root delivery traces."""
    authored = copy.deepcopy(message)
    for header in _SENT_TRACE_HEADERS:
        while authored.get(header) is not None:
            del authored[header]
    return _fingerprint(authored)


def _capabilities(conn):
    return {
        item.decode("ascii", "ignore").upper() if isinstance(item, bytes) else str(item).upper()
        for item in getattr(conn, "capabilities", ())
    }


def _text_part(message, content_type):
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_type() != content_type or part.get_content_disposition() == "attachment":
            continue
        blob = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return blob.decode(charset, errors="replace")
        except LookupError:
            return blob.decode("utf-8", errors="replace")
    return ""


def _is_file_part(part):
    if part.get_content_disposition() == "attachment" or part.get_filename():
        return True
    return bool(not part.is_multipart()
                and (part.get("Content-ID") or part.get_content_disposition() == "inline"))


def _part_data(part):
    decoded = part.get_payload(decode=True)
    if isinstance(decoded, bytes):
        return decoded
    # message/rfc822 attachments have a structured payload rather than a
    # transfer-decoded byte string.  Serialize it for limits and reporting.
    return part.as_bytes(policy=email.policy.SMTP) if _is_file_part(part) else b""


def _message_files(message):
    result = []

    def visit(parent):
        if not parent.is_multipart():
            return
        for part in parent.iter_parts():
            if _is_file_part(part):
                result.append(copy.deepcopy(part))
            else:
                visit(part)

    visit(message)
    return result


def _dedupe(values, excluded=()):
    blocked = {value.casefold() for value in excluded}
    result = []
    for value in values:
        key = value.casefold()
        if key not in blocked:
            blocked.add(key)
            result.append(value)
    return result


def _message_id_domain(sender):
    return sender.rsplit("@", 1)[-1] if "@" in sender else None


def _build_message(sender, to, subject, body, *, cc="", bcc="", html_body="",
                   file_parts=(), extra_headers=None, message_id=None, require_recipient=True):
    recipients_to = _addresses(to)
    recipients_cc = _addresses(cc)
    recipients_bcc = _addresses(bcc)
    if require_recipient and not (recipients_to or recipients_cc or recipients_bcc):
        _fail("At least one recipient is required")
    subject = _subject(subject)
    body = _content(body, "body")
    html_body = _content(html_body, "html_body")

    message = EmailMessage()
    message["From"] = sender
    if recipients_to:
        message["To"] = ", ".join(recipients_to)
    if recipients_cc:
        message["Cc"] = ", ".join(recipients_cc)
    if recipients_bcc:
        # Drafts retain Bcc.  _smtp_send always removes this header from the
        # transported copy while keeping these envelope recipients.
        message["Bcc"] = ", ".join(recipients_bcc)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = message_id or make_msgid(domain=_message_id_domain(sender))
    for key, value in (extra_headers or {}).items():
        if value:
            if "\r" in value or "\n" in value:
                _fail(f"Invalid {key} header")
            message[key] = value
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    for spec in file_parts:
        if spec["kind"] == "part":
            message.make_mixed()
            message.attach(copy.deepcopy(spec["part"]))
        else:
            maintype, subtype = spec["content_type"].split("/", 1)
            message.add_attachment(
                spec["data"], maintype=maintype, subtype=subtype,
                filename=spec["filename"], disposition=spec.get("disposition", "attachment"),
                cid=spec.get("cid"),
            )
    raw = message.as_bytes(policy=email.policy.SMTP)
    if len(raw) > _MAX_MESSAGE:
        _fail("Final MIME message exceeds 12 MB")
    return message, _dedupe(recipients_to + recipients_cc + recipients_bcc)


def _smtp_send(owner, message, recipients):
    """Send once and turn every post-attempt outcome into an explicit receipt."""
    if not recipients:
        _fail("At least one recipient is required")
    transport = copy.deepcopy(message)
    while transport.get("Bcc") is not None:
        del transport["Bcc"]
    smtp = None
    attempted = False
    refused = {}
    try:
        smtp = smtplib.SMTP_SSL("smtp.mail.ru", 465, context=owner.ssl_context, timeout=20)
        smtp.login(owner.username, owner.password)
        attempted = True
        refused = smtp.send_message(transport, from_addr=owner.username, to_addrs=recipients) or {}
    except smtplib.SMTPRecipientsRefused as exc:
        attempted = True
        refused = exc.recipients or {recipient: "refused" for recipient in recipients}
    except Exception:
        return {
            "status": "send_uncertain" if attempted else "send_failed",
            "accepted_recipients": [], "refused_recipients": [],
            "do_not_retry": bool(attempted),
            "note": (
                "SMTP completion is uncertain. Do not retry automatically; inspect delivery state first."
                if attempted else "SMTP failed before a delivery attempt was confirmed."
            ),
        }
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass
    refused_keys = {str(address).casefold() for address in refused}
    accepted = [address for address in recipients if address.casefold() not in refused_keys]
    refused_list = [address for address in recipients if address.casefold() in refused_keys]
    if refused_list and accepted:
        status = "partially_sent"
    elif refused_list:
        status = "refused"
    else:
        status = "sent"
    return {
        "status": status,
        "accepted_recipients": accepted,
        "refused_recipients": refused_list,
        "do_not_retry": bool(accepted),
        "note": (
            "Some recipients accepted the message. Do not retry automatically."
            if accepted and refused_list else
            "All recipients accepted the message." if accepted else
            "No recipient accepted the message."
        ),
    }


class ComposeMixin:
    """Composition operations mixed into ``Mailbox`` by mailbox.py."""

    _compose_locks_guard = threading.Lock()
    _compose_locks = {}
    _delivery_receipts = {}
    _delivery_receipt_ttl = 3600

    @classmethod
    def _operation_lock(cls, key):
        with cls._compose_locks_guard:
            lock = cls._compose_locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            _fail("A draft operation is already in progress; inspect its result before another request")
        return lock

    def _receipt_key(self, folder, uid, uid_validity, message_fingerprint):
        return (self.username.casefold(), folder, uid, uid_validity, message_fingerprint)

    @classmethod
    def _prune_receipts(cls):
        cutoff = time.monotonic() - cls._delivery_receipt_ttl
        with cls._compose_locks_guard:
            cls._delivery_receipts = {
                key: value for key, value in cls._delivery_receipts.items() if value[0] >= cutoff
            }

    def _delivery_receipt(self, key):
        self._prune_receipts()
        with self._compose_locks_guard:
            return self._delivery_receipts.get(key)

    def _record_delivery(self, key, status):
        with self._compose_locks_guard:
            self._delivery_receipts[key] = (time.monotonic(), status)

    def _draft_folder(self, conn):
        folders = self._folders(conn)
        draft = next((item["folder"] for item in folders if self._system_kind(item) == "drafts"), None)
        if not draft:
            _fail("Drafts folder not found; refusing to save elsewhere")
        return draft

    def _sent_folder(self, conn):
        folders = self._folders(conn)
        sent = [item["folder"] for item in folders if self._system_kind(item) == "sent"]
        if len(sent) != 1:
            _fail("Sent folder is missing or ambiguous; the accepted message cannot be archived safely")
        return sent[0]

    @staticmethod
    def _sent_copy_result(status, *, folder="", uid="", uid_validity="", fingerprint="", note=""):
        return {
            "sent_copy_saved": status == "saved",
            "sent_copy_status": status,
            "sent_folder": folder,
            "sent_uid": uid,
            "sent_uid_validity": uid_validity,
            "sent_message_fingerprint": fingerprint,
            "sent_copy_folder": folder,
            "sent_copy_uid": uid,
            "sent_copy_uid_validity": uid_validity,
            "sent_copy_message_fingerprint": fingerprint,
            "sent_copy_note": note,
        }

    def _save_sent_copy(self, conn, message):
        """APPEND the authored MIME once and verify the exact stored object."""
        _, _, folder_arg, _ = _mailbox_api()
        raw = message.as_bytes(policy=email.policy.SMTP)
        expected_fingerprint = _parsed_fingerprint(raw)
        expected_semantic_fingerprint = _sent_semantic_fingerprint(
            email.message_from_bytes(raw, policy=email.policy.default))
        message_id = str(message.get("Message-ID", ""))
        try:
            folder = self._sent_folder(conn)
            self._select(conn, folder, readonly=False)
            before_validity = self._uid_validity(conn)
        except Exception:
            return self._sent_copy_result(
                "failed", fingerprint=expected_fingerprint,
                note="The actual Sent folder could not be resolved before APPEND. Do not resend an accepted message.")
        try:
            typ, data = conn.append(folder_arg(folder), "(\\Seen)", None, raw)
        except Exception:
            return self._sent_copy_result(
                "uncertain", folder=folder, uid_validity=before_validity,
                fingerprint=expected_fingerprint,
                note="Sent APPEND completion is uncertain. Do not retry or resend automatically.")
        if typ != "OK":
            return self._sent_copy_result(
                "failed", folder=folder, uid_validity=before_validity,
                fingerprint=expected_fingerprint,
                note="Sent APPEND was rejected. The accepted message must not be resent.")
        try:
            stored_uid, append_validity = self._append_uid(conn, data, message_id)
            current_validity = self._uid_validity(conn)
            if (not stored_uid or current_validity != before_validity
                    or (append_validity and append_validity != current_validity)):
                return self._sent_copy_result(
                    "uncertain", folder=folder, uid=stored_uid,
                    uid_validity=append_validity or current_validity,
                    fingerprint=expected_fingerprint,
                    note="Sent copy UID or UIDVALIDITY could not be verified. Do not retry or resend automatically.")
            stored = self._fetch(conn, stored_uid)
            stored_fingerprint = _fingerprint(stored)
            if (str(stored.get("Message-ID", "")) != message_id
                    or _sent_semantic_fingerprint(stored) != expected_semantic_fingerprint):
                return self._sent_copy_result(
                    "uncertain", folder=folder, uid=stored_uid, uid_validity=current_validity,
                    fingerprint=stored_fingerprint,
                    note="Stored Sent MIME did not match the authored message. Do not retry or resend automatically.")
        except Exception:
            return self._sent_copy_result(
                "uncertain", folder=folder, fingerprint=expected_fingerprint,
                note="Sent APPEND succeeded, but verification is uncertain. Do not retry or resend automatically.")
        return self._sent_copy_result(
            "saved", folder=folder, uid=stored_uid, uid_validity=current_validity,
            fingerprint=stored_fingerprint, note="Authored MIME copy verified in Sent.")

    def _add_sent_copy(self, smtp_result, message, conn=None):
        result = dict(smtp_result)
        if not result.get("accepted_recipients"):
            return {**result, **self._sent_copy_result(
                "not_attempted", note="No confirmed accepted recipient; no Sent copy was appended.")}
        if conn is None:
            try:
                sent_copy = self._with_imap(lambda active: self._save_sent_copy(active, message))
            except Exception:
                sent_copy = self._sent_copy_result(
                    "failed", fingerprint=_fingerprint(message),
                    note="Could not connect to append the accepted message to Sent. Do not resend it.")
        else:
            sent_copy = self._save_sent_copy(conn, message)
        result.update(sent_copy)
        if result["status"] == "sent" and sent_copy["sent_copy_status"] != "saved":
            result["status"] = ("sent_copy_uncertain"
                                if sent_copy["sent_copy_status"] == "uncertain" else "sent_copy_failed")
            result["do_not_retry"] = True
            result["note"] = sent_copy["sent_copy_note"]
        elif result["status"] == "partially_sent":
            result["do_not_retry"] = True
            result["note"] = ("Some recipients accepted the message. Do not retry automatically. "
                              + sent_copy["sent_copy_note"])
        return result

    def _require_draft_folder(self, conn, folder):
        _, _, folder_arg, _ = _mailbox_api()
        folder_arg(folder)
        item = next((item for item in self._folders(conn) if item["folder"] == folder), None)
        if not item or self._system_kind(item) != "drafts":
            _fail("The selected folder is not the account Drafts folder")
        return folder

    def _resolve_attachments(self, conn, attachments):
        if attachments is None:
            return []
        if not isinstance(attachments, list) or len(attachments) > _MAX_ATTACHMENTS:
            _fail("attachments must be a list with at most 20 items")
        resolved = []
        total = 0
        for item in attachments:
            if not isinstance(item, dict):
                _fail("Each attachment must be an object")
            upload_keys = {"filename", "content_type", "content_base64"}
            source_keys = {"folder", "uid", "index", "uid_validity"}
            has_upload = bool(upload_keys.intersection(item))
            has_source = bool(source_keys.intersection(item))
            if has_upload and has_source:
                _fail("An attachment cannot mix uploaded content with a mailbox reference")
            if has_upload:
                if not {"filename", "content_base64"}.issubset(item) or set(item) - upload_keys:
                    _fail("Uploaded attachments require filename and content_base64; content_type is optional")
                filename = item["filename"]
                content_type = item.get("content_type", "application/octet-stream")
                encoded = item["content_base64"]
                if (not isinstance(filename, str) or not filename or len(filename) > 255
                        or "\r" in filename or "\n" in filename or "\0" in filename):
                    _fail("Invalid attachment filename")
                if not isinstance(content_type, str) or not _CONTENT_TYPE_RE.fullmatch(content_type):
                    _fail("Invalid attachment content_type")
                if not isinstance(encoded, str):
                    _fail("Attachment content_base64 must be a string")
                try:
                    data = base64.b64decode(encoded.encode("ascii"), validate=True)
                except (UnicodeEncodeError, ValueError, binascii.Error):
                    _fail("Attachment content_base64 is not strict base64")
                spec = {"kind": "bytes", "filename": filename,
                        "content_type": content_type.lower(), "data": data}
            elif has_source:
                if not {"folder", "uid", "index", "uid_validity"}.issubset(item) or set(item) - source_keys:
                    _fail("Mailbox attachment references require folder, uid, index and uid_validity")
                folder = item["folder"]
                uid = _uid(item["uid"])
                source_validity = item["uid_validity"]
                if not isinstance(source_validity, str) or not _UID_RE.fullmatch(source_validity):
                    _fail("Attachment source requires a positive uid_validity")
                try:
                    index = int(item["index"])
                except (TypeError, ValueError):
                    _fail("Invalid attachment index")
                if index < 0:
                    _fail("Invalid attachment index")
                self._select(conn, folder)
                validity = self._uid_validity(conn)
                if source_validity != validity:
                    _fail("Attachment source UIDVALIDITY changed; refresh the message")
                source = self._fetch(conn, uid)
                parts = list(source.walk())
                if index >= len(parts) or not _is_file_part(parts[index]):
                    _fail("Referenced MIME part is not an attachment or inline file")
                part = copy.deepcopy(parts[index])
                data = _part_data(part)
                spec = {"kind": "part", "part": part, "data": data}
            else:
                _fail("Attachment must contain uploaded content or a mailbox attachment reference")
            size = len(spec["data"])
            if size > _MAX_FILE:
                _fail("Each attachment is limited to 5 MB")
            total += size
            if total > _MAX_ATTACHMENTS_TOTAL:
                _fail("Attachments are limited to 8 MB total")
            resolved.append(spec)
        return resolved

    @staticmethod
    def _append_uid(conn, append_data, message_id):
        values = []
        for value in append_data or []:
            if isinstance(value, bytes):
                values.append(value)
            else:
                values.append(str(value).encode())
        try:
            response = conn.response("APPENDUID")[1] or []
            values.extend(v if isinstance(v, bytes) else str(v).encode() for v in response)
        except Exception:
            pass
        joined = b" ".join(values)
        matches = re.findall(rb"(?:APPENDUID\s+)?([1-9][0-9]*)\s+([1-9][0-9]*)", joined, re.I)
        if matches:
            validity, uid = matches[-1]
            return uid.decode("ascii"), validity.decode("ascii")
        quoted = '"' + message_id.replace("\\", "\\\\").replace('"', '\\"') + '"'
        typ, rows = conn.uid("SEARCH", None, "HEADER", "Message-ID", quoted)
        if typ == "OK" and rows and isinstance(rows[0], bytes):
            uids = rows[0].split()
            if len(uids) == 1 and _UID_RE.fullmatch(uids[0].decode("ascii", "ignore")):
                return uids[0].decode("ascii"), ""
        return "", ""

    def _append_draft(self, conn, folder, message):
        _, _, folder_arg, _ = _mailbox_api()
        raw = message.as_bytes(policy=email.policy.SMTP)
        if len(raw) > _MAX_MESSAGE:
            _fail("Final MIME message exceeds 12 MB")
        # Attachment resolution and reply/forward reads may have selected a
        # different source mailbox.  APPEND itself is mailbox-qualified, but
        # UIDVALIDITY and the Message-ID fallback search are selection-scoped.
        self._select(conn, folder, readonly=False)
        try:
            typ, data = conn.append(folder_arg(folder), "(\\Draft)", None, raw)
        except Exception:
            return {"status": "draft_append_uncertain", "uid": "", "uid_validity": "",
                    "message_fingerprint": _parsed_fingerprint(raw),
                    "do_not_retry": True,
                    "note": "APPEND completion is uncertain. Inspect Drafts before another request."}
        if typ != "OK":
            return {"status": "draft_append_failed", "uid": "", "uid_validity": "",
                    "message_fingerprint": _parsed_fingerprint(raw),
                    "do_not_retry": False, "note": "APPEND failed; the prior draft, if any, is unchanged."}
        try:
            uid, append_validity = self._append_uid(conn, data, str(message.get("Message-ID", "")))
        except Exception:
            uid, append_validity = "", ""
        try:
            current_validity = self._uid_validity(conn)
        except Exception:
            current_validity = ""
        return {"status": "draft_created" if uid else "draft_created_uid_unresolved",
                "uid": uid, "uid_validity": append_validity or current_validity,
                "message_fingerprint": _parsed_fingerprint(raw),
                "do_not_retry": not bool(uid),
                "note": "Draft saved." if uid else "Draft was saved but its UID could not be resolved; inspect Drafts before another mutation."}

    def _compose_with_connection(self, conn, to, subject, body, cc, bcc, html_body, attachments,
                                 *, extra_parts=(), extra_headers=None, allow_empty_to=False):
        files = list(extra_parts) + self._resolve_attachments(conn, attachments)
        total = sum(len(spec["data"]) for spec in files)
        if len(files) > _MAX_ATTACHMENTS or total > _MAX_ATTACHMENTS_TOTAL:
            _fail("Attachments are limited to 20 files and 8 MB total")
        return _build_message(self.username, to, subject, body, cc=cc, bcc=bcc,
                              html_body=html_body, file_parts=files, extra_headers=extra_headers,
                              require_recipient=not allow_empty_to)

    def compose_create_draft(self, to, subject, body, cc="", bcc="", html_body="", attachments=None):
        def run(conn):
            folder = self._draft_folder(conn)
            message, _ = self._compose_with_connection(
                conn, to, subject, body, cc, bcc, html_body, attachments, allow_empty_to=True)
            result = self._append_draft(conn, folder, message)
            return {**result, "folder": folder, "to": str(message.get("To", "")),
                    "cc": str(message.get("Cc", "")), "bcc": str(message.get("Bcc", "")),
                    "subject": str(message.get("Subject", ""))}
        return self._with_imap(run)

    def compose_send(self, to, subject, body, cc="", bcc="", html_body="", attachments=None):
        def build(conn):
            return self._compose_with_connection(conn, to, subject, body, cc, bcc, html_body, attachments)
        if attachments and any(isinstance(item, dict) and "folder" in item for item in attachments):
            def run(conn):
                message, recipients = build(conn)
                return message, self._add_sent_copy(_smtp_send(self, message, recipients), message, conn)
            message, result = self._with_imap(run)
        else:
            message, recipients = self._compose_with_connection(
                None, to, subject, body, cc, bcc, html_body, attachments)
            result = self._add_sent_copy(_smtp_send(self, message, recipients), message)
        return {**result, "to": str(message.get("To", "")), "cc": str(message.get("Cc", "")),
                "subject": str(message.get("Subject", ""))}

    def list_drafts(self, limit=20, before_uid="", uid_validity=""):
        folder = self._with_imap(self._draft_folder)
        return self.recent(folder=folder, limit=limit, before_uid=before_uid,
                           uid_validity=uid_validity, paged=True)

    def _draft_details(self, conn, folder, uid):
        _, decode, _, _ = _mailbox_api()
        uid = _uid(uid)
        self._require_draft_folder(conn, folder)
        self._select(conn, folder)
        validity = self._uid_validity(conn)
        message = self._fetch(conn, uid)
        files = []
        for index, part in enumerate(message.walk()):
            if _is_file_part(part):
                data = _part_data(part)
                files.append({
                    "folder": folder, "uid": uid, "uid_validity": validity, "index": index,
                    "filename": decode(part.get_filename() or "inline-file"),
                    "content_type": part.get_content_type(), "disposition": part.get_content_disposition() or "inline",
                    "content_id": str(part.get("Content-ID", "")), "size": len(data),
                })
        return message, {
            **self._meta(folder, uid, message),
            "cc": decode(message.get("Cc")), "bcc": decode(message.get("Bcc")),
            "reply_to": decode(message.get("Reply-To")),
            "body": _text_part(message, "text/plain"),
            "html_body": _text_part(message, "text/html"),
            "attachments": files, "uid_validity": validity,
            "message_fingerprint": _fingerprint(message),
        }

    def read_draft(self, folder, uid):
        def run(conn):
            return self._draft_details(conn, folder, uid)[1]
        return self._with_imap(run)

    @staticmethod
    def _guard(message, actual_validity, expected_validity, expected_fingerprint):
        if (not isinstance(expected_validity, str) or not expected_validity
                or not isinstance(expected_fingerprint, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint)):
            _fail("uid_validity and canonical message_fingerprint are required")
        if actual_validity != expected_validity or _fingerprint(message) != expected_fingerprint:
            _fail("Draft or Drafts UIDVALIDITY changed; read the draft again before mutating it")

    def update_draft(self, folder, uid, uid_validity, message_fingerprint, **patch):
        allowed = {"to", "cc", "bcc", "subject", "body", "html_body", "attachments"}
        if not patch or set(patch) - allowed:
            _fail("Provide at least one supported draft field")
        uid = _uid(uid)
        lock = self._operation_lock((self.username.casefold(), folder, uid))
        try:
            receipt_key = self._receipt_key(folder, uid, uid_validity, message_fingerprint)
            if self._delivery_receipt(receipt_key):
                _fail("This exact draft already has a delivery outcome; it cannot be updated safely")

            def run(conn):
                self._require_draft_folder(conn, folder)
                if "UIDPLUS" not in _capabilities(conn):
                    _fail("Server lacks UIDPLUS; refusing unsafe draft replacement")
                self._select(conn, folder, readonly=False)
                validity = self._uid_validity(conn)
                old = self._fetch(conn, uid)
                self._guard(old, validity, uid_validity, message_fingerprint)

                attachments_value = patch.get("attachments", _MISSING)
                if attachments_value is _MISSING:
                    files = [{"kind": "part", "part": part,
                              "data": _part_data(part)} for part in _message_files(old)]
                else:
                    files = self._resolve_attachments(conn, attachments_value)
                    # Resolving mailbox references may select another folder.  Recheck
                    # the exact original immediately before creating its replacement.
                    self._select(conn, folder, readonly=False)
                    validity = self._uid_validity(conn)
                    old = self._fetch(conn, uid)
                    self._guard(old, validity, uid_validity, message_fingerprint)

                values = {
                    "to": patch.get("to", str(old.get("To", ""))),
                    "cc": patch.get("cc", str(old.get("Cc", ""))),
                    "bcc": patch.get("bcc", str(old.get("Bcc", ""))),
                    "subject": patch.get("subject", str(old.get("Subject", ""))),
                    "body": patch.get("body", _text_part(old, "text/plain")),
                    "html_body": patch.get("html_body", _text_part(old, "text/html")),
                }
                extra = {name: str(old.get(name, "")) for name in ("Reply-To", "In-Reply-To", "References")}
                if not {"body", "html_body", "attachments"}.intersection(patch):
                    # A header-only edit must leave the original MIME tree byte
                    # content and nesting intact (including calendar, related,
                    # inline and signed-looking application parts).
                    replacement = copy.deepcopy(old)
                    validated = {
                        "To": _addresses(values["to"]),
                        "Cc": _addresses(values["cc"]),
                        "Bcc": _addresses(values["bcc"]),
                    }
                    _subject(values["subject"])
                    for header, recipients in validated.items():
                        while replacement.get(header) is not None:
                            del replacement[header]
                        if recipients:
                            replacement[header] = ", ".join(recipients)
                    while replacement.get("Subject") is not None:
                        del replacement["Subject"]
                    replacement["Subject"] = values["subject"]
                    for header in ("Date", "Message-ID"):
                        while replacement.get(header) is not None:
                            del replacement[header]
                    replacement["Date"] = formatdate(localtime=False)
                    replacement["Message-ID"] = make_msgid(domain=_message_id_domain(self.username))
                    if len(replacement.as_bytes(policy=email.policy.SMTP)) > _MAX_MESSAGE:
                        _fail("Final MIME message exceeds 12 MB")
                else:
                    replacement, _ = _build_message(
                        self.username, values["to"], values["subject"], values["body"],
                        cc=values["cc"], bcc=values["bcc"], html_body=values["html_body"],
                        file_parts=files, extra_headers=extra, require_recipient=False)
                appended = self._append_draft(conn, folder, replacement)
                if appended["status"] != "draft_created":
                    return {**appended, "folder": folder, "old_uid": uid,
                            "old_draft_retained": True}
                new_uid = appended["uid"]
                if new_uid == uid:
                    return {**appended, "status": "draft_replacement_unresolved", "folder": folder,
                            "old_uid": uid, "old_draft_retained": True, "do_not_retry": True,
                            "note": "Replacement UID was not distinct; inspect Drafts before another mutation."}
                try:
                    self._select(conn, folder, readonly=False)
                    replacement_validity = self._uid_validity(conn)
                    stored_replacement = self._fetch(conn, new_uid)
                except Exception:
                    return {**appended, "status": "draft_replacement_unverified", "folder": folder,
                            "old_uid": uid, "old_draft_retained": True, "do_not_retry": True,
                            "note": "Replacement was appended but could not be verified. The old draft was retained; inspect Drafts before another mutation."}
                appended["uid_validity"] = replacement_validity
                appended["message_fingerprint"] = _fingerprint(stored_replacement)
                if (replacement_validity != validity
                        or str(stored_replacement.get("Message-ID", "")) != str(replacement.get("Message-ID", ""))
                        or _sent_semantic_fingerprint(stored_replacement) != _sent_semantic_fingerprint(
                            email.message_from_bytes(replacement.as_bytes(policy=email.policy.SMTP),policy=email.policy.default))):
                    return {**appended, "status": "draft_replacement_unverified", "folder": folder,
                            "old_uid": uid, "old_draft_retained": None, "do_not_retry": True,
                            "note": "The stored replacement or Drafts UIDVALIDITY did not match. No old UID was deleted; inspect the folder before another mutation."}
                try:
                    self._select(conn,folder,readonly=False)
                    original_validity=self._uid_validity(conn)
                    original_now=self._fetch(conn,uid)
                    self._guard(original_now,original_validity,uid_validity,message_fingerprint)
                except Exception:
                    return {**appended,"status":"draft_original_changed","folder":folder,
                            "old_uid":uid,"old_draft_retained":None,"do_not_retry":True,
                            "note":"Replacement was verified, but the original draft changed or could not be verified before cleanup. No old UID was deleted."}
                try:
                    store_ok = conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")[0] == "OK"
                except Exception:
                    return {**appended, "status": "draft_replaced_cleanup_uncertain", "folder": folder,
                            "old_uid": uid, "old_draft_retained": None, "do_not_retry": True,
                            "note": "Replacement saved, but old-draft marking is uncertain. Do not retry automatically."}
                if not store_ok:
                    return {**appended, "status": "draft_replaced_old_retained", "folder": folder,
                            "old_uid": uid, "old_draft_retained": True, "do_not_retry": True,
                            "note": "Replacement saved, but the old draft could not be marked deleted. Do not retry automatically."}
                try:
                    expunge_ok = conn.uid("EXPUNGE", uid)[0] == "OK"
                except Exception:
                    expunge_ok = False
                if not expunge_ok:
                    return {**appended, "status": "draft_replaced_cleanup_uncertain", "folder": folder,
                            "old_uid": uid, "old_draft_retained": None, "do_not_retry": True,
                            "note": "Replacement saved, but exact old-draft cleanup is uncertain. Do not retry automatically."}
                return {**appended, "status": "draft_updated", "folder": folder,
                        "old_uid": uid, "old_draft_retained": False}
            return self._with_imap(run)
        finally:
            lock.release()

    def send_draft(self, folder, uid, uid_validity, message_fingerprint):
        uid = _uid(uid)
        lock = self._operation_lock((self.username.casefold(), folder, uid))
        key = self._receipt_key(folder, uid, uid_validity, message_fingerprint)
        try:
            receipt = self._delivery_receipt(key)
            if receipt:
                _fail(f"This exact draft already has a recorded delivery outcome ({receipt[1]}); do not resend it")

            def run(conn):
                self._require_draft_folder(conn, folder)
                self._select(conn, folder, readonly=False)
                validity = self._uid_validity(conn)
                message = self._fetch(conn, uid)
                self._guard(message, validity, uid_validity, message_fingerprint)
                recipients = []
                for header in ("To", "Cc", "Bcc"):
                    recipients.extend(_addresses(str(message.get(header, ""))))
                recipients = _dedupe(recipients)
                result = _smtp_send(self, message, recipients)
                if result["status"] in {"sent", "partially_sent", "send_uncertain"}:
                    self._record_delivery(key, result["status"])
                result = self._add_sent_copy(result, message, conn)
                if result["status"] != "sent":
                    return {**result, "folder": folder, "uid": uid, "draft_retained": True}
                if "UIDPLUS" not in _capabilities(conn):
                    return {**result, "status": "sent_cleanup_failed", "folder": folder, "uid": uid,
                            "draft_retained": True, "do_not_retry": True,
                            "note": "Delivery was accepted, but UIDPLUS is unavailable for safe cleanup. Do not resend."}
                try:
                    self._select(conn, folder, readonly=False)
                    cleanup_validity = self._uid_validity(conn)
                    cleanup_message = self._fetch(conn, uid)
                except Exception:
                    return {**result, "status": "sent_cleanup_unverified", "folder": folder, "uid": uid,
                            "draft_retained": None, "do_not_retry": True,
                            "note": "Delivery and Sent copy succeeded, but the original draft could not be re-read. No UID cleanup was attempted."}
                if cleanup_validity != uid_validity or _fingerprint(cleanup_message) != message_fingerprint:
                    return {**result, "status": "sent_cleanup_unverified", "folder": folder, "uid": uid,
                            "draft_retained": None, "do_not_retry": True,
                            "note": "Delivery and Sent copy succeeded, but the original draft identity changed. No UID cleanup was attempted."}
                try:
                    store_ok = conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")[0] == "OK"
                except Exception:
                    return {**result, "status": "sent_cleanup_uncertain", "folder": folder, "uid": uid,
                            "draft_retained": None, "do_not_retry": True,
                            "note": "Delivery was accepted, but draft marking is uncertain. Do not resend."}
                if not store_ok:
                    return {**result, "status": "sent_cleanup_failed", "folder": folder, "uid": uid,
                            "draft_retained": True, "do_not_retry": True,
                            "note": "Delivery was accepted, but the draft could not be marked deleted. Do not resend."}
                try:
                    expunge_ok = conn.uid("EXPUNGE", uid)[0] == "OK"
                except Exception:
                    expunge_ok = False
                if not expunge_ok:
                    return {**result, "status": "sent_cleanup_uncertain", "folder": folder, "uid": uid,
                            "draft_retained": None, "do_not_retry": True,
                            "note": "Delivery was accepted, but exact draft cleanup is uncertain. Do not resend."}
                return {**result, "folder": folder, "uid": uid, "draft_retained": False}
            return self._with_imap(run)
        finally:
            lock.release()

    def _source_message(self, conn, folder, uid, uid_validity=""):
        uid = _uid(uid)
        if not isinstance(uid_validity, str) or not _UID_RE.fullmatch(uid_validity):
            _fail("A positive uid_validity is required for the source message")
        self._select(conn, folder)
        validity = self._uid_validity(conn)
        if uid_validity != validity:
            _fail("Folder UIDVALIDITY changed; read the source message again")
        return self._fetch(conn, uid), validity

    def reply_message(self, folder, uid, body, reply_all=False, send=False, cc="", bcc="",
                      html_body="", attachments=None, uid_validity=""):
        def run(conn):
            original, validity = self._source_message(conn, folder, uid, uid_validity)
            reply_to = _addresses(str(original.get("Reply-To") or original.get("From") or ""), required=True)
            primary = ", ".join(reply_to)
            copied = []
            if reply_all:
                copied.extend(address for _, address in getaddresses(
                    [str(original.get("To", "")), str(original.get("Cc", ""))]) if address)
            copied.extend(_addresses(cc))
            copied = _dedupe(copied, excluded=[self.username, *reply_to])
            subject = str(original.get("Subject", ""))
            if not re.match(r"(?i)^\s*(re|aw|sv)\s*:", subject):
                subject = "Re: " + subject
            original_id = str(original.get("Message-ID", "")).strip()
            references = str(original.get("References", "")).strip()
            if original_id and original_id not in references.split():
                references = (references + " " + original_id).strip()
            message, recipients = self._compose_with_connection(
                conn, primary, subject, body, ", ".join(copied), bcc, html_body, attachments,
                extra_headers={"In-Reply-To": original_id, "References": references})
            if send:
                result = self._add_sent_copy(_smtp_send(self, message, recipients), message, conn)
            else:
                draft_folder = self._draft_folder(conn)
                result = {**self._append_draft(conn, draft_folder, message), "folder": draft_folder}
            return {**result, "source": {"folder": folder, "uid": str(uid), "uid_validity": validity},
                    "reply_all": bool(reply_all)}
        return self._with_imap(run)

    def forward_message(self, folder, uid, to, body="", cc="", bcc="", html_body="",
                        attachments=None, send=False, uid_validity=""):
        def run(conn):
            original, validity = self._source_message(conn, folder, uid, uid_validity)
            subject = str(original.get("Subject", ""))
            if not re.match(r"(?i)^\s*(fwd?|wg)\s*:", subject):
                subject = "Fwd: " + subject
            plain_original = _text_part(original, "text/plain")
            html_original = _text_part(original, "text/html")
            marker = ("\n\n---------- Forwarded message ----------\n"
                      f"From: {original.get('From', '')}\nDate: {original.get('Date', '')}\n"
                      f"Subject: {original.get('Subject', '')}\nTo: {original.get('To', '')}\n\n")
            forward_body = _content(body, "body") + marker + plain_original
            forward_html = html_body
            if html_original:
                forward_html = (_content(html_body, "html_body")
                                + "<hr><div><b>Forwarded message</b></div>" + html_original)
            preserved = [{"kind": "part", "part": part,
                          "data": _part_data(part)} for part in _message_files(original)]
            # Signed/encrypted messages cannot safely be dismantled.  Carry the
            # original RFC822 entity as a file, preserving its complete MIME form.
            if original.get_content_type() in {"multipart/signed", "multipart/encrypted"}:
                raw_original = original.as_bytes(policy=email.policy.SMTP)
                preserved = [{"kind": "bytes", "data": raw_original,
                              "filename": "forwarded-message.eml", "content_type": "message/rfc822"}]
            message, recipients = self._compose_with_connection(
                conn, to, subject, forward_body, cc, bcc, forward_html, attachments,
                extra_parts=preserved)
            if send:
                result = self._add_sent_copy(_smtp_send(self, message, recipients), message, conn)
            else:
                draft_folder = self._draft_folder(conn)
                result = {**self._append_draft(conn, draft_folder, message), "folder": draft_folder}
            return {**result, "source": {"folder": folder, "uid": str(uid), "uid_validity": validity},
                    "preserved_file_count": len(preserved)}
        return self._with_imap(run)

