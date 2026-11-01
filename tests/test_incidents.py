from datetime import datetime, timezone

from waviot_telegram.storage import Store


NOW = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)


def test_investigation_is_durable_and_stale_action_cannot_change_it(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        first = store.open_incident("gas-1", "Корпус 1, кв. 46", "no_data", "Нет свежих данных", now=NOW)
        duplicate = store.open_incident("gas-1", "Корпус 1, кв. 46", "no_data", "Нет свежих данных", now=NOW)
        assert first["id"] == duplicate["id"]
        claimed, changed = store.transition(first["id"], 1, "claim", 10, "Иван", now=NOW)
        assert changed and claimed["assignee"] == "Иван"
        stale, changed = store.transition(first["id"], 1, "claim", 20, "Анна", now=NOW)
        assert not changed and stale["assignee"] == "Иван"
        fault, _ = store.transition(first["id"], 2, "fault", 10, "Иван", "Батарея проверена сотрудником", now=NOW)
        assert fault["state"] == "open" and fault["diagnosis"] == "fault"
        store.mark_recovered("gas-1", now=NOW)
    with Store(path) as store:
        incident = store.incident(first["id"])
        assert incident["recovered"] and incident["state"] == "open"
        closed, _ = store.transition(incident["id"], incident["version"], "repaired", 20, "Анна", "Заменена батарея", now=NOW)
        assert closed["state"] == "closed"
        assert len(store.history(first["id"])) == 5
        assert store.open_incident("gas-1", "Квартира 46", "no_data", "Повтор того же эпизода", now=NOW)["id"] == first["id"]
        store.reset_episode("gas-1")
        assert store.open_incident("gas-1", "Квартира 46", "no_data", "Новый эпизод", now=NOW)["id"] != first["id"]
