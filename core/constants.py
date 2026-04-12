"""Shared constants across all agent-core modules."""

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
VPS_IP = "100.118.21.64"
PI_SSH_USER = "john"
PI_IP = "100.92.165.20"
MAC_IP = "100.122.35.56"

# --- Email ---
BUSINESS_EMAIL = "info@sentryaithermal.com"
PERSONAL_EMAIL = "corn82@icloud.com"
WIFE_EMAIL = "cornash89@gmail.com"
NOTIFY_EMAIL = "Cornelius Family <notify@jcornelius.net>"

# --- Pushover ---
PUSHOVER_USER = "ur9bv8fhxgtkfmwxvnho2v77wi4qhj"
PUSHOVER_TOKEN = "azyfddsu352o62r15jccgvdvp9ewtt"

# --- Databases ---
MESSAGE_BUS_DB = HOME / "logs" / "message_bus.db"
NUDGE_DB = HOME / "logs" / "nudge-state.db"

# --- Calendar ---
SKIP_CALENDARS = {"Siri Suggestions", "US Holidays", "Birthdays"}

# --- Misc ---
GATHER_TTL_SECONDS = 1800  # 30 min cache
WATCH_SECRET = "wc-corn82"
