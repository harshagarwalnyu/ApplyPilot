"""Submission safety tests: no live browser, credentials, or agent process."""
import io
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from applypilot.apply import launcher
from applypilot import config


@pytest.fixture(autouse=True)
def isolated_apply(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_EXCLUDE_CSV", raising=False)
    monkeypatch.setattr(config, "get_company_limit", lambda key: (-1, 30))
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher, "_roster_cache", None)


@pytest.mark.parametrize("status", ["applied", "unconfirmed", "in_progress", "needs_human"])
def test_url_refuses_protected_status(tmp_db, seed_job, status, caplog):
    conn = tmp_db()
    job = seed_job(conn, apply_status=status)
    assert launcher.acquire_job(target_url=job["url"]) is None
    assert "Refusing --url" in caplog.text
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] == status


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("match", ["url", "application_url", "role"])
def test_roster_excludes_and_continues(tmp_db, seed_job, tmp_path, monkeypatch, match, targeted):
    conn = tmp_db()
    job = seed_job(conn, company="Acme, Inc.", title="Senior Engineer!", fit_score=10)
    other = seed_job(conn, company="other", application_url="https://other.example/jobs/2", fit_score=9)
    roster = tmp_path / "roster.csv"
    url = job[match] + "?tracking=1#top" if match != "role" else "https://unrelated.example"
    roster.write_text(f'url,company,title\n{url},ACME INC,senior engineer\n' if match == "role"
                      else f'url,company,title\n{url},unrelated,unrelated\n', encoding="utf-8")
    monkeypatch.setenv("APPLYPILOT_EXCLUDE_CSV", str(roster))
    result = launcher.acquire_job(target_url=job["url"] if targeted else None, max_age_days=0)
    assert result is None if targeted else result["url"] == other["url"]
    assert conn.execute("SELECT apply_status FROM jobs WHERE url=?", (job["url"],)).fetchone()[0] == "excluded_roster"
    assert launcher.acquire_job(target_url=job["url"]) is None


@pytest.mark.parametrize("mode", ["missing", "stale", "unreadable", "invalid"])
def test_roster_fails_closed(tmp_db, seed_job, tmp_path, monkeypatch, mode):
    conn = tmp_db()
    job = seed_job(conn)
    roster = tmp_path / "roster.csv"
    if mode != "missing":
        roster.write_text("bad header\n" if mode == "invalid" else "url,company,title\n")
    if mode == "stale":
        os.utime(roster, (time.time() - 86401,) * 2)
    if mode == "unreadable":
        monkeypatch.setattr("builtins.open", Mock(side_effect=PermissionError("denied")))
    monkeypatch.setenv("APPLYPILOT_EXCLUDE_CSV", str(roster))
    assert launcher.acquire_job() is None
    assert launcher.acquire_job(target_url=job["url"]) is None
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] is None


def test_unset_roster_unchanged(tmp_db, seed_job):
    conn = tmp_db()
    job = seed_job(conn)
    assert launcher.acquire_job(max_age_days=0)["url"] == job["url"]


def test_roster_cache_reload_and_freshness(tmp_path, monkeypatch):
    roster = tmp_path / "roster.csv"
    roster.write_text("url,company,title\nHTTPS://A/,A,B\n")
    monkeypatch.setenv("APPLYPILOT_EXCLUDE_CSV", str(roster))
    first = launcher._load_exclusion_roster()
    assert launcher._load_exclusion_roster() is first
    roster.write_text("url,company,title\nhttps://other.example/,C,D\n")
    assert "https://other.example" in launcher._load_exclusion_roster()[0]
    os.utime(roster, (time.time() - 86401,) * 2)
    with pytest.raises(ValueError):
        launcher._load_exclusion_roster()


def test_stale_lock_is_unconfirmed(tmp_db, seed_job):
    conn = tmp_db()
    job = seed_job(conn, apply_status="in_progress", state="applying", last_attempted_at="2000-01-01 00:00:00")
    assert launcher.acquire_job(max_age_days=0) is None
    row = conn.execute("SELECT apply_status,state FROM jobs WHERE url=?", (job["url"],)).fetchone()
    assert tuple(row) == ("unconfirmed", "unconfirmed")


def test_interrupted_reconnect_is_unconfirmed(tmp_db, seed_job, monkeypatch):
    from applypilot.apply import orchestrator
    conn = tmp_db()
    seed_job(conn, apply_status="in_progress", state="applying", agent_id="worker-99")
    monkeypatch.setattr(orchestrator, "probe_existing_chrome", lambda *a: 123)
    monkeypatch.setattr(orchestrator, "add_event", lambda *a: None)
    assert orchestrator._probe_for_reconnect(99, 9999) == (123, None)
    assert tuple(conn.execute("SELECT apply_status,state FROM jobs").fetchone()) == ("unconfirmed", "unconfirmed")


@pytest.fixture
def fake_run(tmp_db, seed_job, tmp_path, monkeypatch):
    conn = tmp_db()
    job = seed_job(conn, apply_status="in_progress", state="applying")
    for name in ("APP_DIR", "LOG_DIR", "APPLY_WORKER_DIR"):
        monkeypatch.setattr(config, name, tmp_path)
    for name in ("_reset_browser_tabs", "reset_worker_dir", "_refresh_gmail_token", "update_state", "add_event", "_activate_agent_tab", "_parse_account_created", "_parse_qa_lines"):
        monkeypatch.setattr(launcher, name, lambda *a, **kw: None)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **kw: "test prompt")
    monkeypatch.setattr(launcher, "_make_mcp_config", lambda *a, **kw: {})
    monkeypatch.setattr(launcher, "detect_ats", lambda *a: None)
    monkeypatch.setattr(launcher, "_takeover_events", {})
    monkeypatch.setattr("applypilot.database.mark_qa_outcome", lambda *a: None)
    def run(output="", returncode=0, error=None, dry_run=False):
        proc = Mock()
        proc.stdin = io.StringIO()
        proc.stdout = iter([json.dumps({"type": "result", "result": output})])
        proc.returncode = returncode
        proc.wait.side_effect = error
        popen = Mock(return_value=proc)
        monkeypatch.setattr(launcher.subprocess, "Popen", popen)
        result = launcher.run_job(job, 9999, dry_run=dry_run)
        return result, popen
    return job, conn, run


@pytest.mark.parametrize("output,code,error", [
    ("RESULT:UNCONFIRMED:https://example.com", 0, None),
    ("unparseable output", 0, None),
    ("", -9, None),
    ("", 1, None),
    ("", 0, subprocess.TimeoutExpired("agent", 1)),
    ("", 0, RuntimeError("crashed")),
    ("RESULT:FAILED:unknown", 0, None),
])
def test_ambiguous_run_is_durable(fake_run, output, code, error):
    job, conn, run = fake_run
    (status, duration, _), _ = run(output, code, error)
    assert status == "unconfirmed"
    launcher.mark_result(job["url"], status, duration_ms=duration)
    assert tuple(conn.execute("SELECT apply_status,state FROM jobs").fetchone()) == ("unconfirmed", "unconfirmed")


def test_explicit_presubmit_failure(fake_run):
    _, _, run = fake_run
    assert run("RESULT:FAILED:not_eligible_location")[0][0] == "failed:not_eligible_location"


@pytest.mark.parametrize("gate", [False, RuntimeError("injection failed"), True])
def test_dry_run_gate_fail_closed(fake_run, monkeypatch, gate):
    from applypilot.apply import chrome
    job, conn, run = fake_run
    inject = Mock(side_effect=gate) if isinstance(gate, Exception) else Mock(return_value=gate)
    monkeypatch.setattr(chrome, "inject_dry_run_gate", inject)
    (status, duration, _), popen = run("RESULT:APPLIED", dry_run=True)
    if gate is True:
        popen.assert_called_once()
        assert status == "dry_run"
    else:
        popen.assert_not_called()
        assert status == "failed:dry_run_setup_error"
    launcher.mark_result(job["url"], status.split(":", 1)[0], duration_ms=duration)
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] != "applied"


def test_prompt_submit_once_and_captcha_preserved():
    from applypilot.apply import prompt, hitl
    source = Path(prompt.__file__).read_text(encoding="utf-8")
    step = source.split("11. After submit:", 1)[1].split("\n", 1)[0]
    assert "click Submit again" not in step
    assert "Never click Submit/Apply a second time" in step
    assert "RESULT:UNCONFIRMED:" in step
    for line in ('capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")',
                 '--- CAPTCHA DETECT ---', 'https://api.capsolver.com/createTask',
                 'https://api.capsolver.com/getTaskResult'):
        assert line in source
    assert hitl._HITL_INSTRUCTIONS["captcha"] == (
        "CAPTCHA DETECTED. Solve the CAPTCHA shown on the page, then click Done "
        "to let the agent continue the application.")


@pytest.mark.parametrize("verified", [False, True])
def test_chrome_gate_requires_verified_sentinel(monkeypatch, verified):
    from applypilot.apply import chrome
    import websocket
    ws = Mock()
    pending = []
    def send(raw):
        request = json.loads(raw)
        method = request["method"]
        if method == "Target.getTargets":
            result = {"targetInfos": [{"type": "page", "targetId": "page-1"}]}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "session-1"}
        elif method == "Page.addScriptToEvaluateOnNewDocument":
            result = {"identifier": "gate-script"}
        elif method == "Runtime.evaluate":
            expression = request["params"]["expression"]
            result = {"result": {"value": verified if expression.endswith("=== true") else None}}
        else:
            result = {}
        pending.append(json.dumps({"id": request["id"], "result": result}))
    ws.send.side_effect = send
    ws.recv.side_effect = lambda: pending.pop(0)
    monkeypatch.setattr(websocket, "create_connection", lambda *a, **kw: ws)
    response = Mock()
    response.read.return_value = b'{"webSocketDebuggerUrl":"ws://mock"}'
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response)
    assert chrome.inject_dry_run_gate(9999) is False
    ws.send.assert_not_called()


def test_prompt_generation_lock_can_be_released(tmp_db, seed_job):
    conn = tmp_db()
    job = seed_job(conn, apply_status="in_progress", state="applying")
    launcher.release_lock(job["url"], agent_started=False)
    assert tuple(conn.execute("SELECT apply_status,state FROM jobs").fetchone()) == (None, "ready_to_apply")


def test_unconfirmed_blocks_duplicate_application(tmp_db, seed_job):
    conn = tmp_db()
    seed_job(conn, apply_status="unconfirmed")
    seed_job(conn, url_suffix="duplicate")
    assert launcher.acquire_job(max_age_days=0) is None


def test_url_cannot_bypass_duplicate_guard(tmp_db, seed_job):
    conn = tmp_db()
    seed_job(conn, apply_status="unconfirmed")
    duplicate = seed_job(conn, url_suffix="target-duplicate")
    assert launcher.acquire_job(target_url=duplicate["url"]) is None


def test_roster_io_precedes_write_transaction(tmp_db, seed_job, tmp_path, monkeypatch):
    conn = tmp_db()
    seed_job(conn)
    roster = tmp_path / "roster.csv"
    roster.write_text("url,company,title\n")
    monkeypatch.setenv("APPLYPILOT_EXCLUDE_CSV", str(roster))
    original = launcher._load_exclusion_roster
    def load():
        assert not conn.in_transaction
        return original()
    monkeypatch.setattr(launcher, "_load_exclusion_roster", load)
    assert launcher.acquire_job(max_age_days=0) is not None


@pytest.mark.parametrize("status", ["applied", "unconfirmed", "in_progress", "needs_human", "excluded_roster", "manual"])
@pytest.mark.parametrize("action", ["applied", "skip", "error", "reset"])
def test_extension_rejects_protected_actions(tmp_db, seed_job, status, action):
    import urllib.request
    import urllib.error
    conn = tmp_db()
    job = seed_job(conn, apply_status=status)
    port = launcher._start_worker_listener(70)
    try:
        request = urllib.request.Request(
            f"http://localhost:{port}/api/jobs/mark",
            data=json.dumps({"url": job["url"], "action": action}).encode(),
            headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 409
        assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] == status
        with urllib.request.urlopen(f"http://localhost:{port}/api/jobs", timeout=5) as response:
            assert json.load(response)["jobs"] == []
    finally:
        launcher._stop_worker_listener(70)


@pytest.mark.parametrize("reason", ["human submitted", "interrupted review", "no-hitl"])
def test_restart_quarantines_human_review(tmp_db, seed_job, reason):
    from applypilot.apply.hitl import reset_needs_human
    conn = tmp_db()
    seed_job(conn, apply_status="needs_human", needs_human_reason=reason)
    assert reset_needs_human() == 1
    launcher.reset_failed()
    assert launcher.acquire_job(max_age_days=0) is None
    assert tuple(conn.execute("SELECT apply_status,state FROM jobs").fetchone()) == ("unconfirmed", "unconfirmed")


def test_human_done_never_relaunches_or_requeues(tmp_db, seed_job):
    from applypilot.apply.hitl import reset_needs_human
    conn = tmp_db()
    job = seed_job(conn, apply_status="needs_human")
    assert launcher.acquire_job(worker_id=2, max_age_days=0) is None
    reset_needs_human(job["url"])
    # A crash immediately after Done leaves quarantine durable.
    assert launcher.acquire_job(worker_id=3, max_age_days=0) is None
    source = Path(__import__("applypilot.apply.hitl", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert 'last_result, last_dur, last_qs = run_job(' not in source


@pytest.mark.parametrize("crash", [False, True])
def test_hitl_done_quarantines_before_return(tmp_db, seed_job, monkeypatch, crash):
    from applypilot.apply import hitl, human_review
    conn = tmp_db()
    seed_job(conn)
    job = launcher.acquire_job(max_age_days=0)
    def done(worker, event, job_hash):
        assert launcher.acquire_job(worker_id=2, max_age_days=0) is None
        event.set()
        return 1234
    monkeypatch.setattr(hitl, "_start_hitl_listener", done)
    for name in ("_inject_banner_for_worker", "notify_human_needed", "_stop_hitl_listener",
                 "_register_waiting", "_unregister_waiting"):
        monkeypatch.setattr(hitl, name, lambda *a, **kw: None)
    monkeypatch.setattr(human_review, "_start_done_watcher", lambda *a: None)
    monkeypatch.setattr(hitl, "_stdin_fallback_lock", Mock(acquire=Mock(return_value=False)))
    agent = Mock(side_effect=AssertionError("must never relaunch"))
    monkeypatch.setattr(launcher, "run_job", agent)
    original = hitl.reset_needs_human
    def quarantine(url):
        original(url)
        assert launcher.acquire_job(worker_id=3, max_age_days=0) is None
        if crash:
            raise RuntimeError("crash after durable quarantine")
    monkeypatch.setattr(hitl, "reset_needs_human", quarantine)
    args = dict(worker_id=71, port=9999, job=job, reason="login_required",
                instructions="review", navigate_url=job["application_url"], duration_ms=10)
    if crash:
        with pytest.raises(RuntimeError, match="durable quarantine"):
            hitl._run_hitl(**args)
    else:
        assert hitl._run_hitl(**args)[0] == "unconfirmed"
    agent.assert_not_called()
    assert tuple(conn.execute("SELECT apply_status,state FROM jobs").fetchone()) == ("unconfirmed", "unconfirmed")


@pytest.mark.parametrize("output", ["Clicked submit. session has now expired", "Clicked submit. insufficient credits", "Your application has been submitted successfully"])
def test_missing_result_failure_phrase_is_quarantined(fake_run, output):
    _, _, run = fake_run
    assert run(output)[0][0] == "unconfirmed"


def test_dry_run_popup_cannot_start_agent(fake_run):
    # Exercise the real gate and run_job boundary without opening a browser:
    # no agent means no new-tab or popup submit can occur.
    _, _, run = fake_run
    (status, _, _), popen = run("RESULT:APPLIED", dry_run=True)
    assert status == "failed:dry_run_setup_error"
    popen.assert_not_called()


@pytest.mark.parametrize("output", ["RESULT:ALREADY_APPLIED", "You have already applied for this job"])
def test_dry_run_preserves_existing_application(fake_run, monkeypatch, output):
    from applypilot.apply import chrome
    monkeypatch.setattr(chrome, "inject_dry_run_gate", lambda *a: True)
    job, conn, run = fake_run
    (status, _, _), _ = run(output, dry_run=True)
    assert status == "applied"
    launcher.mark_result(job["url"], status)
    launcher.reset_failed()
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] == "applied"


@pytest.mark.parametrize("status", ["applied", "unconfirmed", "needs_human", "in_progress", "excluded_roster"])
def test_cli_mark_failed_cannot_erase_protection(tmp_db, seed_job, status):
    conn = tmp_db()
    job = seed_job(conn, apply_status=status)
    with pytest.raises(ValueError, match="reconciliation"):
        launcher.mark_job(job["url"], "failed")
    launcher.reset_failed()
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] == status


def test_live_long_running_worker_is_not_stolen(tmp_db, seed_job):
    import socket
    conn = tmp_db()
    job = seed_job(conn)
    owned = launcher.acquire_job(worker_id=1, max_age_days=0)
    conn.execute("UPDATE jobs SET last_attempted_at='2000-01-01 00:00:00'")
    conn.commit()
    seed_job(conn, url_suffix="duplicate-live")
    assert launcher._worker_may_be_alive(os.getpid(), socket.gethostname())
    assert launcher.acquire_job(worker_id=2, max_age_days=0) is None
    assert conn.execute("SELECT apply_status FROM jobs WHERE url=?", (job["url"],)).fetchone()[0] == "in_progress"
    launcher.mark_result(job["url"], "failed", lease_token="wrong-generation")
    assert conn.execute("SELECT apply_status FROM jobs WHERE url=?", (job["url"],)).fetchone()[0] == "in_progress"
    launcher.mark_result(job["url"], "applied", lease_token=owned["lease_token"])
    assert conn.execute("SELECT apply_status FROM jobs WHERE url=?", (job["url"],)).fetchone()[0] == "applied"


def test_dead_owner_quarantine_fences_late_writes(tmp_db, seed_job, monkeypatch):
    from applypilot.apply.hitl import mark_needs_human
    conn = tmp_db()
    job = seed_job(conn)
    owned = launcher.acquire_job(max_age_days=0)
    conn.execute("UPDATE jobs SET last_attempted_at='2000-01-01 00:00:00'")
    conn.commit()
    monkeypatch.setattr(launcher, "_worker_may_be_alive", lambda *a: False)
    assert launcher.acquire_job(worker_id=2, max_age_days=0) is None
    launcher.mark_result(job["url"], "failed", lease_token=owned["lease_token"])
    assert not mark_needs_human(job["url"], "test", "url", "test", lease_token=owned["lease_token"])
    launcher.release_lock(job["url"], agent_started=False, lease_token=owned["lease_token"])
    assert tuple(conn.execute("SELECT apply_status,state FROM jobs").fetchone()) == ("unconfirmed", "unconfirmed")


@pytest.mark.parametrize("excluded", [True, False])
def test_bounded_candidate_pages_progress(tmp_db, seed_job, monkeypatch, excluded):
    conn = tmp_db()
    for index in range(205):
        seed_job(conn, company="blocked", fit_score=10,
                 application_url=f"https://example.org/{index}")
    expected = seed_job(conn, company="allowed", fit_score=9,
                        application_url="https://example.org/allowed")
    if excluded:
        monkeypatch.setattr(launcher, "_load_exclusion_roster", lambda: (set(), {("blocked", "seniorsoftwareengineer")}))
    else:
        monkeypatch.setattr(config, "get_company_limit", lambda key: (0 if key == "blocked" else -1, 30))
    queries = []
    conn.set_trace_callback(queries.append)
    result = launcher.acquire_job(max_age_days=0)
    assert result["url"] == expected["url"]
    pages = [q for q in queries if "FROM jobs j" in q]
    assert len(pages) == 3
    assert all("LIMIT 100" in q for q in pages)
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT 1 FROM jobs WHERE application_url=? AND apply_status=?", ("x", "applied")).fetchall()
    assert any("idx_jobs_application_status" in row[3] for row in plan)


@pytest.mark.parametrize("duplicate_heavy", [False, True])
def test_large_queue_concurrent_acquisition(tmp_db, seed_job, monkeypatch, duplicate_heavy):
    from concurrent.futures import ThreadPoolExecutor
    from applypilot.database import get_connection
    conn = tmp_db()
    template = seed_job(conn, apply_status="manual")
    rows = []
    for index in range(4000):
        app_url = f"https://example.org/{index}"
        rows.append((f"https://list.example/{index}", f"company-{index}", app_url,
                     template["discovered_at"], None))
        if duplicate_heavy and index < 3900:
            rows.append((f"https://applied.example/{index}", f"company-{index}", app_url,
                         template["discovered_at"], "applied"))
    conn.executemany("INSERT INTO jobs (url,company,application_url,discovered_at,apply_status,"
                     "title,site,fit_score,tailored_resume_path) "
                     "VALUES (?,?,?,?,?,'Engineer','example',9,'/tmp/r.pdf')", rows)
    conn.commit()
    monkeypatch.setattr(launcher, "detect_ats", lambda *a: None)
    def acquire(worker):
        try:
            return launcher.acquire_job(worker_id=worker, max_age_days=0)
        finally:
            get_connection().close()
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        acquired = list(pool.map(acquire, range(4)))
    elapsed = time.monotonic() - start
    assert all(acquired)
    assert len({job["application_url"] for job in acquired}) == 4
    if duplicate_heavy:
        assert all(int(job["application_url"].rsplit("/", 1)[1]) >= 3900 for job in acquired)
    print(f"queue benchmark: duplicate_heavy={duplicate_heavy}, rows={len(rows)}, workers=4, seconds={elapsed:.3f}")


@pytest.mark.parametrize("a,b,same", [
    ("https://stripe.com/jobs/search?gh_jid=8128745", "https://stripe.com/jobs/search?gh_jid=8128744", False),
    ("https://stripe.com/jobs/search?gh_jid=8128745&utm_source=x", "https://Stripe.com/jobs/search?gh_jid=8128745#apply", True),
    ("https://jobs.lever.co/acme/123/apply/", "https://jobs.lever.co/acme/123/apply?lever-source=linkedin", True),
])
def test_roster_url_keeps_posting_ids(a, b, same):
    assert (launcher._roster_url(a) == launcher._roster_url(b)) is same
