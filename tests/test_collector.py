from datetime import datetime, timezone

from waviot_telegram.collector import inventory_row, stale_candidates


def test_missing_values_stay_unknown_and_old_metadata_is_only_a_candidate():
    row = inventory_row({"element_id": "1", "corpus": "Корпус 1", "apartment": "46"}, {"devices": {
        "2": {"id": 2, "modem_id": "abc", "class_name": "Gas", "registrators": {"gas": {"channel_id": "1053", "last_value_timestamp": 1736424782}}},
        "3": {"id": 3, "modem_id": "def", "class_name": "Gas", "registrators": {"gas": {"channel_id": "1053", "last_value_timestamp": None}}}
    }}, "2026-10-31T10:00:00+00:00")
    candidates = stale_candidates([row], datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert len(candidates) == 1 and candidates[0]["gas_id"] == "2"
    assert "Причина не установлена" in candidates[0]["evidence"]
    assert row["gas"][1]["last_timestamp"] is None
