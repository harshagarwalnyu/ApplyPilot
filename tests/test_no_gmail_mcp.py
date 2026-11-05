"""Owner refuses OAuth: the apply agent gets no Gmail tool and must park emailed codes for the operator."""
import inspect

from applypilot.apply import launcher, prompt


def test_mcp_config_has_no_gmail_server(monkeypatch):
    import applypilot.apply.chrome as chrome
    monkeypatch.setattr(chrome, "_get_real_user_agent", lambda: "UA")
    monkeypatch.setattr(chrome, "get_worker_viewport", lambda wid: (1280, 800))
    servers = launcher._make_mcp_config(9222, worker_id=0)["mcpServers"]
    assert set(servers) == {"playwright"}


def test_prompt_never_tells_agent_to_use_gmail():
    src = inspect.getsource(prompt).lower()
    assert "gmail mcp" not in src
    assert "search_emails" not in src
    assert "needs_human:email_verification" in src


def test_run_job_does_not_refresh_gmail_token():
    assert "_refresh_gmail_token()" not in inspect.getsource(launcher.run_job)
