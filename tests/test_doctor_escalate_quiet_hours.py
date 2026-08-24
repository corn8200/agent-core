from __future__ import annotations

import sys
import types
import unittest
import urllib.parse
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import doctor_escalate  # noqa: E402


ET = ZoneInfo("America/New_York")


class DoctorEscalateQuietHoursTest(unittest.TestCase):
    def test_dst_safe_quiet_hour_boundaries(self) -> None:
        cases = (
            (datetime(2026, 7, 11, 21, 29, tzinfo=ET), False),
            (datetime(2026, 7, 11, 21, 30, tzinfo=ET), True),
            (datetime(2026, 7, 12, 6, 59, tzinfo=ET), True),
            (datetime(2026, 7, 12, 7, 0, tzinfo=ET), False),
            (datetime(2026, 1, 11, 21, 30, tzinfo=ET), True),
            (datetime(2026, 1, 12, 7, 0, tzinfo=ET), False),
        )
        for when, expected in cases:
            with self.subTest(when=when.isoformat()):
                self.assertEqual(
                    doctor_escalate._in_phone_quiet_hours(when), expected
                )

    def test_p1_is_held_before_voice_or_network(self) -> None:
        reroute = mock.Mock(return_value=True)
        voice_module = types.ModuleType("core.voice_reroute")
        voice_module.voice_reroute_send = reroute

        with mock.patch.dict(sys.modules, {"core.voice_reroute": voice_module}):
            with mock.patch.object(doctor_escalate.urllib.request, "urlopen") as urlopen:
                sent = doctor_escalate._pushover_direct(
                    "test", "body", priority=1,
                    now=datetime(2026, 7, 11, 22, 0, tzinfo=ET),
                )

        self.assertFalse(sent)
        reroute.assert_not_called()
        urlopen.assert_not_called()

    def test_explicit_p2_can_cross_quiet_hours(self) -> None:
        reroute = mock.Mock(return_value=False)
        voice_module = types.ModuleType("core.voice_reroute")
        voice_module.voice_reroute_send = reroute

        with mock.patch.dict(sys.modules, {"core.voice_reroute": voice_module}):
            with mock.patch.dict(
                "os.environ",
                {"PUSHOVER_APP_TOKEN": "token", "PUSHOVER_USER_KEY": "user"},
                clear=False,
            ):
                with mock.patch.object(
                    doctor_escalate.urllib.request, "urlopen", return_value=object()
                ) as urlopen:
                    sent = doctor_escalate._pushover_direct(
                        "test", "body", priority=2,
                        now=datetime(2026, 7, 11, 22, 0, tzinfo=ET),
                    )

        self.assertTrue(sent)
        reroute.assert_called_once()
        request = urlopen.call_args.args[0]
        payload = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(payload["priority"], ["2"])
        self.assertEqual(payload["retry"], ["60"])
        self.assertEqual(payload["expire"], ["3600"])

    def test_bypass_receipt_records_quiet_hold_not_delivery(self) -> None:
        events: list[dict] = []
        with mock.patch.object(doctor_escalate, "_record_bypass", return_value=1):
            with mock.patch.object(
                doctor_escalate, "_in_phone_quiet_hours", return_value=True
            ):
                with mock.patch.object(
                    doctor_escalate, "_pushover_direct", return_value=False
                ) as pushover:
                    with mock.patch.object(
                        doctor_escalate, "_log_event", side_effect=events.append
                    ):
                        doctor_escalate._deliver_bypass(
                            watcher="nightly-infra-check",
                            severity="critical",
                            summary="redis unavailable",
                            briefing="briefing",
                            reason="timeout",
                            redis_conn=None,
                            bypass_priority=None,
                            fingerprint="fp-night",
                            target_host="mac",
                        )

        pushover.assert_called_once()
        self.assertFalse(events[0]["pushover_sent"])
        self.assertTrue(events[0]["quiet_hours_held"])


if __name__ == "__main__":
    unittest.main()
