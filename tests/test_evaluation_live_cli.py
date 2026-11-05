import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation import __main__, runner
from evaluation.dataset import TaskSpec
from lilbot.config import LilBotConfig


@pytest.mark.parametrize("endpoint", ["http://example.test/localhost", "https://key@example.test/v1", "https://example.test/v1?key=x"])
def test_live_config_rejects_unsafe_endpoint(tmp_path, monkeypatch, endpoint):
    config = LilBotConfig(workspace=tmp_path, model="configured", api_key="test-only", base_url=endpoint)
    monkeypatch.setattr(__main__, "load_config", lambda _: config)
    with pytest.raises(ValueError, match="live evaluation requires"):
        __main__._load_live_config()


def test_live_config_requires_real_provider(tmp_path, monkeypatch):
    config = LilBotConfig(workspace=tmp_path, provider="mock", model="configured", api_key="test-only")
    monkeypatch.setattr(__main__, "load_config", lambda _: config)
    with pytest.raises(ValueError, match="real HTTP provider"):
        __main__._load_live_config()


def test_worker_gets_only_selected_key_outside_request_file(tmp_path, monkeypatch):
    config = LilBotConfig(workspace=tmp_path, model="configured", api_key="trial-only-synthetic-key")
    monkeypatch.setenv("OTHER_API_KEY", "unrelated-synthetic-key")
    task = TaskSpec("example", "Read the test fixture.", {}, ({"type": "run_completed"},), 30)

    def launch(command, **kwargs):
        request = Path(command[-1]).read_text(encoding="utf-8")
        assert config.api_key not in request
        assert all(config.api_key not in arg for arg in command)
        assert kwargs["env"]["LILBOT_API_KEY"] == config.api_key
        assert "OTHER_API_KEY" not in kwargs["env"]
        assert json.loads(request)["provider_settings"]["model"] == config.model
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", launch)
    _, error, _ = runner._run_worker(task, "example", tmp_path, tmp_path, tmp_path / "trace.jsonl", config)
    assert error is None
