"""tests for core.mail_imap — unit tests for parsers + one live integration probe.

The integration test hits the real iCloud IMAP. It is skipped if creds are
missing (CI without 1Password access).
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.mail_imap import (  # noqa: E402
    MailIMAP,
    MailIMAPError,
    _decode_mime,
    _format_imap_date,
    _parse_addr,
    _extract_snippet,
)


class TestDecodeMime:
    def test_plain(self):
        assert _decode_mime("Hello world") == "Hello world"

    def test_empty(self):
        assert _decode_mime("") == ""
        assert _decode_mime(None) == ""

    def test_rfc2047_utf8_b64(self):
        # =?UTF-8?B?SGVsbG8gV29ybGQ=?= → "Hello World"
        assert _decode_mime("=?UTF-8?B?SGVsbG8gV29ybGQ=?=") == "Hello World"

    def test_rfc2047_qp(self):
        # quoted-printable
        assert _decode_mime("=?utf-8?Q?Caf=C3=A9?=") == "Café"

    def test_mixed(self):
        # Multiple encoded segments + literal text
        assert _decode_mime("=?UTF-8?B?SGVsbG8=?= world") == "Hello world"


class TestParseAddr:
    def test_name_and_addr(self):
        assert _parse_addr("Ashley <ash@example.com>") == ("Ashley", "ash@example.com")

    def test_addr_only(self):
        assert _parse_addr("ash@example.com") == ("", "ash@example.com")

    def test_empty(self):
        assert _parse_addr("") == ("", "")

    def test_lowercases_addr(self):
        assert _parse_addr("Foo <FOO@BAR.COM>")[1] == "foo@bar.com"

    def test_decodes_name(self):
        assert _parse_addr("=?utf-8?Q?Caf=C3=A9?= <c@e.com>") == ("Café", "c@e.com")


class TestFormatImapDate:
    def test_format(self):
        d = dt.datetime(2026, 4, 25, tzinfo=dt.timezone.utc)
        assert _format_imap_date(d) == "25-Apr-2026"


class TestExtractSnippet:
    def test_collapses_whitespace(self):
        s = _extract_snippet(b"Hello   world\n\n\twith   tabs")
        assert s == "Hello world with tabs"

    def test_strips_qp_softbreaks(self):
        s = _extract_snippet(b"a longer line that is wrapped=\nat the soft break")
        assert s == "a longer line that is wrappedat the soft break"

    def test_truncates(self):
        s = _extract_snippet(b"x" * 1000)
        assert len(s) == 240


class TestMailIMAPInit:
    def test_unknown_account_raises(self):
        with pytest.raises(ValueError, match="unknown account"):
            MailIMAP("bogus")

    def test_known_accounts_construct(self):
        # Should not raise — connect is lazy
        for acct in ("icloud", "gmail-burner"):
            m = MailIMAP(acct)
            assert m.account == acct
            assert m._conn is None  # lazy


class TestMailIMAPLive:
    """Live integration test against real iCloud IMAP. Skipped if creds missing."""

    @pytest.fixture(autouse=True)
    def _check_creds(self):
        try:
            from core.vault import get_secret
        except ImportError:
            pytest.skip("core.vault not available")
        if not (get_secret("ICLOUD_EMAIL") and get_secret("ICLOUD_APP_PASSWORD")):
            pytest.skip("ICLOUD_EMAIL / ICLOUD_APP_PASSWORD not in vault")

    def test_recent_returns_dicts_with_required_keys(self):
        with MailIMAP("icloud") as imap:
            rows = imap.recent(limit=3, days=14)
        # Mailbox might genuinely be empty; allow [] but if non-empty must have shape
        assert isinstance(rows, list)
        for r in rows:
            assert set(r.keys()) >= {"msg_id", "subject", "from", "from_addr", "date", "snippet", "account", "rfc_message_id"}
            assert r["account"] == "icloud"

    def test_search_by_subject_returns_list(self):
        with MailIMAP("icloud") as imap:
            rows = imap.search_by_subject("the", days=14, limit=5)
        assert isinstance(rows, list)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
