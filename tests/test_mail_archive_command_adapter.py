from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.mail_archive_canary import _conflicting_uidvalidity
from core.mail_archive_command_adapter import (
    DEFAULT_STATE_ROOT,
    MailArchiveConflictError,
    MailArchiveCommandBridge,
    MailArchiveError,
    MailArchiveRequest,
    MailArchiveVerificationError,
    RECEIPT_SCHEMA,
    _display_snippet,
    _parse_copyuid,
    _parse_fetch_message,
)


FIXED_TIME = "2026-07-28T12:00:00Z"
EXECUTE_ID = "decision:execute:00000001"
UNDO_ID = "decision:undo:00000001"


@pytest.mark.parametrize("current", ("1", "1111", "9999999999999999999"))
def test_canary_conflict_uidvalidity_is_valid_and_different(
    current: str,
) -> None:
    conflicting = _conflicting_uidvalidity(current)
    request = MailArchiveRequest.from_mapping(
        icloud_proposal(uidvalidity=conflicting)["action"]["payload"]
    )
    assert request.uidvalidity > 0
    assert request.uidvalidity != int(current)


def icloud_proposal(**payload_overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "requested_verb": "archive",
        "provider": "icloud",
        "subject": "iCloud invoice",
        "account": "icloud",
        "origin_mailbox": "INBOX",
        "uidvalidity": "1111",
        "uid": "101",
        "rfc_message_id": "<icloud-101@example.com>",
        "x_gm_msgid": "",
        "observed_labels": [],
        "archive_method": "move",
        "destination_mailbox": "Archive",
    }
    payload.update(payload_overrides)
    return {
        "proposal_version": "duffields-action-proposal/v1",
        "proposal_id": "mail-archive-icloud-0001",
        "action": {
            "adapter": "mail",
            "kind": "mail.archive",
            "payload": payload,
            "readback_required": True,
            "undo_supported": True,
            "undo_window_seconds": 3600,
        },
        "target": {
            "system": "mail",
            "resource_type": "message",
            "scope": payload["account"],
            "display": "iCloud invoice",
        },
        "risk": {
            "level": "low",
            "reversible": True,
            "external_communication": False,
        },
    }


def gmail_proposal(**payload_overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "requested_verb": "archive",
        "provider": "gmail",
        "subject": "gmail invoice",
        "account": "gmail",
        "origin_mailbox": "INBOX",
        "uidvalidity": "2222",
        "uid": "202",
        "rfc_message_id": "<gmail-202@example.com>",
        "x_gm_msgid": "555000111222333444",
        "observed_labels": ["ALL", "INBOX", "Travel"],
        "archive_method": "gmail_label",
        "destination_mailbox": "",
    }
    payload.update(payload_overrides)
    return {
        "proposal_version": "duffields-action-proposal/v1",
        "proposal_id": "mail-archive-gmail-0001",
        "action": {
            "adapter": "mail",
            "kind": "mail.archive",
            "payload": payload,
            "readback_required": True,
            "undo_supported": True,
            "undo_window_seconds": 3600,
        },
        "target": {
            "system": "mail",
            "resource_type": "message",
            "scope": payload["account"],
            "display": "gmail invoice",
        },
        "risk": {
            "level": "low",
            "reversible": True,
            "external_communication": False,
        },
    }


@dataclass
class _ICloudMailbox:
    uidvalidity: int
    next_uid: int
    messages: dict[int, dict[str, object]]


class FakeMailArchiveBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.probe_calls: list[tuple[str, str | None]] = []
        self.icloud = {
            "INBOX": _ICloudMailbox(
                uidvalidity=1111,
                next_uid=150,
                messages={
                    101: {
                        "account": "icloud",
                        "provider": "icloud",
                        "mailbox": "INBOX",
                        "uidvalidity": 1111,
                        "uid": 101,
                        "message_id": "<icloud-101@example.com>",
                    }
                },
            ),
            "Archive": _ICloudMailbox(uidvalidity=9001, next_uid=500, messages={}),
        }
        self.gmail = {
            "account": "gmail",
            "provider": "gmail",
            "mailbox": "INBOX",
            "uidvalidity": 2222,
            "uid": 202,
            "message_id": "<gmail-202@example.com>",
            "gmail_msgid": "555000111222333444",
            "labels": ("ALL", "INBOX", "Travel"),
        }
        self.partial_mode: str | None = None

    def probe(self, account: str, mailbox: str | None = None) -> dict[str, object]:
        self.probe_calls.append((account, mailbox))
        return {
            "ok": True,
            "schema": "mail-archive-probe/v1",
            "account": account,
            "mailbox": mailbox,
            "capabilities": ["UIDPLUS", "X-GM-EXT-1"] if account == "gmail" else ["UIDPLUS"],
            "supports_uidplus": True,
            "supports_gmail": account == "gmail",
            "selected": {"mailbox": mailbox, "uidvalidity": 2222 if account == "gmail" else 1111}
            if mailbox
            else None,
            "uidvalidity": 2222 if account == "gmail" else 1111 if mailbox else None,
        }

    def execute(
        self, request: MailArchiveRequest, *, operation_id: str
    ) -> dict[str, object]:
        self.calls.append(("execute", request.provider))
        if request.provider == "gmail":
            return self._gmail_execute(request, operation_id=operation_id)
        return self._icloud_execute(request, operation_id=operation_id)

    def readback(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append(("readback", request.provider))
        after = execute_receipt["after"]
        if request.provider == "gmail":
            current = self._gmail_snapshot()
            normalized_after = dict(after)
            normalized_after["labels"] = tuple(normalized_after["labels"])
            if current != normalized_after:
                raise MailArchiveVerificationError("current provider state does not match the archive receipt")
            return {
                "x_gm_msgid": current["gmail_msgid"],
                "all_mail_present": True,
                "inbox_absent": "INBOX" not in current["labels"],
            }
        else:
            current = self._icloud_archive_snapshot()
        if current != after:
            raise MailArchiveVerificationError("current provider state does not match the archive receipt")
        if current["origin"]:
            raise MailArchiveVerificationError("origin is still present")
        return {
            "origin_absent": True,
            "destination_present": current["archive"] is not None,
            "destination_identity": {
                "account": request.account,
                "mailbox": request.archive_mailbox,
                "uidvalidity": str(current["archive"]["uidvalidity"]),
                "uid": str(current["archive"]["uid"]),
                "rfc_message_id": current["archive"]["message_id"],
            },
        }

    def undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: dict[str, object],
        approval_ref: str,
    ) -> dict[str, object]:
        _ = approval_ref
        self.calls.append(("undo", request.provider))
        before = execute_receipt["after"]
        if request.provider == "gmail":
            current = self._gmail_snapshot()
            normalized_before = dict(before)
            normalized_before["labels"] = tuple(normalized_before["labels"])
            if current != normalized_before:
                raise MailArchiveConflictError("gmail undo refused because the archived state changed")
            self.gmail = dict(self.gmail)
            labels = set(self.gmail["labels"])
            labels.add("INBOX")
            self.gmail["labels"] = tuple(sorted(labels))
            after = self._gmail_snapshot()
        else:
            current = self._icloud_archive_snapshot()
            if current["archive"] != before["archive"]:
                raise MailArchiveConflictError("iCloud undo refused because the archived copy changed")
            archive = self.icloud["Archive"]
            source = self.icloud["INBOX"]
            archived = archive.messages.pop(int(before["archive"]["uid"]))
            restored_uid = source.next_uid
            source.next_uid += 1
            source.messages[restored_uid] = {
                **archived,
                "mailbox": "INBOX",
                "uid": restored_uid,
                "uidvalidity": source.uidvalidity,
            }
            after = self._icloud_origin_snapshot()
        return {
            "ok": True,
            "schema": "mail-archive-undo-receipt/v1",
            "provider": request.provider,
            "account": request.account,
            "status": "restored",
            "effect_verified": True,
            "before": before,
            "after": after,
        }

    def readback_undo(
        self,
        request: MailArchiveRequest,
        *,
        execute_receipt: dict[str, object],
        undo_receipt: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append(("readback-undo", request.provider))
        previous = execute_receipt["before"]
        if request.provider == "gmail":
            current = self._gmail_snapshot()
            normalized_after = dict(undo_receipt["after"])
            normalized_after["labels"] = tuple(normalized_after["labels"])
            if current != normalized_after:
                raise MailArchiveVerificationError("undo readback does not match the restored provider state")
            return {
                "x_gm_msgid": current["gmail_msgid"],
                "restored_labels": list(current["labels"]),
            }
        else:
            current = self._icloud_origin_snapshot()
        if current != undo_receipt["after"]:
            raise MailArchiveVerificationError("undo readback does not match the restored provider state")
        return {
            "origin_present": bool(current["origin"]),
            "destination_absent": current["archive"] is None,
        }

    def _icloud_execute(
        self, request: MailArchiveRequest, *, operation_id: str
    ) -> dict[str, object]:
        _ = operation_id
        mailbox = self.icloud[request.origin_mailbox]
        archive = self.icloud[request.archive_mailbox]
        current = mailbox.messages.get(request.uid)
        if mailbox.uidvalidity != request.uidvalidity:
            raise MailArchiveConflictError("UIDVALIDITY changed before archive")
        if current is None:
            raise MailArchiveConflictError("message was not found in the origin mailbox")
        if current["message_id"] != request.message_id:
            raise MailArchiveConflictError("RFC Message-ID changed before archive")
        before = self._icloud_origin_snapshot()
        dest_uid = archive.next_uid
        archive.next_uid += 1
        copied = {
            **current,
            "mailbox": request.archive_mailbox,
            "uidvalidity": archive.uidvalidity,
            "uid": dest_uid,
        }
        archive.messages[dest_uid] = copied
        status = "archived"
        effect_verified = True
        if self.partial_mode == "icloud-copy-only":
            status = "uncertain"
            effect_verified = False
        else:
            mailbox.messages.pop(request.uid)
        after = self._icloud_archive_snapshot()
        return {
            "ok": effect_verified,
            "schema": RECEIPT_SCHEMA,
            "provider": request.provider,
            "account": request.account,
            "operation_id": operation_id,
            "status": status,
            "effect_verified": effect_verified,
            "before": before,
            "after": after,
            "undo": {
                "operation": "copy_back_and_delete_archive",
                "origin_mailbox": request.origin_mailbox,
                "archive_mailbox": request.archive_mailbox,
                "archive_uidvalidity": archive.uidvalidity,
                "archive_uid": dest_uid,
                "message_id": request.message_id,
            },
        }

    def _gmail_execute(
        self, request: MailArchiveRequest, *, operation_id: str
    ) -> dict[str, object]:
        _ = operation_id
        if self.gmail["uidvalidity"] != request.uidvalidity:
            raise MailArchiveConflictError("gmail UIDVALIDITY changed before archive")
        if self.gmail["uid"] != request.uid:
            raise MailArchiveConflictError("gmail UID changed before archive")
        if self.gmail["message_id"] != request.message_id:
            raise MailArchiveConflictError("gmail RFC Message-ID changed before archive")
        if self.gmail["gmail_msgid"] != request.gmail_msgid:
            raise MailArchiveConflictError("gmail X-GM-MSGID changed before archive")
        if tuple(request.labels) != tuple(self.gmail["labels"]):
            raise MailArchiveConflictError("gmail observed labels changed before archive")
        before = self._gmail_snapshot()
        labels = set(self.gmail["labels"])
        labels.discard("INBOX")
        self.gmail = dict(self.gmail)
        self.gmail["labels"] = tuple(sorted(labels))
        after = self._gmail_snapshot()
        return {
            "ok": True,
            "schema": RECEIPT_SCHEMA,
            "provider": request.provider,
            "account": request.account,
            "operation_id": operation_id,
            "status": "archived",
            "effect_verified": True,
            "before": before,
            "after": after,
            "undo": {
                "operation": "restore_inbox_label",
                "uid": request.uid,
                "gmail_msgid": request.gmail_msgid,
                "labels_before": list(before["labels"]),
                "labels_after": list(after["labels"]),
            },
        }

    def _icloud_origin_snapshot(self) -> dict[str, object]:
        source = self.icloud["INBOX"]
        messages = [
            {
                "mailbox": "INBOX",
                "uidvalidity": source.uidvalidity,
                "uid": uid,
                "message_id": message["message_id"],
            }
            for uid, message in sorted(source.messages.items())
        ]
        return {"origin": messages, "archive": None}

    def _icloud_archive_snapshot(self) -> dict[str, object]:
        source = self.icloud["INBOX"]
        archive = self.icloud["Archive"]
        origin_messages = [
            {
                "mailbox": "INBOX",
                "uidvalidity": source.uidvalidity,
                "uid": uid,
                "message_id": message["message_id"],
            }
            for uid, message in sorted(source.messages.items())
        ]
        archive_messages = [
            {
                "mailbox": "Archive",
                "uidvalidity": archive.uidvalidity,
                "uid": uid,
                "message_id": message["message_id"],
            }
            for uid, message in sorted(archive.messages.items())
        ]
        return {"origin": origin_messages, "archive": archive_messages[0] if archive_messages else None}

    def _gmail_snapshot(self) -> dict[str, object]:
        return {
            "mailbox": "All Mail",
            "uidvalidity": self.gmail["uidvalidity"],
            "uid": self.gmail["uid"],
            "message_id": self.gmail["message_id"],
            "gmail_msgid": self.gmail["gmail_msgid"],
            "labels": tuple(self.gmail["labels"]),
        }


def _bridge(tmp_path: Path) -> MailArchiveCommandBridge:
    return MailArchiveCommandBridge(
        FakeMailArchiveBackend(),
        state_root=tmp_path / "state",
        clock=lambda: FIXED_TIME,
    )


def test_execute_readback_undo_and_undo_readback_for_icloud(tmp_path: Path) -> None:
    backend = FakeMailArchiveBackend()
    subject = MailArchiveCommandBridge(backend, state_root=tmp_path / "state", clock=lambda: FIXED_TIME)
    document = icloud_proposal()

    executed = subject.dispatch("execute", {"proposal": document, "operation_id": EXECUTE_ID})
    assert executed["ok"] is True
    assert executed["reference"].startswith("mail-archive-operation:")
    assert executed["details"]["status"] == "archived"
    assert executed["details"]["effect_verified"] is True
    assert executed["state"]["archive"]["uid"] == 500

    replayed = subject.dispatch("execute", {"proposal": document, "operation_id": EXECUTE_ID})
    assert replayed["details"]["replayed"] is True
    assert [name for name, _ in backend.calls].count("execute") == 1

    readback = subject.dispatch("readback", {"proposal": document, "reference": executed["reference"]})
    assert readback["ok"] is True
    assert readback["observed"]["origin_absent"] is True

    undone = subject.dispatch(
        "undo",
        {
            "proposal": document,
            "reference": executed["reference"],
            "operation_id": UNDO_ID,
        },
    )
    assert undone["ok"] is True
    assert undone["state"]["origin"][0]["mailbox"] == "INBOX"

    undo_readback = subject.dispatch(
        "readback-undo",
        {"proposal": document, "reference": executed["reference"]},
    )
    assert undo_readback["ok"] is True
    assert undo_readback["observed"]["origin_present"] is True
    assert [name for name, _ in backend.calls].count("undo") == 1


def test_execute_readback_undo_and_undo_readback_for_gmail(tmp_path: Path) -> None:
    backend = FakeMailArchiveBackend()
    subject = MailArchiveCommandBridge(backend, state_root=tmp_path / "state", clock=lambda: FIXED_TIME)
    document = gmail_proposal()

    executed = subject.dispatch("execute", {"proposal": document, "operation_id": EXECUTE_ID})
    assert executed["details"]["status"] == "archived"
    assert executed["state"]["labels"] == ("ALL", "Travel")

    readback = subject.dispatch("readback", {"proposal": document, "reference": executed["reference"]})
    assert readback["observed"]["inbox_absent"] is True

    undone = subject.dispatch(
        "undo",
        {
            "proposal": document,
            "reference": executed["reference"],
            "operation_id": UNDO_ID,
        },
    )
    assert "INBOX" in undone["state"]["labels"]

    undo_readback = subject.dispatch(
        "readback-undo",
        {"proposal": document, "reference": executed["reference"]},
    )
    assert "INBOX" in undo_readback["observed"]["restored_labels"]


def test_uidvalidity_and_identity_conflicts_refuse_before_mutation(tmp_path: Path) -> None:
    backend = FakeMailArchiveBackend()
    subject = MailArchiveCommandBridge(backend, state_root=tmp_path / "state", clock=lambda: FIXED_TIME)

    backend.icloud["INBOX"].uidvalidity = 9999
    with pytest.raises(MailArchiveConflictError, match="UIDVALIDITY changed"):
        subject.execute(icloud_proposal(), operation_id=EXECUTE_ID)
    assert [name for name, _ in backend.calls].count("execute") == 1

    backend = FakeMailArchiveBackend()
    subject = MailArchiveCommandBridge(backend, state_root=tmp_path / "state2", clock=lambda: FIXED_TIME)
    backend.icloud["INBOX"].messages[101]["message_id"] = "<other@example.com>"
    with pytest.raises(MailArchiveConflictError, match="RFC Message-ID changed"):
        subject.execute(icloud_proposal(), operation_id=EXECUTE_ID)

    backend = FakeMailArchiveBackend()
    subject = MailArchiveCommandBridge(backend, state_root=tmp_path / "state3", clock=lambda: FIXED_TIME)
    backend.gmail["message_id"] = "<changed@example.com>"
    with pytest.raises(MailArchiveConflictError, match="gmail RFC Message-ID changed"):
        subject.execute(gmail_proposal(), operation_id=EXECUTE_ID)


def test_replay_does_not_mutate_a_second_time_and_state_is_private(tmp_path: Path) -> None:
    backend = FakeMailArchiveBackend()
    subject = MailArchiveCommandBridge(backend, state_root=tmp_path / "state", clock=lambda: FIXED_TIME)
    document = icloud_proposal()

    executed = subject.execute(document, operation_id=EXECUTE_ID)
    replayed = subject.execute(document, operation_id=EXECUTE_ID)

    assert replayed["details"]["replayed"] is True
    assert len([name for name, _ in backend.calls if name == "execute"]) == 1

    digest = executed["reference"].removeprefix("mail-archive-operation:")
    state_path = tmp_path / "state" / "operations" / f"{digest}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["execute_receipt"]["schema"] == RECEIPT_SCHEMA
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


def test_uncertain_partial_failure_is_visible_and_readbackable(tmp_path: Path) -> None:
    backend = FakeMailArchiveBackend()
    backend.partial_mode = "icloud-copy-only"
    subject = MailArchiveCommandBridge(backend, state_root=tmp_path / "state", clock=lambda: FIXED_TIME)

    executed = subject.execute(icloud_proposal(), operation_id=EXECUTE_ID)
    assert executed["details"]["status"] == "uncertain"
    assert executed["details"]["effect_verified"] is False

    with pytest.raises(MailArchiveVerificationError, match="origin is still present"):
        subject.readback(icloud_proposal(), reference=executed["reference"])


def test_probe_is_read_only_and_returns_capabilities(tmp_path: Path) -> None:
    backend = FakeMailArchiveBackend()
    subject = MailArchiveCommandBridge(backend, state_root=tmp_path / "state", clock=lambda: FIXED_TIME)

    result = subject.dispatch("probe", {"account": "gmail", "mailbox": "INBOX"})
    assert result["ok"] is True
    assert result["supports_gmail"] is True
    assert backend.probe_calls == [("gmail", "INBOX")]
    assert backend.calls == []


def test_closed_request_validation_rejects_shape_errors() -> None:
    with pytest.raises(MailArchiveError, match="mail archive request"):
        MailArchiveRequest.from_mapping({"account": "gmail"})


def test_fetch_parser_uses_uid_and_canonical_gmail_labels() -> None:
    parsed = _parse_fetch_message(
        [
            (
                b'7 (UID 202 X-GM-MSGID 555000111222333444 '
                b'X-GM-LABELS (\\Inbox \\All "Travel Plans") '
                b'BODY[HEADER.FIELDS (MESSAGE-ID)] {41}',
                b"Message-ID: <gmail-202@example.com>\r\n\r\n",
            ),
            b")",
        ]
    )
    assert parsed == {
        "uid": 202,
        "message_id": "<gmail-202@example.com>",
        "gmail_msgid": "555000111222333444",
        "labels": ["INBOX", "ALL", "Travel Plans"],
    }


def test_display_snippet_strips_html_before_bounding() -> None:
    snippet = _display_snippet(
        b"<html><body><p>Show up 15 minutes early.</p>"
        b"<script>discard me</script><p>123 Main Street</p></body></html>",
        b"Content-Type: text/html; charset=utf-8\r\n",
    )
    assert snippet == "Show up 15 minutes early. 123 Main Street"


def test_copyuid_parser_accepts_tagged_response_cache_shape() -> None:
    assert _parse_copyuid(["COPYUID", b"1 45397 9001"]) == (1, 9001)


if __name__ == "__main__":
    pytest.main([__file__])
