from core import claude_usage_guard


def test_hit_wall_alone_does_not_block(monkeypatch):
    monkeypatch.setattr(
        claude_usage_guard,
        "read_active",
        lambda: {
            "icloud-20x": {
                "read_ok": True,
                "five_hour_pct": 0.02,
                "five_hour_status": "allowed",
                "seven_day_pct": 0.17,
                "seven_day_status": "allowed",
                "primary_pct": 0.17,
                "hit_wall": True,
                "http_code": 200,
            }
        },
    )

    assert claude_usage_guard.claude_usage_block_reason("test", threshold=0.80) is None


def test_rejected_window_blocks(monkeypatch):
    monkeypatch.setattr(
        claude_usage_guard,
        "read_active",
        lambda: {
            "icloud-20x": {
                "read_ok": True,
                "five_hour_pct": 0.02,
                "five_hour_status": "allowed",
                "seven_day_pct": 0.17,
                "seven_day_status": "rejected",
                "primary_pct": 0.17,
                "hit_wall": False,
                "http_code": 200,
            }
        },
    )

    reason = claude_usage_guard.claude_usage_block_reason("test", threshold=0.80)
    assert reason is not None
    assert "usage wall" in reason


def test_threshold_still_blocks(monkeypatch):
    monkeypatch.setattr(
        claude_usage_guard,
        "read_active",
        lambda: {
            "gmail-20x": {
                "read_ok": True,
                "five_hour_pct": 0.02,
                "five_hour_status": "allowed",
                "seven_day_pct": 0.91,
                "seven_day_status": "allowed_warning",
                "primary_pct": 0.91,
                "hit_wall": False,
                "http_code": 200,
            }
        },
    )

    reason = claude_usage_guard.claude_usage_block_reason("test", threshold=0.80)
    assert reason is not None
    assert "above Claude automation gate" in reason
