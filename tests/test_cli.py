import json

import httpx

from waviot_telegram.cli import main
from waviot_telegram.waviot_client import WaviotClient


def test_check_requires_key_but_does_not_attempt_network(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("WAVIOT_API_KEY", raising=False)
    assert main(["--env-file", str(tmp_path / "absent"), "check"]) == 2
    assert "missing" in capsys.readouterr().out


def test_discovery_command_saves_redacted_observations(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WAVIOT_API_KEY", "secret-test-key")
    monkeypatch.setenv("WAVIOT_REQUEST_INTERVAL_SECONDS", "0")
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "tree": {"10": {"id": 10, "name": "Private name", "type": 1}}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("waviot_telegram.cli.WaviotClient", lambda key, **kwargs: WaviotClient(key, http))
    output = tmp_path / "evidence"
    assert main(["--env-file", str(tmp_path / "absent"), "discover", "--output-dir", str(output)]) == 0
    assert calls == ["/api.tree/get_tree/"]
    evidence = (output / "evidence.json").read_text()
    assert "Private name" not in evidence
    assert "secret-test-key" not in evidence
    assert json.loads(evidence)["live_alerts_ready"] is False
    assert "secret-test-key" not in capsys.readouterr().out
    http.close()


def test_bad_local_config_is_reported_without_echoing_values(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("WAVIOT_REQUEST_INTERVAL_SECONDS=secret-test-key\n")
    assert main(["--env-file", str(env), "check"]) == 2
    assert "secret-test-key" not in capsys.readouterr().err
