import email.policy
import base64
import json
import re
import time
import unittest
from email.message import EmailMessage

from mailru_mcp.mailbox import MailError, Mailbox, _encode_folder
from mailru_mcp.message_ops import MessageOpsMixin


Ops = Mailbox if issubclass(Mailbox, MessageOpsMixin) else type(
    "Ops", (MessageOpsMixin, Mailbox), {}
)


def raw_message(mid, subject="subject", body="body", refs="", reply_to=""):
    msg = EmailMessage()
    msg["From"] = "from@example.test"
    msg["To"] = "to@example.test"
    msg["Subject"] = subject
    msg["Message-ID"] = mid
    if refs:
        msg["References"] = refs
    if reply_to:
        msg["In-Reply-To"] = reply_to
    msg.set_content(body)
    return msg.as_bytes(policy=email.policy.default)


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeImap:
    def __init__(self, folders, validity="7", capabilities=(b"MOVE", b"UIDPLUS")):
        # folders: name -> {flags: str, messages: uid -> {raw, flags}}
        self.folders = folders
        self.validity = validity
        self.capabilities = capabilities
        self.selected = None
        self.readonly_selects = []
        self.uid_calls = []
        self.create_calls = []
        self.create_result = "OK"
        self.move_fail_after = None
        self.move_count = 0
        self.store_count = 0
        self.change_validity_after_move = None
        self.change_validity_after_store = None
        self.sock = FakeSocket()

    def _set_selected_validity(self, value):
        if isinstance(self.validity, dict):
            self.validity[self.selected] = value
        else:
            self.validity = value

    def list(self):
        rows = []
        for name, data in self.folders.items():
            rows.append(f'({data.get("flags", "")}) "/" "{name}"'.encode())
        return "OK", rows

    def select(self, folder_arg, readonly=True):
        name = self._unquote(folder_arg)
        if name not in self.folders:
            return "NO", []
        self.selected = name
        self.readonly_selects.append(readonly)
        return "OK", [str(len(self.folders[name]["messages"])).encode()]

    def response(self, name):
        value = self.validity[self.selected] if isinstance(self.validity, dict) else self.validity
        return name, [str(value).encode()]

    def create(self, folder_arg):
        name = self._unquote(folder_arg)
        self.create_calls.append(name)
        if self.create_result != "OK":
            return self.create_result, []
        self.folders[name] = {"flags": "", "messages": {}}
        return "OK", []

    @staticmethod
    def _unquote(value):
        if isinstance(value, bytes):
            value = value.decode()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return value.replace('\\"', '"').replace('\\\\', '\\')

    @staticmethod
    def _expand(uid_set):
        values = []
        for item in str(uid_set).split(","):
            if ":" in item:
                first, last = map(int, item.split(":"))
                values.extend(str(value) for value in range(first, last + 1))
            elif item:
                values.append(item)
        return values

    def uid(self, command, *args):
        command = command.upper()
        self.uid_calls.append((command, args))
        messages = self.folders[self.selected]["messages"]
        if command == "SEARCH":
            return "OK", [" ".join(sorted(messages, key=int)).encode()]
        if command == "FETCH":
            uid_set, selector = args
            uids = self._expand(uid_set)
            rows = []
            for uid in uids:
                item = messages.get(uid)
                if not item:
                    continue
                if "RFC822.SIZE" in selector:
                    rows.append(f"1 (UID {uid} RFC822.SIZE {len(item['raw'])})".encode())
                elif "FLAGS" in selector and "BODY" not in selector:
                    flags = " ".join(sorted(item["flags"]))
                    rows.append(f"1 (UID {uid} FLAGS ({flags}))".encode())
                else:
                    rows.append((f"1 (UID {uid} BODY[] {{{len(item['raw'])}}})".encode(), item["raw"]))
            return "OK", rows
        if command == "MOVE":
            self.move_count += 1
            if self.move_fail_after is not None and self.move_count > self.move_fail_after:
                return "NO", []
            uid_set, target_arg = args
            target = self._unquote(target_arg)
            for uid in self._expand(uid_set):
                if uid in messages:
                    self.folders[target]["messages"][uid] = messages.pop(uid)
            if self.change_validity_after_move == self.move_count:
                self._set_selected_validity("99")
            return "OK", []
        if command == "STORE":
            self.store_count += 1
            uid_set, operation, flags = args
            self.assert_seen_only = flags
            for uid in self._expand(uid_set):
                if uid not in messages:
                    continue
                if operation == "+FLAGS.SILENT":
                    messages[uid]["flags"].add("\\Seen")
                elif operation == "-FLAGS.SILENT":
                    messages[uid]["flags"].discard("\\Seen")
            if self.change_validity_after_store == self.store_count:
                self._set_selected_validity("99")
            return "OK", []
        if command == "COPY":
            return "OK", []
        if command == "EXPUNGE":
            return "OK", []
        raise AssertionError((command, args))


def folder(messages=None, flags=""):
    return {"flags": flags, "messages": messages or {}}


def item(raw, *flags):
    return {"raw": raw, "flags": set(flags)}


def make_box(fake):
    box = Ops("account@example.test", "secret")
    box._with_imap = lambda callback: callback(fake)
    box._with_readonly = lambda callback: callback(fake, time.monotonic() + 20)
    return box


class ReadMessagesTests(unittest.TestCase):
    def test_read_is_readonly_peek_and_does_not_change_seen(self):
        fake = FakeImap({"INBOX": folder({
            "1": item(raw_message("<one@example.test>", body="hello"), "\\Flagged")
        })})
        result = make_box(fake).read_messages([
            {"folder": "INBOX", "uid": "1", "uid_validity": "7"}
        ])

        self.assertEqual(result["read_count"], 1)
        self.assertEqual(result["items"][0]["body"].strip(), "hello")
        self.assertTrue(all(fake.readonly_selects))
        selectors = [args[1] for command, args in fake.uid_calls if command == "FETCH"]
        self.assertTrue(any("BODY.PEEK" in selector for selector in selectors))
        self.assertEqual(fake.folders["INBOX"]["messages"]["1"]["flags"], {"\\Flagged"})

    def test_per_item_missing_uid_is_honest(self):
        fake = FakeImap({"INBOX": folder({
            "1": item(raw_message("<one@example.test>"))
        })})
        result = make_box(fake).read_messages([
            {"folder": "INBOX", "uid": "1"},
            {"folder": "INBOX", "uid": "99"},
        ])
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["read_count"], 1)
        self.assertEqual(result["items"][1]["status"], "not_read")

    def test_total_body_cap_keeps_all_message_metadata(self):
        messages = {
            str(n): item(raw_message(f"<{n}@example.test>", body="x" * 12_000))
            for n in range(1, 21)
        }
        fake = FakeImap({"INBOX": folder(messages)})
        refs = [{"folder": "INBOX", "uid": str(n)} for n in range(1, 21)]
        result = make_box(fake).read_messages(refs, body_limit=12_000)
        self.assertEqual(result["read_count"], 20)
        self.assertEqual(sum(len(entry["body"]) for entry in result["items"]), 200_000)
        self.assertTrue(result["incomplete"])


class BulkMutationTests(unittest.TestCase):
    def test_missing_uid_refuses_every_mutation_before_move(self):
        fake = FakeImap({
            "INBOX": folder({"1": item(raw_message("<one@example.test>"))}),
            "Exact": folder(),
        })
        with self.assertRaisesRegex(MailError, "missing"):
            make_box(fake).move_messages("INBOX", ["1", "2"], "Exact", "7")
        self.assertFalse(any(command == "MOVE" for command, _ in fake.uid_calls))
        self.assertIn("1", fake.folders["INBOX"]["messages"])

    def test_move_uses_exact_target_and_reports_partial_state(self):
        messages = {str(n): item(raw_message(f"<{n}@example.test>")) for n in range(1, 1002)}
        fake = FakeImap({
            "INBOX": folder(messages),
            "Target": folder(),
            "Target old": folder(),
        })
        fake.move_fail_after = 1
        result = make_box(fake).move_messages(
            "INBOX", [str(n) for n in range(1, 1002)], "Target", "7"
        )
        self.assertEqual(result["status"], "partial_or_uncertain")
        self.assertEqual(result["requested_count"], 1001)
        self.assertEqual(result["completed_count"], 1000)
        self.assertEqual(result["verified_count"], 1000)
        move_targets = [FakeImap._unquote(args[1]) for command, args in fake.uid_calls if command == "MOVE"]
        self.assertEqual(move_targets, ["Target", "Target"])

    def test_move_stops_before_second_chunk_when_uidvalidity_changes(self):
        messages = {str(n): item(raw_message(f"<{n}@example.test>")) for n in range(1, 1002)}
        messages["2000"] = item(raw_message("<unrelated@example.test>"), "\\Flagged")
        fake = FakeImap({"INBOX": folder(messages), "Target": folder()})
        fake.change_validity_after_move = 1

        result = make_box(fake).move_messages(
            "INBOX", [str(n) for n in range(1, 1002)], "Target", "7"
        )

        self.assertEqual(result["status"], "partial_or_uncertain")
        self.assertEqual(result["completed_count"], 1000)
        self.assertEqual(result["verified_count"], 0)
        self.assertEqual(fake.move_count, 1)
        self.assertIn("1001", fake.folders["INBOX"]["messages"])
        self.assertEqual(fake.folders["INBOX"]["messages"]["2000"]["flags"], {"\\Flagged"})

    def test_mark_read_changes_only_seen_and_verifies_each_item(self):
        fake = FakeImap({"INBOX": folder({
            "1": item(raw_message("<one@example.test>"), "\\Flagged", "custom"),
            "2": item(raw_message("<two@example.test>"), "\\Answered"),
        })})
        result = make_box(fake).mark_messages_read("INBOX", ["1", "2"], True, "7")
        self.assertEqual(result["verified_count"], 2)
        self.assertEqual(fake.assert_seen_only, "(\\Seen)")
        self.assertEqual(fake.folders["INBOX"]["messages"]["1"]["flags"],
                         {"\\Flagged", "custom", "\\Seen"})
        self.assertEqual(fake.folders["INBOX"]["messages"]["2"]["flags"],
                         {"\\Answered", "\\Seen"})

    def test_mark_read_stops_before_second_chunk_when_uidvalidity_changes(self):
        messages = {str(n): item(raw_message(f"<{n}@example.test>")) for n in range(1, 1002)}
        messages["2000"] = item(raw_message("<unrelated@example.test>"), "\\Flagged")
        fake = FakeImap({"INBOX": folder(messages)})
        fake.change_validity_after_store = 1

        result = make_box(fake).mark_messages_read(
            "INBOX", [str(n) for n in range(1, 1002)], True, "7"
        )

        self.assertEqual(result["status"], "partial_or_uncertain")
        self.assertEqual(result["completed_count"], 1000)
        self.assertEqual(result["verified_count"], 0)
        self.assertEqual(fake.store_count, 1)
        self.assertNotIn("\\Seen", fake.folders["INBOX"]["messages"]["1001"]["flags"])
        self.assertEqual(fake.folders["INBOX"]["messages"]["2000"]["flags"], {"\\Flagged"})

    def test_archive_missing_uid_does_not_create_folder(self):
        fake = FakeImap({"INBOX": folder({
            "1": item(raw_message("<one@example.test>"))
        })})
        with self.assertRaisesRegex(MailError, "missing"):
            make_box(fake).archive_messages("INBOX", ["2"], "7")
        self.assertEqual(fake.create_calls, [])

    def test_archive_creation_failure_is_safe(self):
        fake = FakeImap({"INBOX": folder({
            "1": item(raw_message("<one@example.test>"))
        })})
        fake.create_result = "NO"
        with self.assertRaisesRegex(MailError, "CREATE failed"):
            make_box(fake).archive_messages("INBOX", ["1"], "7")
        self.assertEqual(fake.create_calls, [_encode_folder("Архив")])
        self.assertIn("1", fake.folders["INBOX"]["messages"])

    def test_archive_uses_special_use_folder_without_creating(self):
        fake = FakeImap({
            "INBOX": folder({"1": item(raw_message("<one@example.test>"))}),
            "Custom Archive": folder(flags="\\Archive"),
        })
        result = make_box(fake).archive_messages("INBOX", ["1"], "7")
        self.assertEqual(result["destination"], "Custom Archive")
        self.assertFalse(result["created_archive_folder"])
        self.assertEqual(fake.create_calls, [])


class ThreadTests(unittest.TestCase):
    def test_thread_uses_ids_and_does_not_conflate_same_subject(self):
        root = "<root@example.test>"
        fake = FakeImap({
            "INBOX": folder({
                "1": item(raw_message(root, subject="Same topic", body="root")),
                "2": item(raw_message("<unrelated@example.test>", subject="Same topic", body="wrong")),
            }),
            "Sent": folder({
                "3": item(raw_message("<reply@example.test>", subject="Same topic", body="reply",
                                      refs=root, reply_to=root)),
            }, flags="\\Sent"),
        })
        result = make_box(fake).read_thread("INBOX", "1", limit=20)
        ids = {entry["message_id"] for entry in result["messages"]}
        self.assertEqual(ids, {root, "<reply@example.test>"})
        self.assertNotIn("<unrelated@example.test>", ids)

    def test_thread_cursor_continues_and_detects_folder_rename(self):
        root = "<root@example.test>"
        fake = FakeImap({
            "INBOX": folder({"1": item(raw_message(root))}),
            "Sent": folder({"3": item(raw_message("<reply@example.test>", refs=root))}),
        })
        box = make_box(fake)
        first = box.read_thread("INBOX", "1", limit=1)
        self.assertTrue(first["has_more"])
        second = box.read_thread("INBOX", "1", limit=1, cursor=first["next_cursor"])
        self.assertEqual(second["messages"][0]["message_id"], "<reply@example.test>")

        # Continue from a fresh first page after changing a cursor-scoped path.
        fake2 = FakeImap({
            "INBOX": folder({"1": item(raw_message(root))}),
            "Sent": folder({"3": item(raw_message("<reply@example.test>", refs=root))}),
        })
        box2 = make_box(fake2)
        page = box2.read_thread("INBOX", "1", limit=1)
        fake2.folders["Renamed"] = fake2.folders.pop("Sent")
        resumed = box2.read_thread("INBOX", "1", limit=20, cursor=page["next_cursor"])
        self.assertTrue(resumed["restart_required"])
        self.assertTrue(resumed["incomplete"])

    def test_continuation_tracks_each_folders_distinct_uidvalidity(self):
        root = "<root@example.test>"
        fake = FakeImap({
            "INBOX": folder({"1": item(raw_message(root))}),
            "Sent": folder({"3": item(raw_message("<reply@example.test>", refs=root))}),
        }, validity={"INBOX": "11", "Sent": "22"})
        box = make_box(fake)
        first = box.read_thread("INBOX", "1", limit=2)
        self.assertTrue(first["has_more"])
        encoded = first["next_cursor"].split(".", 1)[0]
        state = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(state["folder_validity"], {"INBOX": "11", "Sent": "22"})

        second = box.read_thread("INBOX", "1", limit=2, cursor=first["next_cursor"])
        self.assertFalse(second["restart_required"])
        self.assertFalse(second["has_more"])


if __name__ == "__main__":
    unittest.main()
