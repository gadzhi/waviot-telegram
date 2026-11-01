import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from waviot_telegram.audit import collect_audit, write_audit
from waviot_telegram.config import Settings, load_settings
from waviot_telegram.waviot_client import WaviotClient


TREE = json.loads((Path(__file__).parent / "fixtures/waviot/discovery.json").read_text())
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def test_env_file_is_data_not_shell_and_process_env_takes_precedence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('WAVIOT_API_KEY="file-key"\nWAVIOT_HOUSE_ID=100\n')
    settings = load_settings(env_file, {"WAVIOT_API_KEY": "process-key"})
    assert settings.api_key == "process-key"
    assert settings.house_id == "100"
    assert "process-key" not in repr(settings)


def test_discovery_preserves_tree_and_flags_incomplete_house_scope(tmp_path):
    settings = Settings(api_key="secret-test-key", house_id="200", request_interval=0)
    scope = {"house_id": "200", "expected_node_ids": ["201", "301"]}
    with WaviotClient(settings.api_key, httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=TREE)))) as client:
        result = collect_audit(settings, client, expected_scope=scope, now=NOW)
    assert result["coverage"]["status"] == "missing"
    assert result["coverage"]["missing_node_ids"] == ["301"]
    assert result["live_alerts_ready"] is False
    output = tmp_path / "report"
    write_audit(result, output, secrets=(settings.api_key,))
    saved = json.loads((output / "evidence.json").read_text())
    assert saved["responses"]["tree"]["tree"]["100"]["id"] == 100
    assert "Resident name" not in (output / "evidence.json").read_text()
    assert "secret-test-key" not in (output / "report.md").read_text()


@pytest.mark.parametrize("channel", ["17", "cold_water_flow_water7", "electro_ac_p_lsum_tsum"])
def test_sampling_keeps_packet_time_separate_and_null_is_not_zero(channel):
    responses = {
        "/api.tree/get_tree/": TREE,
        "/api.data/get_full_element_info/": {"status": "ok", "devices": []},
        "/api.modem/info/": {"status": "ok", "modem": {"battery": 3.6}},
        "/api.modem/history/": {"status": "ok", "history": []},
        "/api.modem/get_last_message/": {"status": "ok", "packet": {"event_time": 1793448000, "received_time": "1793448100"}},
        "/api.data/get_modem_channel_values/": {"status": "ok", "channel": channel, "values": {"1793358000": "10.060", "1793361600": None}},
        "/api.data/get_events/": {"status": "ok", "events": []},
    }
    def handler(request):
        return httpx.Response(200, json=responses[request.url.path])

    settings = Settings(api_key="secret-test-key", house_id="100", request_interval=0)
    with WaviotClient(settings.api_key, httpx.Client(transport=httpx.MockTransport(handler))) as client:
        result = collect_audit(settings, client, element_ids=["201"], modem_id="00ABCD", channel=channel, now=NOW)
    assert result["channel_summary"]["usable_values"] == 1
    assert result["channel_summary"]["missing_values"] == 1
    assert result["channel_summary"]["last_measurement_timestamp"] == 1793358000
    assert result["packet_observation"]["event_time"] == 1793448000
    assert result["live_alerts_ready"] is False
    assert result["coverage"]["status"] == "unverified"


def test_failed_tree_stops_sampling_without_inventing_meter_incidents():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "error", "message": "secret-test-key"})

    settings = Settings(api_key="secret-test-key", request_interval=0)
    with WaviotClient(settings.api_key, httpx.Client(transport=httpx.MockTransport(handler))) as client:
        result = collect_audit(settings, client, modem_id="00ABCD", channel="17", now=NOW)
    assert calls == ["/api.tree/get_tree/"]
    assert result["errors"][0]["endpoint"] == "tree"
    assert "secret-test-key" not in json.dumps(result)
    assert result["channel_summary"] is None


def test_rate_limit_stops_audit_instead_of_retrying_other_endpoints():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "60"})

    settings = Settings(api_key="secret-test-key", request_interval=0)
    with WaviotClient(settings.api_key, httpx.Client(transport=httpx.MockTransport(handler))) as client:
        result = collect_audit(settings, client, element_ids=["201"], now=NOW)
    assert len(calls) == 1
    assert result["errors"][0]["code"] == "rate_limited"


def test_matching_expected_nodes_does_not_certify_complete_house():
    settings = Settings(api_key="key", house_id="100", request_interval=0)
    with WaviotClient(settings.api_key, httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=TREE)))) as client:
        result = collect_audit(settings, client, expected_scope={"house_id": "100", "expected_node_ids": ["201"]}, now=NOW)
    assert result["coverage"]["status"] == "expected_nodes_found"
    assert result["live_alerts_ready"] is False


def test_evidence_redacts_nested_credentials_and_personal_fields(tmp_path):
    output = tmp_path / "report"
    result = {"responses": {"nested": {"api_key": "other-key", "lastname": "Person", "echo": "url?key=actual-key", "values": {"1": None}}}, "live_alerts_ready": False}
    write_audit(result, output, secrets=("actual-key",))
    text = (output / "evidence.json").read_text()
    assert all(secret not in text for secret in ["other-key", "Person", "actual-key"])
    assert json.loads(text)["responses"]["nested"]["values"]["1"] is None


def test_output_is_private_and_never_overwrites_an_existing_report(tmp_path):
    import stat
    import pytest

    output = tmp_path / "report"
    write_audit({"live_alerts_ready": False}, output)
    original = (output / "evidence.json").read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "evidence.json").stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_audit({"replacement": True}, output)
    assert (output / "evidence.json").read_bytes() == original
