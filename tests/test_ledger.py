"""Tests for the trust-ledger capture service (verdict cockpit, slice d).

The subprocess boundary is mocked — these assert the exact argv the ledger CLI
is invoked with, that it runs in the worktree's directory, how the command is
resolved from config, and that every failure mode comes back as (False, reason)
rather than raising.
"""

import subprocess
from unittest.mock import MagicMock

import pytest

from super_worker.config import (
    LedgerConfig,
    SWConfig,
    load_config,
    load_toml,
    save_project_config,
)
from super_worker.services.ledger import (
    DEFAULT_LEDGER_CMD,
    LEDGER_EVENTS,
    build_ledger_argv,
    log_ledger_event,
)


def _mock_run(monkeypatch, *, returncode=0, stdout="ledger: logged", stderr="", exc=None):
    """Patch subprocess.run in the ledger module; return a dict capturing the call."""
    calls: dict = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        if exc is not None:
            raise exc
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result

    monkeypatch.setattr("super_worker.services.ledger.subprocess.run", fake_run)
    return calls


# ── build_ledger_argv: exact event → argv mapping ──────────────────────────

def test_argv_basic_always_sends_task():
    assert build_ledger_argv("ledger.sh", "agreed", "sw-feat") == [
        "ledger.sh", "log", "agreed", "--task", "sw-feat",
    ]


def test_argv_includes_note_when_given():
    assert build_ledger_argv("ledger.sh", "override", "sw-feat", note="bad abstraction") == [
        "ledger.sh", "log", "override", "--task", "sw-feat", "--note", "bad abstraction",
    ]


def test_argv_escape_defaults_attributed_task_to_task():
    # Design: escape's attributed task defaults to the active worktree's branch.
    assert build_ledger_argv("ledger.sh", "escape", "sw-feat") == [
        "ledger.sh", "log", "escape", "--task", "sw-feat", "--attributed-task", "sw-feat",
    ]


def test_argv_escape_uses_explicit_attributed_task():
    assert build_ledger_argv("ledger.sh", "escape", "sw-feat", attributed_task="PROJ-123") == [
        "ledger.sh", "log", "escape", "--task", "sw-feat", "--attributed-task", "PROJ-123",
    ]


@pytest.mark.parametrize("event", ["agreed", "override", "false_alarm"])
def test_argv_non_escape_never_sends_attributed_task(event):
    # The CLI rejects --attributed-task on non-escape events, so it must be absent.
    argv = build_ledger_argv("ledger.sh", event, "sw-feat", attributed_task="PROJ-9")
    assert "--attributed-task" not in argv


def test_argv_note_and_escape_attribution_together():
    assert build_ledger_argv(
        "ledger.sh", "escape", "sw-feat", note="null deref", attributed_task="PROJ-1",
    ) == [
        "ledger.sh", "log", "escape", "--task", "sw-feat",
        "--note", "null deref", "--attributed-task", "PROJ-1",
    ]


def test_argv_shell_splits_command_with_args():
    argv = build_ledger_argv("bash /opt/ledger.sh", "agreed", "sw-feat")
    assert argv[:5] == ["bash", "/opt/ledger.sh", "log", "agreed", "--task"]


def test_argv_empty_command_falls_back_to_default():
    assert build_ledger_argv("", "agreed", "sw-feat")[0] == DEFAULT_LEDGER_CMD


# ── log_ledger_event: invocation, cwd, results, failure modes ───────────────

def test_log_runs_in_worktree_dir_with_exact_argv(monkeypatch):
    calls = _mock_run(monkeypatch)
    ok, _ = log_ledger_event("ledger.sh", "override", "/repo/wt-feat", "sw-feat", note="nope")
    assert ok is True
    assert calls["argv"] == [
        "ledger.sh", "log", "override", "--task", "sw-feat", "--note", "nope",
    ]
    assert calls["kwargs"]["cwd"] == "/repo/wt-feat"


@pytest.mark.parametrize("event", list(LEDGER_EVENTS))
def test_log_all_events_invoke_cli(monkeypatch, event):
    calls = _mock_run(monkeypatch)
    ok, _ = log_ledger_event("ledger.sh", event, "/repo", "sw-feat")
    assert ok is True
    assert calls["argv"][1:3] == ["log", event]
    assert calls["kwargs"]["cwd"] == "/repo"


def test_log_success_returns_cli_stdout(monkeypatch):
    _mock_run(monkeypatch, stdout="ledger: logged 'agreed' for [repo] task 'sw-feat'")
    ok, msg = log_ledger_event("ledger.sh", "agreed", "/repo", "sw-feat")
    assert ok is True
    assert "logged 'agreed'" in msg


def test_log_nonzero_exit_returns_stderr(monkeypatch):
    _mock_run(monkeypatch, returncode=1, stderr="ledger: jq is required")
    ok, msg = log_ledger_event("ledger.sh", "agreed", "/repo", "sw-feat")
    assert ok is False
    assert "jq is required" in msg


def test_log_missing_binary_reports_config_hint(monkeypatch):
    _mock_run(monkeypatch, exc=FileNotFoundError())
    ok, msg = log_ledger_event("nope.sh", "agreed", "/repo", "sw-feat")
    assert ok is False
    assert ".sw.toml" in msg and "not found" in msg


def test_log_timeout_is_caught(monkeypatch):
    _mock_run(monkeypatch, exc=subprocess.TimeoutExpired(cmd="ledger.sh", timeout=15))
    ok, msg = log_ledger_event("ledger.sh", "agreed", "/repo", "sw-feat")
    assert ok is False
    assert "timed out" in msg


def test_log_unknown_event_rejected_without_subprocess(monkeypatch):
    calls = _mock_run(monkeypatch)
    ok, msg = log_ledger_event("ledger.sh", "bogus", "/repo", "sw-feat")
    assert ok is False
    assert "Unknown ledger event" in msg
    assert calls == {}, "subprocess must not run for an invalid event"


# ── config: ledger_cmd resolution and round-trip ────────────────────────────

def test_resolved_config_defaults_ledger_cmd(fake_config):
    assert fake_config.ledger_cmd == "ledger.sh"


def test_load_config_reads_ledger_cmd(tmp_path, monkeypatch):
    import git as gitpython

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".sw.toml").write_text('[ledger]\ncmd = "bash /opt/ledger.sh"\n')

    mock_repo = MagicMock()
    mock_repo.working_dir = str(repo_root)
    origin = MagicMock()
    origin.name = "origin"
    mock_repo.remotes = [origin]
    mock_repo.git.symbolic_ref.return_value = "refs/remotes/origin/main"
    mock_repo.git.rev_parse.return_value = ".git"
    monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

    cfg = load_config(str(repo_root))
    assert cfg.ledger_cmd == "bash /opt/ledger.sh"


def test_ledger_config_round_trips(tmp_path):
    path = save_project_config(tmp_path, SWConfig(ledger=LedgerConfig(cmd="scripts/ledger.sh")))
    assert load_toml(path).ledger.cmd == "scripts/ledger.sh"


def test_empty_ledger_config_writes_nothing(tmp_path):
    path = save_project_config(tmp_path, SWConfig())
    assert "ledger" not in path.read_text()
