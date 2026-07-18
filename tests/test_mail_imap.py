from __future__ import annotations

import base64
import unittest

from core.mail_imap import MailIMAP, _extract_snippet


class MailIMAPSnippetTests(unittest.TestCase):
    def test_extract_snippet_decodes_base64_mime_part(self) -> None:
        body = base64.b64encode("Confirm tomorrow at 9 — bring ID.".encode("utf-8"))
        headers = b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n"
        self.assertEqual(
            _extract_snippet(body, headers),
            "Confirm tomorrow at 9 — bring ID.",
        )

    def test_parse_fetch_associates_continuation_tuples_with_message(self) -> None:
        raw = [
            (
                b"42 (BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] {139}",
                b"From: Clinic <clinic@example.com>\r\n"
                b"Subject: Appointment reminder\r\n"
                b"Date: Fri, 17 Jul 2026 20:00:00 -0400\r\n"
                b"Message-ID: <stable@example.com>\r\n\r\n",
            ),
            (
                b" BODY[1.MIME] {87}",
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"Content-Transfer-Encoding: quoted-printable\r\n\r\n",
            ),
            (b" BODY[1]<0> {31}", b"Please confirm=20your appointment."),
            b")",
        ]
        rows = MailIMAP("icloud")._parse_fetch(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["msg_id"], "42")
        self.assertEqual(rows[0]["rfc_message_id"], "<stable@example.com>")
        self.assertEqual(rows[0]["snippet"], "Please confirm your appointment.")


if __name__ == "__main__":
    unittest.main()
