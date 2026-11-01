import json

from waviot_telegram.collector import collect_inventory
from waviot_telegram.config import Settings
from waviot_telegram.storage import Store
from waviot_telegram.waviot_client import WaviotError


class Stop:
    def wait(self, seconds):
        return False


def test_midway_rate_limit_keeps_previous_whole_inventory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "audit-scope.json").write_text(json.dumps({"accepted_apartment_count": 2}))
    database = tmp_path / "state.db"
    old_inventory = [{"element_id": "1", "resources": ["Gas"]}, {"element_id": "2", "resources": ["Electricity"]}]
    with Store(database) as store:
        store.set("inventory", old_inventory)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, endpoint, **params):
            if endpoint == "tree":
                return {"tree": {"1": {"id": "1", "type": "apt", "appartment": "1"}, "2": {"id": "2", "type": "apt", "appartment": "2"}}}
            if params["id"] == "2":
                raise WaviotError("rate_limited", "Rate limited")
            return {"devices": {}}

    monkeypatch.setattr("waviot_telegram.collector.WaviotClient", Client)
    monkeypatch.setattr("waviot_telegram.collector.load_settings", lambda: Settings(api_key="synthetic", house_id="100"))
    collect_inventory(database, Stop())
    with Store(database) as store:
        assert store.get("inventory") == old_inventory
        assert store.get("scan_status")["state"] == "failed"
        assert store.get("scan_status")["error"] == "rate_limited"


def test_incomplete_tree_does_not_publish_partial_house(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "audit-scope.json").write_text(json.dumps({"accepted_apartment_count": 2}))
    database = tmp_path / "state.db"
    with Store(database) as store:
        store.set("inventory", [{"element_id": "accepted-house"}])

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, endpoint, **params):
            if endpoint != "tree":
                raise AssertionError("Incomplete scope must not issue element requests")
            return {"tree": {"1": {"id": "1", "type": "apt", "appartment": "1"}}}

    monkeypatch.setattr("waviot_telegram.collector.WaviotClient", Client)
    monkeypatch.setattr("waviot_telegram.collector.load_settings", lambda: Settings(api_key="synthetic", house_id="100"))
    collect_inventory(database, Stop())
    with Store(database) as store:
        assert store.get("inventory") == [{"element_id": "accepted-house"}]
        assert store.get("scan_status")["state"] == "failed"


def test_missing_confirmed_apartment_count_does_not_replace_inventory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'audit-scope.json').write_text('{}')
    database = tmp_path / 'state.db'
    with Store(database) as store:
        store.set('inventory', [{'element_id': 'previous'}])
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, endpoint, **params):
            assert endpoint == 'tree'
            return {'tree': {'1': {'id': '1', 'type': 'apt', 'appartment': '1'}}}
    monkeypatch.setattr('waviot_telegram.collector.WaviotClient', Client)
    monkeypatch.setattr('waviot_telegram.collector.load_settings', lambda: Settings(api_key='synthetic', house_id='100'))
    collect_inventory(database, Stop())
    with Store(database) as store:
        assert store.get('inventory') == [{'element_id': 'previous'}]
        assert store.get('scan_status')['state'] == 'failed'
        assert 'accepted_apartment_count' in store.get('scan_status')['error']
