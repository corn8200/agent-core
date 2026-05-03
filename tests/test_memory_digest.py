import sqlite3
from datetime import datetime, timedelta

from scripts import memory_digest


def _memory_db(path):
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "CREATE TABLE memories (agent TEXT, category TEXT, content TEXT, timestamp TEXT)"
        )


def _insert_memory(path, *, agent, category, content, minutes_ago=0):
    ts = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "INSERT INTO memories (agent, category, content, timestamp) VALUES (?, ?, ?, ?)",
            (agent, category, content, ts),
        )


def test_handler_pattern_publishes_anomaly_item_without_push(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    state = tmp_path / "state.json"
    _memory_db(db)
    for i in range(3):
        _insert_memory(
            db,
            agent="handler",
            category="diagnosis",
            content=f"mailhub backlogqueue repeated outage sample {i}",
            minutes_ago=i,
        )

    events = []
    monkeypatch.setattr(
        memory_digest.cp,
        "event",
        lambda agent, kind, payload=None, **_kwargs: events.append((agent, kind, payload)) or 123,
    )

    assert memory_digest.main(["--db-path", str(db), "--state-path", str(state)]) == 0

    assert len(events) == 1
    agent, kind, payload = events[0]
    assert agent == "memory-digest"
    assert kind == "anomaly"
    assert payload["kind"] == "anomaly"
    assert payload["priority"] == 2
    assert payload["sources"] == ("ui",)
    assert "Handler anomaly" in payload["message"]
    assert state.exists()


def test_quiet_brief_pattern_updates_dedup_without_item(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    state = tmp_path / "state.json"
    _memory_db(db)
    for i in range(3):
        _insert_memory(
            db,
            agent="home_ops",
            category="brief",
            content=f"Schoolpickup topic repeats in morning brief {i}",
            minutes_ago=i,
        )

    def fail_event(*_args, **_kwargs):
        raise AssertionError("quiet pattern should not publish a Stack item")

    monkeypatch.setattr(memory_digest.cp, "event", fail_event)

    assert memory_digest.main(["--db-path", str(db), "--state-path", str(state)]) == 0

    assert state.exists()
