"""Structural checks for Claude SDK defaults.

These tests do not call Claude. They only assert that wrapper-created options
carry the coding-quality defaults the rest of the stack depends on.
"""

import inspect

from core.mac_sdk import _apply_defaults
from core.swarm_dispatch import dispatched_sdk_agent


def test_mac_sdk_default_options_are_coding_strong():
    options = _apply_defaults(None)

    assert options.model == "opus"
    assert options.max_turns == 20
    assert options.permission_mode == "bypassPermissions"
    assert options.thinking["type"] == "enabled"
    assert options.effort == "max"
    assert options.hooks
    assert options.mcp_servers


def test_swarm_sdk_dispatch_defaults_to_opus():
    signature = inspect.signature(dispatched_sdk_agent)

    assert signature.parameters["model"].default == "opus"
