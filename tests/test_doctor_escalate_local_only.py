from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import doctor_escalate  # noqa: E402


class DoctorEscalateLocalOnlyTest(unittest.TestCase):
    def test_local_only_logs_without_touching_external_routes(self) -> None:
        events: list[dict] = []

        with mock.patch.dict(os.environ, {"DOCTOR_ESCALATE_LOCAL_ONLY": "1"}, clear=False):
            with mock.patch.object(
                doctor_escalate,
                "_get_redis",
                side_effect=AssertionError("local-only mode must not touch Redis"),
            ):
                with mock.patch.object(
                    doctor_escalate,
                    "_pane_ask_binary",
                    side_effect=AssertionError("local-only mode must not inspect pane-ask"),
                ):
                    with mock.patch.object(
                        doctor_escalate,
                        "_deliver_bypass",
                        side_effect=AssertionError("local-only mode must not send Pushover"),
                    ):
                        with mock.patch.object(doctor_escalate, "_log_event", side_effect=events.append):
                            result = doctor_escalate.doctor_escalate(
                                watcher="stale-creds-watcher",
                                severity="warn",
                                summary="ACCOUNT DRIFT: synthetic marker",
                                context={"marker": "codex-redis-auth-test-local-only"},
                                dedup_scope="account-drift:synthetic",
                            )

        self.assertTrue(result["local_only"])
        self.assertFalse(result["dispatched"])
        self.assertFalse(result["bypassed"])
        self.assertFalse(result["dedup_hit"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "local_only")
        self.assertEqual(events[0]["watcher"], "stale-creds-watcher")
        self.assertIn("fingerprint", events[0])
        self.assertEqual(events[0]["context"]["marker"], "codex-redis-auth-test-local-only")


if __name__ == "__main__":
    unittest.main()
