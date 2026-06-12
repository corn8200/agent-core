from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


CORE_DIR = Path(__file__).resolve().parents[1] / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import doctor_escalate  # noqa: E402


class FakeLatchRedis:
    """Minimal stateful fake: tracks setex/delete on the dedup latch."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int]] = {}  # key -> (value, ttl)

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def ttl(self, key: str) -> int:
        return self.store[key][1] if key in self.store else -2

    def setex(self, key: str, ttl: int, value) -> bool:
        self.store[key] = (value, int(ttl))
        return True

    def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


class DoctorEscalateBypassLatchTest(unittest.TestCase):
    def test_successful_dispatch_requires_receiver_ack(self) -> None:
        successful_run = types.SimpleNamespace(returncode=0, stderr="", stdout="")

        with mock.patch.dict("os.environ", {"TMUX_PANE": ""}, clear=False):
            with mock.patch.object(doctor_escalate, "_get_redis", return_value=None):
                with mock.patch.object(doctor_escalate, "_token_bucket_check", return_value=True):
                    with mock.patch.object(
                        doctor_escalate, "_check_cluster", return_value=("individual", [])
                    ):
                        with mock.patch.object(
                            doctor_escalate, "_pane_ask_binary", return_value="/fake/pane-ask-v2"
                        ):
                            with mock.patch.object(
                                doctor_escalate.subprocess, "run", return_value=successful_run
                            ) as run:
                                with mock.patch.object(doctor_escalate, "_log_event"):
                                    result = doctor_escalate.doctor_escalate(
                                        watcher="ack-contract",
                                        severity="critical",
                                        summary="must not mark unverified banners dispatched",
                                        dedup_scope="ack-contract",
                                    )

        self.assertTrue(result["dispatched"])
        argv = run.call_args.args[0]
        self.assertIn("--require-ack", argv)
        self.assertLess(argv.index("--require-ack"), argv.index("--auto-recover-wedge"))

    def test_bypass_rearms_dedup_latch_with_short_ttl_instead_of_deleting(self) -> None:
        fake_r = FakeLatchRedis()
        failed_run = types.SimpleNamespace(returncode=1, stderr="pane busy", stdout="")

        with mock.patch.object(doctor_escalate, "_get_redis", return_value=fake_r):
            with mock.patch.object(doctor_escalate, "_token_bucket_check", return_value=True):
                with mock.patch.object(
                    doctor_escalate, "_check_cluster", return_value=("individual", [])
                ):
                    with mock.patch.object(
                        doctor_escalate, "_pane_ask_binary", return_value="/fake/pane-ask-v2"
                    ):
                        with mock.patch.object(
                            doctor_escalate.subprocess, "run", return_value=failed_run
                        ):
                            with mock.patch.object(doctor_escalate.time, "sleep"):
                                with mock.patch.object(
                                    doctor_escalate, "_deliver_bypass"
                                ) as deliver:
                                    with mock.patch.object(doctor_escalate, "_log_event"):
                                        result = doctor_escalate.doctor_escalate(
                                            watcher="latch-test-watcher",
                                            severity="warn",
                                            summary="pane route held - bypass storm repro",
                                            dedup_scope="latch-test:bypass",
                                        )

        self.assertTrue(result["bypassed"])
        deliver.assert_called_once()

        latch_key = f"doctor:escalation:{result['fingerprint']}"
        self.assertIn(latch_key, fake_r.store, "bypass must NOT delete the dedup latch")
        value, ttl = fake_r.store[latch_key]
        self.assertLessEqual(ttl, 1800)
        self.assertGreater(ttl, 0)
        self.assertEqual(
            json.loads(value),
            {
                "watcher": "latch-test-watcher",
                "severity": "warn",
                "summary": "pane route held - bypass storm repro",
            },
        )


if __name__ == "__main__":
    unittest.main()
