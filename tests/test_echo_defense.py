"""Echo-defense regression tests for MessageReader.

Guards the attribution regex against typedstream-extractor prefix garbage
(seen 2026-04-18 #storm1, 2026-04-19 #storm2). When is_from_me=1 and the
text looks like our own outbound, we MUST drop it — otherwise turbo echoes
its own "[N] Standing by" replies back into the router and dispatches loop
forever.
"""

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "worktree_message_reader_echo", _HERE / "core" / "message_reader.py"
)
_mr = importlib.util.module_from_spec(_spec)
sys.modules["worktree_message_reader_echo"] = _mr
_spec.loader.exec_module(_mr)

_looks_like_bot_attribution = _mr._looks_like_bot_attribution


# Real echo patterns observed in imessage-bus.log 2026-04-19 06:10-08:45
ECHOES = [
    "[37] Standing by.",
    "[37] [commander idle] [no-ops queued] [cache:~2%]",
    "k[37] [commander idle] [no-ops queued] [cache:~2%]\n\nAcknowledged.",
    "P[38] [commander idle] [no pane activity] [cache:~4%]",
    "L[39] [commander idle] [no pane activity]",
    "![40] Standing by. No task queued.",
    "[41] Standing by.",
    "[Watch Commander] status ping",
    "[Agent] hello from the bus",
    "[Nudge] Tomorrow: 3 events",
]

# Real user messages — MUST NOT be flagged as echoes
USER_MSGS = [
    "hello",
    "can you check this?",
    "Give me a really good 1:59 minute deep dive on Iran",
    "check out [this link]",
    "Hi [John]",
    "",
    "the answer is 42",
    "running low on disk — can you clean up?",
]


def test_echo_patterns_caught():
    missed = [m for m in ECHOES if not _looks_like_bot_attribution(m)]
    assert not missed, f"echo regex missed: {missed}"


def test_user_messages_not_caught():
    false_positives = [m for m in USER_MSGS if _looks_like_bot_attribution(m)]
    assert not false_positives, f"false positives on user text: {false_positives}"


def test_letter_prefix_extractor_junk():
    # The 2026-04-19 storm: typedstream extractor pastes stray letters.
    for prefix in ("k", "P", "L", "!", ".", "?"):
        msg = f"{prefix}[42] Standing by."
        assert _looks_like_bot_attribution(msg), f"missed {msg!r}"


if __name__ == "__main__":
    test_echo_patterns_caught()
    test_user_messages_not_caught()
    test_letter_prefix_extractor_junk()
    print("All echo-defense tests passed.")
