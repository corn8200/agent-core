"""Tests for core.mac_capabilities — choose_tool and inventory helpers."""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from core.mac_capabilities import (
    CapabilityChoice,
    choose_tool,
    inventory,
    list_intents,
)


class TestChooseTool:
    def test_send_imessage(self):
        c = choose_tool("send-imessage")
        assert isinstance(c, CapabilityChoice)
        assert c.tool in {"send_imessage_reliable", "imsg"}
        assert c.intent == "send-imessage"

    def test_send_email(self):
        c = choose_tool("send-email")
        assert c.module == "core.mailhub"
        assert c.channel == "python"

    def test_schedule_event(self):
        c = choose_tool("schedule-event")
        assert "calendar" in c.module.lower() or "osascript" in c.channel

    def test_set_reminder(self):
        c = choose_tool("set-reminder")
        assert c.intent == "set-reminder"

    def test_look_up_contact(self):
        c = choose_tool("look-up-contact")
        assert c.channel == "mcp"

    def test_transcribe_voice_memo(self):
        c = choose_tool("transcribe-voice-memo")
        assert "voice" in c.module.lower() or "whisper" in (c.fallback or "")

    def test_query_mail(self):
        c = choose_tool("query-mail")
        assert c.intent == "query-mail"

    def test_unknown_intent_raises(self):
        with pytest.raises(KeyError):
            choose_tool("fly-to-moon")

    def test_case_normalized(self):
        c1 = choose_tool("send-imessage")
        c2 = choose_tool("Send-iMessage")
        assert c1 == c2

    def test_all_intents_return_choice(self):
        for intent in list_intents():
            c = choose_tool(intent)
            assert c.tool
            assert c.module
            assert c.channel

    def test_choice_is_frozen(self):
        c = choose_tool("send-email")
        with pytest.raises((AttributeError, TypeError)):
            c.tool = "something_else"  # type: ignore[misc]


class TestListIntents:
    def test_returns_list(self):
        intents = list_intents()
        assert isinstance(intents, list)
        assert len(intents) >= 7

    def test_required_intents_present(self):
        intents = set(list_intents())
        required = {
            "send-imessage", "send-email", "schedule-event",
            "set-reminder", "look-up-contact",
            "transcribe-voice-memo", "query-mail",
        }
        assert required.issubset(intents), f"Missing: {required - intents}"


class TestInventory:
    def test_inventory_raises_on_non_mac(self):
        if sys.platform == "darwin":
            pytest.skip("Running on Mac — skip non-Mac guard test")
        with pytest.raises(RuntimeError, match="Mac-only"):
            inventory()

    @pytest.mark.skipif(sys.platform != "darwin", reason="Mac only")
    def test_dry_run_returns_dict(self):
        result = inventory(dry_run=True)
        assert isinstance(result, dict)

    @pytest.mark.skipif(sys.platform != "darwin", reason="Mac only")
    def test_dry_run_structure(self):
        result = inventory(dry_run=True)
        assert "installed_apps" in result
        assert "mcp_servers" in result
        assert "apple_data_sources" in result
        assert "launchagents" in result
        assert "osascript_helpers" in result
        assert "tool_routing" in result

    @pytest.mark.skipif(sys.platform != "darwin", reason="Mac only")
    def test_dry_run_apple_data_sources_count(self):
        result = inventory(dry_run=True)
        assert len(result["apple_data_sources"]) >= 6

    @pytest.mark.skipif(sys.platform != "darwin", reason="Mac only")
    def test_dry_run_mcp_servers(self):
        result = inventory(dry_run=True)
        # At minimum the ~/.claude/.mcp.json servers
        assert len(result["mcp_servers"]) >= 4

    @pytest.mark.skipif(sys.platform != "darwin", reason="Mac only")
    def test_dry_run_launchagents(self):
        result = inventory(dry_run=True)
        assert len(result["launchagents"]) >= 8

    @pytest.mark.skipif(sys.platform != "darwin", reason="Mac only")
    def test_dry_run_installed_apps(self):
        result = inventory(dry_run=True)
        assert len(result["installed_apps"]) >= 5

    @pytest.mark.skipif(sys.platform != "darwin", reason="Mac only")
    def test_dry_run_is_json_serializable(self):
        result = inventory(dry_run=True)
        serialized = json.dumps(result)
        assert serialized  # not empty

    @pytest.mark.skipif(sys.platform != "darwin", reason="Mac only")
    def test_tool_routing_covers_all_intents(self):
        result = inventory(dry_run=True)
        routing_keys = set(result["tool_routing"])
        all_intents = set(list_intents())
        assert routing_keys == all_intents
