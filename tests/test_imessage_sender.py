import asyncio

from cockpit import imessage_sender


class FakeApi:
    def __init__(self, rows):
        self.rows = rows
        self.sent = []
        self.failed = []

    def claim_pending(self, *, limit):
        assert limit == 5
        return self.rows

    def mark_sent(self, row_id, *, sent_at=None):
        self.sent.append((row_id, sent_at))
        return {"id": row_id, "status": "sent"}

    def mark_failed(self, row_id, *, error_text):
        self.failed.append((row_id, error_text))
        return {"id": row_id, "status": "pending"}


def test_poll_once_sends_claimed_rows_and_marks_sent():
    api = FakeApi([{"id": 7, "thread_id": "+15555550100", "body": "hello"}])
    sends = []

    async def fake_send(thread_id, body, _approved=False):
        sends.append((thread_id, body, _approved))
        return True, "sent"

    stats = asyncio.run(imessage_sender.poll_once(api, send_fn=fake_send))

    assert stats == {"claimed": 1, "sent": 1, "failed": 0}
    assert sends == [("+15555550100", "hello", True)]
    assert api.sent == [(7, api.sent[0][1])]
    assert api.sent[0][1].endswith("Z")
    assert api.failed == []


def test_poll_once_marks_failed_when_send_fails():
    api = FakeApi([{"id": 8, "thread_id": "+15555550101", "body": "hello"}])

    async def fake_send(_thread_id, _body, _approved=False):
        return False, "delivery timeout"

    stats = asyncio.run(imessage_sender.poll_once(api, send_fn=fake_send))

    assert stats == {"claimed": 1, "sent": 0, "failed": 1}
    assert api.sent == []
    assert api.failed == [(8, "delivery timeout")]


def test_poll_once_does_not_send_when_queue_empty():
    api = FakeApi([])

    async def fake_send(*_args, **_kwargs):
        raise AssertionError("send should not be called for empty queue")

    stats = asyncio.run(imessage_sender.poll_once(api, send_fn=fake_send))

    assert stats == {"claimed": 0, "sent": 0, "failed": 0}
    assert api.sent == []
    assert api.failed == []
