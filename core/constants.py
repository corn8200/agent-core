"""Shared constants across all agent-core modules."""

import os
from pathlib import Path

# --- Paths ---
HOME = Path.home()
AGENT_CORE = HOME / "Projects" / "agent-core"
CLAUDE_AGENTS = HOME / ".claude" / "agents"
CLAUDE_CONFIG = HOME / "claude-config"
HANDOFF_DIR = Path("/tmp/handoff")
GATHER_CACHE = Path("/tmp/claude-gather.json")

# --- Network ---
VPS_SSH = "vps"
VPS_IP = ""
PI_SSH_USER = "john"
PI_IP = ""
MAC_IP = ""

# --- Email ---
BUSINESS_EMAIL = "info@sentryaithermal.com"
PERSONAL_EMAIL = ""
WIFE_EMAIL = ""
NOTIFY_EMAIL = " <>"

# --- Pushover ---
PUSHOVER_USER = os.environ.get("PUSHOVER_USER_KEY", "")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_APP_TOKEN", "")

# --- Databases ---
MESSAGE_BUS_DB = HOME / "logs" / "message_bus.db"
NUDGE_DB = HOME / "logs" / "nudge-state.db"

# --- Calendar ---
SKIP_CALENDARS = {"Siri Suggestions", "US Holidays", "Birthdays"}

# --- Misc ---
GATHER_TTL_SECONDS = 2700  # 45 min cache
WATCH_SECRET = os.environ.get("WATCH_SECRET", "")
