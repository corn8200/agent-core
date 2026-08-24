from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from core import (
    agent_cp_client,
    doctor_escalate,
    message_router,
    message_vector,
    swarm_dispatch,
    vector,
    vps,
)
from core.retired_services import RetiredServiceError, is_retired_route


ROOT = Path(__file__).resolve().parents[1]
DELETED_VPS_HOST = ".".join(("100", "118", "21", "64"))
DELETED_VPS_URL = f"http://{DELETED_VPS_HOST}:8768"


class RetiredVpsStaticTest(unittest.TestCase):
    def test_deleted_tailnet_ip_is_not_embedded_in_executable_sources(self) -> None:
        deleted_ip = ".".join(("100", "118", "21", "64"))
        paths = [
            *ROOT.joinpath("core").rglob("*.py"),
            *ROOT.joinpath("core").rglob("*.sh"),
            *ROOT.joinpath("bin").rglob("*"),
        ]

        offenders = [
            str(path.relative_to(ROOT))
            for path in paths
            if path.is_file() and deleted_ip in path.read_text(errors="ignore")
        ]

        self.assertEqual(offenders, [])

    def test_no_executable_ssh_vps_transport_remains(self) -> None:
        transport_re = re.compile(
            r"(create_subprocess_exec|subprocess\.run)\([^)]*['\"]ssh['\"][^)]*['\"]vps['\"]",
            re.DOTALL,
        )
        offenders = []
        for path in ROOT.joinpath("core").rglob("*.py"):
            if path.name == "hooks.py":
                continue
            if transport_re.search(path.read_text(errors="ignore")):
                offenders.append(str(path.relative_to(ROOT)))

        self.assertEqual(offenders, [])


class RetiredVpsRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_vps_client_returns_retired_before_http(self) -> None:
        with mock.patch.object(vps, "BASE_URL", ""):
            with mock.patch.object(vps.httpx, "AsyncClient") as client:
                result = await vps.sandbox_run(["true"])

        client.assert_not_called()
        self.assertTrue(result["retired"])
        self.assertEqual(result["status_code"], 410)

    async def test_explicit_deleted_vps_url_is_retired_before_http(self) -> None:
        with mock.patch.object(vps, "BASE_URL", DELETED_VPS_URL):
            with mock.patch.object(vps.httpx, "AsyncClient") as client:
                result = await vps.sandbox_run(["true"])

        client.assert_not_called()
        self.assertTrue(result["retired"])

    async def test_message_router_email_context_does_not_ssh(self) -> None:
        with mock.patch.object(message_router.asyncio, "create_subprocess_exec") as create_proc:
            result = await message_router._get_email_context()

        create_proc.assert_not_called()
        self.assertIn("retired", result["error"])

    def test_agent_cp_client_does_not_post_without_replacement_url(self) -> None:
        with mock.patch.object(agent_cp_client, "CP_URL", ""):
            with mock.patch.object(agent_cp_client, "LOCAL_DB", None):
                with mock.patch.object(agent_cp_client.urllib.request, "urlopen") as urlopen:
                    event_id = agent_cp_client.event("test-agent", "start")
                    killed = agent_cp_client.is_killed("test-agent")

        urlopen.assert_not_called()
        self.assertIsNone(event_id)
        self.assertFalse(killed)

    def test_agent_cp_client_rejects_explicit_deleted_vps_url(self) -> None:
        with mock.patch.object(agent_cp_client, "CP_URL", DELETED_VPS_URL):
            with mock.patch.object(agent_cp_client, "LOCAL_DB", None):
                with mock.patch.object(agent_cp_client.urllib.request, "urlopen") as urlopen:
                    event_id = agent_cp_client.event("test-agent", "start")

        urlopen.assert_not_called()
        self.assertIsNone(event_id)

    def test_vector_db_fails_before_connect_without_replacement_dsn(self) -> None:
        with mock.patch.object(vector, "PG_DSN", ""):
            with mock.patch.object(vector.psycopg2, "connect") as connect:
                with self.assertRaises(RetiredServiceError):
                    vector.get_stats()

        connect.assert_not_called()

    def test_vector_and_message_queue_reject_explicit_deleted_vps_urls(self) -> None:
        dsn = f"postgresql://user:password@{DELETED_VPS_HOST}:5432/appdb"
        redis_url = f"redis://{DELETED_VPS_HOST}:6379/0"
        with mock.patch.object(vector, "PG_DSN", dsn):
            with mock.patch.object(vector.psycopg2, "connect") as connect:
                with self.assertRaises(RetiredServiceError):
                    vector.get_stats()
        with mock.patch.object(message_vector, "REDIS_URL", redis_url):
            with mock.patch.object(message_vector, "QUEUE_NAME", "vector"):
                with mock.patch.object(message_vector, "_queue", None):
                    queue = message_vector._get_queue()

        connect.assert_not_called()
        self.assertIsNone(queue)

    def test_retired_route_classifier_covers_aliases_and_old_control_plane(self) -> None:
        for value in (
            "vps",
            "claude-vps:3",
            f"ubuntu@{DELETED_VPS_HOST}:22",
            DELETED_VPS_URL,
            "https://cp.jcornelius.net/api",
        ):
            self.assertTrue(is_retired_route(value), value)
        for value in ("mac", "air", "home-pi", "http://127.0.0.1:8770"):
            self.assertFalse(is_retired_route(value), value)

    def test_doctor_vps_target_logs_retired_before_transport(self) -> None:
        events: list[dict] = []

        with mock.patch.object(
            doctor_escalate,
            "_get_redis",
            side_effect=AssertionError("retired VPS route must not touch Redis"),
        ):
            with mock.patch.object(
                doctor_escalate,
                "_pane_ask_binary",
                side_effect=AssertionError("retired VPS route must not inspect pane-ask"),
            ):
                with mock.patch.object(
                    doctor_escalate,
                    "_deliver_bypass",
                    side_effect=AssertionError("retired VPS route must not bypass"),
                ):
                    with mock.patch.object(doctor_escalate, "_log_event", side_effect=events.append):
                        result = doctor_escalate.doctor_escalate(
                            watcher="old-vps-route",
                            severity="warn",
                            summary="deleted server route",
                            target_host="vps",
                        )

        self.assertTrue(result["retired"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(events[0]["event"], "retired_route")

    def test_doctor_mac_target_is_local_only_after_voice_lane_retirement(self) -> None:
        events: list[dict] = []

        with mock.patch.object(
            doctor_escalate,
            "_get_redis",
            side_effect=AssertionError("retired doctor route must not touch Redis"),
        ):
            with mock.patch.object(
                doctor_escalate,
                "_pane_ask_binary",
                side_effect=AssertionError("retired doctor route must not inspect pane-ask"),
            ):
                with mock.patch.object(
                    doctor_escalate,
                    "_deliver_bypass",
                    side_effect=AssertionError("retired doctor route must not bypass"),
                ):
                    with mock.patch.object(doctor_escalate, "_log_event", side_effect=events.append):
                        result = doctor_escalate.doctor_escalate(
                            watcher="retired-doctor-route",
                            severity="warn",
                            summary="local doctor lane retired",
                            target_host="mac",
                        )

        self.assertTrue(result["retired"])
        self.assertTrue(result["local_only"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(events[0]["event"], "retired_route")

    def test_swarm_vps_pane_dispatch_fails_before_subprocess(self) -> None:
        with mock.patch.object(swarm_dispatch.subprocess, "run") as run:
            with mock.patch.object(swarm_dispatch, "doctor_escalate") as escalate:
                result = swarm_dispatch.dispatched_pane_ask(
                    "claude-vps:3",
                    "status",
                    ssh="vps",
                )

        run.assert_not_called()
        escalate.assert_not_called()
        self.assertTrue(result["retired"])
