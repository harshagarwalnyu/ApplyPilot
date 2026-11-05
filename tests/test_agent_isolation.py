"""The apply agent runs on its own Claude config dir once that dir has been logged in."""

from applypilot import config
from applypilot.apply import launcher


def test_agent_config_dir_requires_its_own_login(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    assert launcher.agent_config_dir() is None
    (tmp_path / "claude-agent").mkdir()
    assert launcher.agent_config_dir() is None  # an empty dir would just prompt for login mid-run
    (tmp_path / "claude-agent" / ".credentials.json").write_text("{}")
    assert launcher.agent_config_dir() == tmp_path / "claude-agent"
