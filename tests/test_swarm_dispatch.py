"""Tests for core.swarm_dispatch.

All subprocess and doctor_escalate calls are mocked. No real pane-ask-v2
invocations, no real doctor pane traffic.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, call, patch


class _SubprocResult:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


ESCALATE_PATH = "core.swarm_dispatch.doctor_escalate"


class TestDispatchedPaneAskSuccess(unittest.TestCase):
    """rc=0 on first attempt — no escalation, single subprocess call."""

    def test_pane_ask_success_first_try(self):
        result_obj = _SubprocResult(0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=result_obj) as mock_run, \
             patch(ESCALATE_PATH) as mock_esc:
            from core.swarm_dispatch import dispatched_pane_ask
            result = dispatched_pane_ask("claude:2", "hello")

        self.assertTrue(result["ok"])
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["stdout"], "ok")
        mock_run.assert_called_once()
        mock_esc.assert_not_called()


class TestDispatchedPaneAskBusyThenSuccess(unittest.TestCase):
    """rc=1 (busy) then rc=0 — two attempts, no escalation."""

    def test_pane_ask_busy_then_success(self):
        busy = _SubprocResult(1, stderr="busy")
        ok = _SubprocResult(0, stdout="done", stderr="")
        side_effects = [busy, ok]

        with patch("subprocess.run", side_effect=side_effects) as mock_run, \
             patch(ESCALATE_PATH) as mock_esc, \
             patch("time.sleep"):
            from core.swarm_dispatch import dispatched_pane_ask
            result = dispatched_pane_ask("claude:2", "hello", retries=2)

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(mock_run.call_count, 2)
        mock_esc.assert_not_called()


class TestDispatchedPaneAskPersistentBusyEscalates(unittest.TestCase):
    """rc=1 every attempt — exhausts retries, escalates exactly once."""

    def test_pane_ask_persistent_busy_escalates(self):
        busy = _SubprocResult(1, stderr="target busy")

        with patch("subprocess.run", return_value=busy), \
             patch(ESCALATE_PATH) as mock_esc, \
             patch("time.sleep"):
            from core.swarm_dispatch import dispatched_pane_ask
            result = dispatched_pane_ask("claude:3", "hi", retries=2)

        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 3)  # 1 initial + 2 retries
        self.assertTrue(result.get("escalated"))
        mock_esc.assert_called_once()
        _, kwargs = mock_esc.call_args
        self.assertEqual(kwargs["severity"], "warn")
        self.assertIn("claude:3", kwargs["summary"])


class TestDispatchedPaneAskTerminalFailureNoRetry(unittest.TestCase):
    """rc=3 (need-ssh) — terminal, no retry, immediate escalation."""

    def test_pane_ask_terminal_failure_escalates_immediately(self):
        terminal = _SubprocResult(3, stderr="session not present locally")

        with patch("subprocess.run", return_value=terminal) as mock_run, \
             patch(ESCALATE_PATH) as mock_esc, \
             patch("time.sleep"):
            from core.swarm_dispatch import dispatched_pane_ask
            result = dispatched_pane_ask("claude-vps:2", "task", retries=3)

        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 1)  # no retry on terminal code
        self.assertEqual(mock_run.call_count, 1)
        mock_esc.assert_called_once()


class TestDispatchedPaneAskSshFlag(unittest.TestCase):
    """--ssh arg is forwarded to pane-ask-v2 command."""

    def test_ssh_flag_forwarded(self):
        ok = _SubprocResult(0)
        with patch("subprocess.run", return_value=ok) as mock_run, \
             patch(ESCALATE_PATH):
            from core.swarm_dispatch import dispatched_pane_ask
            dispatched_pane_ask("claude-vps:3", "task", ssh="vps")

        cmd = mock_run.call_args[0][0]
        self.assertIn("--ssh", cmd)
        self.assertIn("vps", cmd)


class TestDispatchedSdkAgentSkippedWhenUnavailable(unittest.TestCase):
    """Missing claude_agent_sdk returns skipped sentinel without escalating."""

    def test_sdk_skipped_when_unavailable(self):
        # Temporarily remove the module from sys.modules so the import fails.
        saved = sys.modules.pop("claude_agent_sdk", None)
        try:
            with patch(ESCALATE_PATH) as mock_esc:
                from core.swarm_dispatch import dispatched_sdk_agent
                result = dispatched_sdk_agent(
                    system_prompt="You are a helper.",
                    user_prompt="Hello",
                )
        finally:
            if saved is not None:
                sys.modules["claude_agent_sdk"] = saved
            # Also evict any cached import inside swarm_dispatch module
            import importlib
            import core.swarm_dispatch as sd
            importlib.reload(sd)

        self.assertFalse(result["ok"])
        self.assertTrue(result.get("skipped"))
        self.assertIn("not installed", result.get("reason", ""))
        mock_esc.assert_not_called()


class TestEscalateSwarmFailurePassthrough(unittest.TestCase):
    """escalate_swarm_failure calls doctor_escalate with swarm-prefixed scope."""

    def test_escalate_passthrough(self):
        with patch(ESCALATE_PATH, return_value={"dispatched": True}) as mock_esc:
            from core.swarm_dispatch import escalate_swarm_failure
            ok = escalate_swarm_failure(
                watcher_name="test-watcher",
                summary="scout returned partial 3x",
                context={"attempts": 3},
                severity="warn",
            )

        self.assertTrue(ok)
        mock_esc.assert_called_once()
        _, kwargs = mock_esc.call_args
        self.assertEqual(kwargs["watcher"], "test-watcher")
        self.assertEqual(kwargs["severity"], "warn")
        self.assertEqual(kwargs["summary"], "scout returned partial 3x")
        self.assertEqual(kwargs["dedup_scope"], "swarm-test-watcher")

    def test_escalate_custom_dedup_scope(self):
        with patch(ESCALATE_PATH, return_value={"dispatched": False}) as mock_esc:
            from core.swarm_dispatch import escalate_swarm_failure
            ok = escalate_swarm_failure(
                watcher_name="foo",
                summary="bar",
                dedup_scope="custom-scope",
            )

        self.assertFalse(ok)
        _, kwargs = mock_esc.call_args
        self.assertEqual(kwargs["dedup_scope"], "custom-scope")

    def test_escalate_default_severity_warn(self):
        with patch(ESCALATE_PATH, return_value={"dispatched": True}) as mock_esc:
            from core.swarm_dispatch import escalate_swarm_failure
            escalate_swarm_failure(watcher_name="w", summary="s")

        _, kwargs = mock_esc.call_args
        self.assertEqual(kwargs["severity"], "warn")


class TestDispatchedPaneAskExceptionEscalatesCritical(unittest.TestCase):
    """Unexpected exception during subprocess call escalates with severity=critical."""

    def test_exception_escalates_critical(self):
        with patch("subprocess.run", side_effect=OSError("no such file")), \
             patch(ESCALATE_PATH) as mock_esc:
            from core.swarm_dispatch import dispatched_pane_ask
            result = dispatched_pane_ask("claude:2", "hi")

        self.assertFalse(result["ok"])
        self.assertTrue(result.get("escalated"))
        _, kwargs = mock_esc.call_args
        self.assertEqual(kwargs["severity"], "critical")


class TestDispatchedPaneAskWatcherNaming(unittest.TestCase):
    """watcher_name kwarg overrides default watcher in escalation."""

    def test_custom_watcher_name_used(self):
        terminal = _SubprocResult(5, stderr="submit failed")
        with patch("subprocess.run", return_value=terminal), \
             patch(ESCALATE_PATH) as mock_esc, \
             patch("time.sleep"):
            from core.swarm_dispatch import dispatched_pane_ask
            dispatched_pane_ask("claude:4", "task", watcher_name="my-custom-watcher")

        _, kwargs = mock_esc.call_args
        self.assertEqual(kwargs["watcher"], "my-custom-watcher")

    def test_default_watcher_name_includes_target(self):
        terminal = _SubprocResult(5, stderr="")
        with patch("subprocess.run", return_value=terminal), \
             patch(ESCALATE_PATH) as mock_esc, \
             patch("time.sleep"):
            from core.swarm_dispatch import dispatched_pane_ask
            dispatched_pane_ask("claude-vps:6", "task")

        _, kwargs = mock_esc.call_args
        self.assertIn("claude-vps:6", kwargs["watcher"])


if __name__ == "__main__":
    unittest.main()
