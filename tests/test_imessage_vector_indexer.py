import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "worktree_imessage_vector_indexer", _HERE / "bin" / "imessage_vector_indexer.py"
)
_idx = importlib.util.module_from_spec(_spec)
sys.modules["worktree_imessage_vector_indexer"] = _idx
_spec.loader.exec_module(_idx)


def _record(*fields):
    return _idx._FIELD_SEP.join(str(f) for f in fields)


def test_parse_messages_logs_bad_record_and_continues():
    skipped = []

    def skip_writer(*args, **kwargs):
        skipped.append((args, kwargs))

    raw = _idx._RECORD_SEP.join([
        "bad-record",
        _record(1, "+15555550100", "John", 42, "guid-42", "hello there", "", 0, "2026-05-04 06:15:00", "+15555550100"),
    ])

    messages = _idx.parse_messages(raw, "run-1", skip_writer=skip_writer)

    assert len(messages) == 1
    assert messages[0].rowid == 42
    assert skipped
    assert skipped[0][0][1] == "malformed_record"


def test_bucket_source_ids_are_stable_for_idempotent_upsert():
    msg = _idx.ChatMessage(
        chat_id=1,
        chat_identifier="+15555550100",
        thread_name="John",
        rowid=42,
        guid="guid-42",
        text="hello there this is long enough to index",
        from_me=False,
        timestamp="2026-05-04 06:15:00",
        sender="+15555550100",
    )

    buckets = _idx._bucket_messages([msg, msg])
    key = next(iter(buckets))
    source_id = f"{key[0]}::{key[1]}"

    assert source_id == "+15555550100::2026-W19"
    assert len(buckets[key]) == 2


def test_week_start_expands_to_monday_for_catchup():
    dt = datetime(2026, 4, 17, 6, 18, tzinfo=timezone.utc)

    assert _idx._week_start(dt).isoformat() == "2026-04-13T00:00:00+00:00"


def test_excluded_threads_are_filtered():
    msg = _idx.ChatMessage(
        chat_id=1,
        chat_identifier="SENTINEL",
        thread_name="SENTINEL ops",
        rowid=1,
        guid="guid-1",
        text="should not index",
        from_me=False,
        timestamp="2026-05-04 06:15:00",
        sender="SENTINEL",
    )

    assert _idx._bucket_messages([msg]) == {}


def test_short_code_suffix_threads_are_filtered():
    msg = _idx.ChatMessage(
        chat_id=1,
        chat_identifier="32858(smsft)",
        thread_name="32858(smsft)",
        rowid=1,
        guid="guid-1",
        text="verification code",
        from_me=False,
        timestamp="2026-05-04 06:15:00",
        sender="32858",
    )

    assert _idx._bucket_messages([msg]) == {}
