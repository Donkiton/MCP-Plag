"""Bounded message reading and exact-UID bulk operations.

This module deliberately imports :mod:`mailbox` helpers inside methods.  Mailbox
inherits this mixin, so importing them at module import time would create a
circular import.
"""
from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import hmac
import imaplib
import json
import re
import time
from typing import Any


_UID_RE = re.compile(r"[1-9][0-9]{0,19}")
_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")
_MAX_BULK = 200_000
_MAX_READ_BODY = 200_000


def _symbols():
    from .mailbox import (MailError, _decode, _encode_folder, _folder_arg,
                          _folder_label, _preview)
    return MailError, _decode, _encode_folder, _folder_arg, _folder_label, _preview


def _ids(value: Any) -> set[str]:
    """Return normalized RFC message identifiers, never subject-like text."""
    if not value:
        return set()
    return {match.casefold() for match in _MESSAGE_ID_RE.findall(str(value))}


class MessageOpsMixin:
    """Mixin for read-only batch reads, thread traversal, and bulk mutations."""

    @staticmethod
    def _body_limit(value: Any, default: int) -> int:
        MailError = _symbols()[0]
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise MailError("body_limit must be an integer") from None
        if not 0 <= value <= _MAX_READ_BODY:
            raise MailError("body_limit must be between 0 and 200000")
        return value

    def _message_result(self, folder: str, uid: str, msg, body_limit: int,
                        uid_validity: str) -> dict[str, Any]:
        _, _, _, _, _, preview = _symbols()
        raw_body = preview(msg, body_limit + 1)
        truncated = len(raw_body) > body_limit
        body = raw_body[:body_limit]
        attachments = []
        for index, part in enumerate(msg.walk()):
            if part.get_content_disposition() == "attachment" or part.get_filename():
                payload = part.get_payload(decode=True) or b""
                attachments.append({
                    "index": index,
                    "filename": _symbols()[1](part.get_filename() or "attachment"),
                    "content_type": part.get_content_type(),
                    "size": len(payload),
                })
        return {
            **self._meta(folder, uid, msg),
            "body": body,
            "body_truncated": truncated,
            "body_truncated_at": body_limit if truncated else None,
            "attachments": attachments,
            "uid_validity": uid_validity,
            "message_fingerprint": hashlib.sha256(msg.as_bytes()).hexdigest(),
        }

    @staticmethod
    def _validate_refs(messages: Any) -> list[dict[str, str]]:
        MailError, _, _, folder_arg, _, _ = _symbols()
        if not isinstance(messages, list) or not messages or len(messages) > 20:
            raise MailError("Provide between 1 and 20 message references")
        refs = []
        for value in messages:
            if not isinstance(value, dict):
                raise MailError("Each message reference must contain folder and uid")
            folder = value.get("folder")
            uid = value.get("uid")
            validity = value.get("uid_validity", "")
            folder_arg(folder)
            if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
                raise MailError("Each message reference must contain a positive UID string")
            if validity and (not isinstance(validity, str) or not _UID_RE.fullmatch(validity)):
                raise MailError("Invalid UIDVALIDITY in message reference")
            refs.append({"folder": folder, "uid": uid, "uid_validity": validity})
        return refs

    def _fetch_read_bounded(self, conn, uid: str, deadline: float, headers_only=False):
        """The read-budget equivalent of Mailbox._fetch.

        A full fetch uses two IMAP commands. Resetting the socket timeout from
        the shared deadline before *each* command keeps the whole page inside
        the existing 20-second read budget.
        """
        MailError = _symbols()[0]
        if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
            raise MailError("Invalid UID")
        if not headers_only:
            self._read_budget(conn, deadline)
            typ, size_rows = conn.uid("FETCH", uid, "(RFC822.SIZE)")
            if typ != "OK":
                raise MailError("Could not inspect message size")
            chunks = []
            for value in size_rows or []:
                if isinstance(value, bytes):
                    chunks.append(value)
                elif isinstance(value, tuple) and isinstance(value[0], bytes):
                    chunks.append(value[0])
            sizes = re.findall(rb"RFC822.SIZE\s+(\d+)", b" ".join(chunks))
            if not sizes:
                raise MailError("Message not found or size metadata unavailable; refresh the folder and UID")
            if int(sizes[0]) > 12_000_000:
                raise MailError("Message exceeds 12 MB download limit; headers remain available")
        selector = "(BODY.PEEK[HEADER])" if headers_only else "(BODY.PEEK[])"
        self._read_budget(conn, deadline)
        typ, rows = conn.uid("FETCH", uid, selector)
        if typ != "OK":
            raise MailError("Fetch failed")
        raw = b"".join(value[1] for value in rows or []
                       if isinstance(value, tuple) and isinstance(value[1], bytes))
        if not raw:
            raise MailError("Message not found")
        return email.message_from_bytes(raw, policy=email.policy.default)

    def read_messages(self, messages, body_limit=12000):
        """Read at most twenty messages without setting ``\\Seen``."""
        refs = self._validate_refs(messages)
        body_limit = self._body_limit(body_limit, 12000)

        def run(conn, deadline):
            results: list[dict[str, Any] | None] = [None] * len(refs)
            body_chars = 0
            interrupted = False
            current_folder = None
            current_validity = ""
            for index, ref in enumerate(refs):
                if interrupted:
                    results[index] = {**ref, "status": "not_read", "error": "Read time budget exhausted"}
                    continue
                try:
                    if ref["folder"] != current_folder:
                        self._read_budget(conn, deadline)
                        self._select(conn, ref["folder"], readonly=True)
                        current_folder = ref["folder"]
                        current_validity = self._uid_validity(conn)
                    if ref["uid_validity"] and ref["uid_validity"] != current_validity:
                        raise _symbols()[0]("Folder UIDVALIDITY changed; refresh this message reference")
                    remaining = _MAX_READ_BODY - body_chars
                    msg = self._fetch_read_bounded(conn, ref["uid"], deadline)
                    item = self._message_result(ref["folder"], ref["uid"], msg,
                                                min(body_limit, max(0, remaining)), current_validity)
                    body_chars += len(item["body"])
                    results[index] = {"status": "read", **item}
                except (TimeoutError, OSError, imaplib.IMAP4.abort):
                    interrupted = True
                    results[index] = {**ref, "status": "not_read", "error": "Read time budget exhausted"}
                except Exception as exc:
                    # A missing or changed message is a per-item result; other valid
                    # references can still be read safely.
                    results[index] = {**ref, "status": "not_read", "error": str(exc)}
            items = [item for item in results if item is not None]
            errors = sum(item.get("status") != "read" for item in items)
            truncated = any(bool(item.get("body_truncated")) for item in items)
            return {
                "status": "complete" if not errors and not interrupted and not truncated else "incomplete",
                "items": items,
                "results": items,
                "requested_count": len(refs),
                "read_count": len(refs) - errors,
                "error_count": errors,
                "body_character_count": body_chars,
                "body_output_limit": _MAX_READ_BODY,
                "incomplete": bool(errors or interrupted or truncated),
                "interrupted": interrupted,
                "note": "BODY.PEEK was used; reading does not set Seen. Per-message errors are final for this request.",
            }

        return self._with_readonly(run)

    # ---- Thread traversal -------------------------------------------------

    def _thread_cursor(self, folder: str, uid: str, state=None, cursor=""):
        MailError = _symbols()[0]
        binding = hashlib.sha256(json.dumps(
            [self.username, folder, uid], ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()
        key = hashlib.sha256(("mail-thread-cursor:" + self.password).encode()).digest()
        if state is not None:
            raw = json.dumps({"v": 1, "binding": binding, **state},
                             ensure_ascii=False, separators=(",", ":")).encode()
            if len(raw) > 48_000:
                raise MailError("Thread cursor state is too large; narrow the requested thread")
            return (base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." +
                    hmac.new(key, raw, hashlib.sha256).hexdigest())
        try:
            if not isinstance(cursor, str) or len(cursor) > 65_536:
                raise ValueError
            encoded, signature = cursor.split(".")
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            if not hmac.compare_digest(signature, hmac.new(key, raw, hashlib.sha256).hexdigest()):
                raise ValueError
            value = json.loads(raw)
            if value.get("v") != 1 or value.get("binding") != binding:
                raise ValueError
            paths = value["paths"]
            ids = value["ids"]
            resolved = value["resolved"]
            seen = value["seen"]
            folder_validity = value["folder_validity"]
            if (not isinstance(paths, list) or not 1 <= len(paths) <= 100 or
                    not isinstance(ids, list) or len(ids) > 200 or
                    not isinstance(resolved, list) or len(resolved) > 200 or
                    not isinstance(seen, list) or len(seen) > 2000 or
                    not isinstance(folder_validity, dict) or len(folder_validity) > 100 or
                    type(value["index"]) is not int or not 0 <= value["index"] <= len(paths) or
                    type(value["passes"]) is not int or not 0 <= value["passes"] <= 5 or
                    type(value.get("missing_headers", 0)) is not int or value.get("missing_headers", 0) < 0 or
                    type(value.get("relationship_overflow", False)) is not bool):
                raise ValueError
            for path in paths:
                _symbols()[3](path)
            if any(not isinstance(item, str) or not _MESSAGE_ID_RE.fullmatch(item)
                   for item in ids + resolved):
                raise ValueError
            for item in seen:
                if not isinstance(item, str):
                    raise ValueError
                parts = item.split("\x1f")
                if len(parts) != 2 or parts[0] not in paths or not _UID_RE.fullmatch(parts[1]):
                    raise ValueError
            if any(path not in paths or not isinstance(validity, str) or not _UID_RE.fullmatch(validity)
                   for path, validity in folder_validity.items()):
                raise ValueError
            before = value.get("before", "")
            if before and (not isinstance(before, str) or not _UID_RE.fullmatch(before)):
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise MailError("Invalid thread cursor for this account/message; start a fresh thread read") from None

    @staticmethod
    def _fetch_thread_headers(conn, folder: str, uids: list[str]):
        MailError = _symbols()[0]
        if not uids:
            return []
        selector = "(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])"
        typ, rows = conn.uid("FETCH", ",".join(uids), selector)
        if typ != "OK":
            raise MailError("Thread header batch fetch failed")
        requested = set(uids)
        found = {}
        for row in rows or []:
            if not isinstance(row, tuple) or not isinstance(row[0], bytes) or not isinstance(row[1], bytes):
                continue
            match = re.search(rb"\bUID\s+([1-9][0-9]*)\b", row[0])
            if not match:
                raise MailError("Thread header response missing UID")
            item_uid = match.group(1).decode("ascii")
            if item_uid in requested:
                found[item_uid] = email.message_from_bytes(row[1], policy=email.policy.default)
        return [(item_uid, found[item_uid]) for item_uid in uids if item_uid in found]

    def read_thread(self, folder, uid, limit=20, cursor="", body_limit=6000):
        MailError, _, _, folder_arg, _, _ = _symbols()
        folder_arg(folder)
        if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
            raise MailError("Invalid thread seed UID")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise MailError("limit must be an integer") from None
        limit = max(1, min(limit, 20))
        body_limit = self._body_limit(body_limit, 6000)
        resumed = self._thread_cursor(folder, uid, cursor=cursor) if cursor else None

        def run(conn, deadline):
            output = []
            body_chars = 0
            interrupted = False
            restart_required = False
            if resumed:
                state = dict(resumed)
                paths = state["paths"]
                known = set(state["ids"])
                resolved = set(state["resolved"])
                seen = set(state["seen"])
                folder_validity = dict(state["folder_validity"])
                index = state["index"]
                before = state.get("before", "")
                passes = state["passes"]
                pass_grew = bool(state.get("pass_grew", False))
                omitted = int(state.get("omitted", 0))
                missing_headers = int(state.get("missing_headers", 0))
                relationship_overflow = bool(state.get("relationship_overflow", False))
                source_validity = state["source_validity"]
                seed_id = state["seed_id"]
                # A cursor must never silently follow a reused UID.
                self._read_budget(conn, deadline)
                self._select(conn, folder, readonly=True)
                if self._uid_validity(conn) != source_validity:
                    raise MailError("Thread source UIDVALIDITY changed; restart thread reading")
                self._read_budget(conn, deadline)
                seed_now = self._fetch_read_bounded(conn, uid, deadline, headers_only=True)
                if next(iter(sorted(_ids(seed_now.get("Message-ID")))), "") != seed_id:
                    raise MailError("Thread seed identity changed; restart thread reading")
                self._read_budget(conn, deadline)
                current_folders = [item for item in self._folders(conn)
                                   if "\\noselect" not in item["flags"].casefold().split()]
                current_paths = [item["folder"] for item in current_folders[:100]]
                if folder not in current_paths:
                    current_paths[-1] = folder
                if set(current_paths) != set(paths):
                    restart_required = True
            else:
                self._read_budget(conn, deadline)
                folders = [item for item in self._folders(conn)
                           if "\\noselect" not in item["flags"].casefold().split()]
                exact_source = [item for item in folders if item["folder"] == folder]
                if len(exact_source) != 1:
                    raise MailError("Thread source folder is missing or ambiguous")
                omitted = max(0, len(folders) - 100)
                paths = [item["folder"] for item in folders[:100]]
                if folder not in paths:
                    paths[-1] = folder
                self._read_budget(conn, deadline)
                self._select(conn, folder, readonly=True)
                source_validity = self._uid_validity(conn)
                seed = self._fetch_read_bounded(conn, uid, deadline)
                seed_ids = _ids(seed.get("Message-ID"))
                seed_id = next(iter(sorted(seed_ids)), "")
                known = set(seed_ids) | _ids(seed.get("In-Reply-To")) | _ids(seed.get("References"))
                resolved = set(seed_ids)
                seed_item = self._message_result(folder, uid, seed, body_limit, source_validity)
                output.append(seed_item)
                body_chars += len(seed_item["body"])
                seen = {folder + "\x1f" + uid}
                folder_validity = {folder: source_validity}
                missing_headers = 0
                relationship_overflow = False
                index = 0
                before = ""
                passes = 0
                pass_grew = False
                if not seed_id:
                    return {
                        "status": "incomplete", "messages": output, "items": output,
                        "returned_count": len(output), "has_more": False, "next_cursor": None,
                        "incomplete": True, "interrupted": False,
                        "note": "The seed has no Message-ID, so cross-folder thread membership cannot be established safely.",
                    }

            # Scan bounded header batches. A new relationship triggers another
            # pass, making discovery order-independent without subject matching.
            scanned = 0
            scan_cap = 2000
            while (not restart_required and len(output) < limit and
                   scanned < scan_cap and passes < 5):
                if index >= len(paths):
                    passes += 1
                    if not pass_grew:
                        break
                    index = 0
                    before = ""
                    pass_grew = False
                    continue
                path = paths[index]
                try:
                    self._read_budget(conn, deadline)
                    self._select(conn, path, readonly=True)
                    validity = self._uid_validity(conn)
                    expected = folder_validity.get(path, "")
                    if expected and expected != validity:
                        raise MailError("A thread folder UIDVALIDITY changed; restart thread reading")
                    folder_validity[path] = validity
                    self._read_budget(conn, deadline)
                    all_uids = sorted(self._all_uids(conn), key=int, reverse=True)
                    candidates = [item for item in all_uids if not before or int(item) < int(before)]
                    batch = candidates[:min(250, scan_cap - scanned)]
                    if not batch:
                        index += 1
                        before = ""
                        continue
                    self._read_budget(conn, deadline)
                    headers = self._fetch_thread_headers(conn, path, batch)
                    missing_headers += len(batch) - len(headers)
                    scanned += len(batch)
                    stopped_at = ""
                    for item_uid, header in headers:
                        key = path + "\x1f" + item_uid
                        mids = _ids(header.get("Message-ID"))
                        related = mids & known or _ids(header.get("In-Reply-To")) & known or _ids(header.get("References")) & known
                        if not related:
                            continue
                        resolved.update(mids)
                        newly_known = (mids | _ids(header.get("In-Reply-To")) | _ids(header.get("References"))) - known
                        if newly_known:
                            known.update(newly_known)
                            if len(known) > 200:
                                relationship_overflow = True
                                known = set(sorted(known)[:200])
                                resolved.intersection_update(known)
                            pass_grew = True
                        if key in seen:
                            continue
                        if len(output) >= limit:
                            break
                        full = self._fetch_read_bounded(conn, item_uid, deadline)
                        remaining = _MAX_READ_BODY - body_chars
                        item = self._message_result(path, item_uid, full, min(body_limit, remaining), validity)
                        output.append(item)
                        body_chars += len(item["body"])
                        seen.add(key)
                        if len(output) >= limit:
                            stopped_at = item_uid
                            break
                    more_here = len(candidates) > len(batch)
                    if stopped_at:
                        before = stopped_at
                    elif more_here:
                        before = batch[-1]
                    else:
                        index += 1
                        before = ""
                except (TimeoutError, OSError, imaplib.IMAP4.abort):
                    interrupted = True
                    break
                except MailError:
                    if resumed:
                        restart_required = True
                        break
                    raise

            exhausted = index >= len(paths) and not pass_grew
            pass_cap_reached = passes >= 5 and not exhausted
            if pass_cap_reached:
                restart_required = True
            limited = scanned >= scan_cap or pass_cap_reached or len(output) >= limit
            has_more = not exhausted and not restart_required
            next_cursor = None
            if has_more:
                cursor_state = {
                    "paths": paths, "index": index, "before": before,
                    "ids": sorted(known), "resolved": sorted(resolved),
                    "seen": sorted(seen), "passes": passes,
                    "pass_grew": pass_grew, "omitted": omitted,
                    "source_validity": source_validity, "seed_id": seed_id,
                    "folder_validity": folder_validity,
                    "missing_headers": missing_headers,
                    "relationship_overflow": relationship_overflow,
                }
                try:
                    next_cursor = self._thread_cursor(folder, uid, state=cursor_state)
                except MailError:
                    has_more = False
                    restart_required = True
            unresolved = known - resolved
            body_truncated_count = sum(bool(item.get("body_truncated")) for item in output)
            incomplete = bool(has_more or interrupted or restart_required or omitted or limited or
                              missing_headers or unresolved or relationship_overflow or
                              body_truncated_count)
            return {
                "status": "incomplete" if incomplete else "complete",
                "messages": output, "items": output, "returned_count": len(output),
                "has_more": has_more, "next_cursor": next_cursor,
                "incomplete": incomplete, "interrupted": interrupted,
                "restart_required": restart_required,
                "folders_in_scope": len(paths), "omitted_folder_count": omitted,
                "headers_scanned_this_page": scanned,
                "missing_header_count": missing_headers,
                "unresolved_reference_count": len(unresolved),
                "relationship_id_limit_reached": relationship_overflow,
                "body_character_count": body_chars,
                "body_truncated_count": body_truncated_count,
                "note": "Membership uses only Message-ID, In-Reply-To, and References. Continue with next_cursor while has_more is true; incomplete never asserts mailbox-wide completeness.",
            }

        return self._with_readonly(run)

    # ---- Exact UID mutations ---------------------------------------------

    @staticmethod
    def _validate_bulk(folder, uids, uid_validity="", target=None):
        MailError, _, _, folder_arg, _, _ = _symbols()
        folder_arg(folder)
        if target is not None:
            folder_arg(target)
            if folder == target:
                raise MailError("Source and destination folders are the same")
        if (not isinstance(uids, list) or not uids or len(uids) > _MAX_BULK or
                any(not isinstance(value, str) or not _UID_RE.fullmatch(value) for value in uids) or
                len(set(uids)) != len(uids)):
            raise MailError("Provide 1 to 200000 unique individual positive UIDs")
        if uid_validity and (not isinstance(uid_validity, str) or not _UID_RE.fullmatch(uid_validity)):
            raise MailError("Invalid UIDVALIDITY")
        return list(uids)

    @staticmethod
    def _emit_progress(progress, value):
        if progress:
            progress(value)

    def _mutation_preflight(self, conn, folder, uids, uid_validity, target=None):
        MailError = _symbols()[0]
        folders = self._folders(conn)
        source_matches = [item for item in folders if item["folder"] == folder]
        if len(source_matches) != 1:
            raise MailError("Source folder is missing or ambiguous; refresh folders")
        if "\\noselect" in source_matches[0]["flags"].casefold().split():
            raise MailError("Source folder is not selectable")
        if target is not None:
            target_matches = [item for item in folders if item["folder"] == target]
            if len(target_matches) != 1:
                raise MailError("Target folder is missing or ambiguous; refresh folders")
            if "\\noselect" in target_matches[0]["flags"].casefold().split():
                raise MailError("Target folder is not selectable")
        self._select(conn, folder, readonly=False)
        actual_validity = self._uid_validity(conn)
        if uid_validity and uid_validity != actual_validity:
            raise MailError("Folder UIDVALIDITY changed; no messages were changed")
        existing = set(self._all_uids(conn))
        missing = [item for item in uids if item not in existing]
        if missing:
            raise MailError(f"{len(missing)} selected UIDs are missing; no messages were changed")
        # Re-read identity immediately before the first mutation. This protects
        # an operation whose mailbox was reselected or recreated during preflight.
        if self._uid_validity(conn) != actual_validity:
            raise MailError("Folder UIDVALIDITY changed during preflight; no messages were changed")
        return actual_validity

    def _require_mutation_validity(self, conn, folder, captured_validity):
        """Reselect source and reject UID reuse before mutation or verification."""
        MailError = _symbols()[0]
        self._select(conn, folder, readonly=False)
        if self._uid_validity(conn) != captured_validity:
            raise MailError(
                "Folder UIDVALIDITY changed during the bulk operation; "
                "remaining messages were not changed"
            )

    def _move_bulk_selected(self, conn, folder, uids, target, validity, progress=None,
                            created=False, action="move_messages"):
        total = len(uids)
        completed = 0
        error = ""
        uncertain = False
        self._emit_progress(progress, {
            "phase": "snapshot_captured", "action": action,
            "requested_count": total, "completed_count": 0, "verified_count": 0,
        })
        for uid_set, count in self._uid_chunks(uids):
            self._emit_progress(progress, {
                "phase": "mutating", "action": action,
                "requested_count": total, "completed_count": completed,
                "current_chunk_count": count,
            })
            try:
                self._require_mutation_validity(conn, folder, validity)
                self._move_selected(conn, uid_set, target)
                completed += count
            except Exception as exc:
                error = str(exc)
                uncertain = True
                break
            self._emit_progress(progress, {
                "phase": "chunk_acknowledged", "action": action,
                "requested_count": total, "completed_count": completed,
            })
        try:
            self._require_mutation_validity(conn, folder, validity)
            remaining = set(self._all_uids(conn))
            verified = sum(item not in remaining for item in uids)
            verification_complete = True
        except Exception as exc:
            verified = 0
            verification_complete = False
            uncertain = True
            error = error or str(exc)
        partial = completed != total or verified != total
        status = "moved" if not partial and verification_complete else "partial_or_uncertain"
        result = {
            "status": status, "action": action, "folder": folder,
            "destination": target, "uid_validity": validity,
            "requested_count": total, "completed_count": completed,
            "verified_count": verified, "unverified_count": total - verified,
            "partial": partial, "uncertain": uncertain or not verification_complete,
            "created_archive_folder": bool(created),
            "error": error or None,
            "note": "Only the captured UIDs were targeted. Never automatically retry a partial or uncertain mutation; inspect current state first.",
        }
        self._emit_progress(progress, {"phase": "finished", **result})
        return result

    def move_messages(self, folder, uids, target, uid_validity="", progress=None):
        uids = self._validate_bulk(folder, uids, uid_validity, target)

        def run(conn):
            validity = self._mutation_preflight(conn, folder, uids, uid_validity, target)
            return self._move_bulk_selected(conn, folder, uids, target, validity, progress)

        return self._with_imap(run)

    def _archive_target(self, conn):
        MailError, _, encode_folder, folder_arg, _, _ = _symbols()
        folders = self._folders(conn)
        matches = [item for item in folders if self._system_kind(item) == "archive"]
        if len(matches) > 1:
            raise MailError("Archive folder is ambiguous; no messages were changed")
        if len(matches) == 1:
            return matches[0]["folder"], False
        wire = encode_folder("Архив")
        # create_folder rejects reserved names by design. Archive creation is an
        # explicit service operation, so use IMAP CREATE and verify discovery.
        typ, _ = conn.create(folder_arg(wire))
        if typ != "OK":
            raise MailError("Archive folder is absent and CREATE failed; no messages were changed")
        folders = self._folders(conn)
        matches = [item for item in folders
                   if self._system_kind(item) == "archive" or item["folder"] == wire]
        unique = {item["folder"]: item for item in matches}
        if len(unique) != 1 or wire not in unique:
            raise MailError("Created Archive folder could not be uniquely verified; no messages were changed")
        return wire, True

    def archive_messages(self, folder, uids, uid_validity="", progress=None):
        uids = self._validate_bulk(folder, uids, uid_validity)

        def run(conn):
            # Validate the complete source snapshot before a missing Archive
            # folder is created. Invalid UIDs therefore cannot create anything.
            existing = [item for item in self._folders(conn)
                        if self._system_kind(item) == "archive"]
            if len(existing) > 1:
                raise _symbols()[0]("Archive folder is ambiguous; no messages were changed")
            if not existing:
                self._mutation_preflight(conn, folder, uids, uid_validity)
            target, created = self._archive_target(conn)
            if target == folder:
                raise _symbols()[0]("Messages are already in Archive")
            validity = self._mutation_preflight(conn, folder, uids, uid_validity, target)
            return self._move_bulk_selected(conn, folder, uids, target, validity,
                                            progress, created, "archive_messages")

        return self._with_imap(run)

    @staticmethod
    def _fetch_flags(conn, uid_set):
        MailError = _symbols()[0]
        typ, rows = conn.uid("FETCH", uid_set, "(UID FLAGS)")
        if typ != "OK":
            raise MailError("Could not verify message flags")
        result = {}
        for row in rows or []:
            raw = row[0] if isinstance(row, tuple) else row
            if not isinstance(raw, bytes):
                continue
            uid_match = re.search(rb"\bUID\s+([1-9][0-9]*)\b", raw)
            flags_match = re.search(rb"\bFLAGS\s+\(([^)]*)\)", raw, re.I)
            if uid_match and flags_match:
                flags = {value.decode("ascii", "ignore").casefold() for value in flags_match.group(1).split()}
                result[uid_match.group(1).decode("ascii")] = "\\seen" in flags
        return result

    def mark_messages_read(self, folder, uids, read, uid_validity="", progress=None):
        MailError = _symbols()[0]
        uids = self._validate_bulk(folder, uids, uid_validity)
        if type(read) is not bool:
            raise MailError("read must be true or false")

        def run(conn):
            validity = self._mutation_preflight(conn, folder, uids, uid_validity)
            total = len(uids)
            completed = 0
            error = ""
            self._emit_progress(progress, {
                "phase": "snapshot_captured", "action": "mark_messages_read",
                "requested_count": total, "completed_count": 0, "verified_count": 0,
            })
            for uid_set, count in self._uid_chunks(uids):
                try:
                    self._require_mutation_validity(conn, folder, validity)
                    operation = "+FLAGS.SILENT" if read else "-FLAGS.SILENT"
                    typ, _ = conn.uid("STORE", uid_set, operation, "(\\Seen)")
                    if typ != "OK":
                        raise MailError("STORE failed")
                    completed += count
                except Exception as exc:
                    error = str(exc)
                    break
                self._emit_progress(progress, {
                    "phase": "chunk_acknowledged", "action": "mark_messages_read",
                    "requested_count": total, "completed_count": completed,
                })
            states = {}
            verification_error = ""
            try:
                self._require_mutation_validity(conn, folder, validity)
                for uid_set, _ in self._uid_chunks(uids):
                    states.update(self._fetch_flags(conn, uid_set))
            except Exception as exc:
                verification_error = str(exc)
            verified_uids = {item for item in uids if states.get(item) is read}
            verified = len(verified_uids)
            item_limit = 1000
            items = [{"uid": item, "read": states.get(item),
                      "verified": item in verified_uids} for item in uids[:item_limit]]
            partial = completed != total or verified != total
            result = {
                "status": "updated" if not partial else "partial_or_uncertain",
                "action": "mark_messages_read", "folder": folder,
                "uid_validity": validity, "read": read,
                "requested_count": total, "completed_count": completed,
                "verified_count": verified, "unverified_count": total - verified,
                "partial": partial,
                "uncertain": bool(error or verification_error or verified != completed),
                "items": items, "items_truncated": len(uids) > item_limit,
                "error": error or verification_error or None,
                "note": "STORE changed only the Seen flag and preserved every other flag. Never automatically retry a partial or uncertain mutation.",
            }
            self._emit_progress(progress, {"phase": "finished", **result})
            return result

        return self._with_imap(run)
