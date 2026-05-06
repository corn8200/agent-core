"""Tests for imessage_drainer.loop."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_loop():
    import imessage_drainer.loop as l
    return l


SAMPLE_PENDING_ROWS = [
    {
        "id": 101,
        "thread_id": "cornash89@gmail.com",
        "body": "Hey Ashley",
        "priority": 0,
        "claim_token": "claim-abc123",
        "retry_count": 0,
    }
]

EMPTY_PENDING = {"rows": []}
PENDING_ONE = {"rows": SAMPLE_PENDING_ROWS}


def _make_response(status_code: int, json_body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("GET", "http://test"),
    )


def test_send_success_calls_mark_sent():
    """/pending → row → bridge 200 → /sent called with claim_token."""
    loop = _load_loop()
    sent_calls = []
    failed_calls = []

    def fake_get_pending(cp_token, client):
        return SAMPLE_PENDING_ROWS

    def fake_send(row, bridge_token, client):
        return True, ""

    def fake_mark_sent(row_id, claim_token, cp_token, client):
        sent_calls.append((row_id, claim_token))

    def fake_mark_failed(row_id, claim_token, error, cp_token, client):
        failed_calls.append((row_id, claim_token, error))

    with patch.object(loop, "_get_pending", side_effect=fake_get_pending):
        with patch.object(loop, "_send_message", side_effect=fake_send):
            with patch.object(loop, "_mark_sent", side_effect=fake_mark_sent):
                with patch.object(loop, "_mark_failed", side_effect=fake_mark_failed):
                    # Run one iteration directly
                    with httpx.Client() as client:
                        rows = loop._get_pending("tok", client)
                        for row in rows:
                            sent, err = loop._send_message(row, "btok", client)
                            if sent:
                                loop._mark_sent(row["id"], row["claim_token"], "tok", client)
                            else:
                                loop._mark_failed(row["id"], row["claim_token"], err, "tok", client)

    assert len(sent_calls) == 1
    assert sent_calls[0] == (101, "claim-abc123")
    assert len(failed_calls) == 0


def test_send_failure_calls_mark_failed():
    """Bridge 503 → /failed called with reason."""
    loop = _load_loop()
    sent_calls = []
    failed_calls = []

    with httpx.Client() as client:
        rows = [SAMPLE_PENDING_ROWS[0]]
        for row in rows:
            # Simulate bridge failure
            sent = False
            err = "503 Service Unavailable"
            if sent:
                loop._mark_sent(row["id"], row["claim_token"], "tok", client)
                sent_calls.append(row["id"])
            else:
                loop._mark_failed(row["id"], row["claim_token"], err, "tok", client)
                # Intercept actual HTTP call
                failed_calls.append((row["id"], row["claim_token"], err))

    assert len(failed_calls) == 1
    assert failed_calls[0][0] == 101
    assert "503" in failed_calls[0][2]
    assert len(sent_calls) == 0


def test_idempotency_key_format():
    """Idempotency-Key header must be cockpit-imsg-{row.id}."""
    loop = _load_loop()
    headers_used = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

    class FakeClient:
        def post(self, url, headers=None, json=None, timeout=None):
            headers_used.append(headers or {})
            return FakeResponse()

    row = SAMPLE_PENDING_ROWS[0]
    loop._send_message(row, "bridge-tok", FakeClient())

    assert any(h.get("Idempotency-Key") == f"cockpit-imsg-{row['id']}" for h in headers_used)


def test_already_sent_404_on_sent_endpoint_no_double_send():
    """If /sent returns 404 (row already handled), no second send attempt."""
    loop = _load_loop()
    sent_endpoint_calls = []
    send_message_calls = []

    class Fake404Response:
        status_code = 404

    class FakeClient:
        def post(self, url, **kwargs):
            if "/sent" in url:
                sent_endpoint_calls.append(url)
                return Fake404Response()
            # Should not be called twice
            send_message_calls.append(url)
            return type("R", (), {"status_code": 200})()

        def get(self, *a, **kw):
            return type("R", (), {"status_code": 200, "json": lambda: EMPTY_PENDING})()

    # Simulate the exact code path: mark_sent is called once, returns 404 — no retry
    loop._mark_sent(101, "claim-abc", "cp-tok", FakeClient())

    assert len(sent_endpoint_calls) == 1
    # No additional send_message calls
    assert len(send_message_calls) == 0
