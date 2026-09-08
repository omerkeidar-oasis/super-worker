"""Tests for workspace persistence — the UI-state layer and ProjectView restore.

- Unit tests for super_worker.services.ui_state (save/load round-trip, atomic
  write, and the never-raise-on-missing/corrupt-file contract).
- ProjectView restore tests: a mounted ProjectView adopts the persisted
  worktree tab + session, and falls back gracefully for stale hints.

Everything runs against a redirected STATE_DIR and a mocked tmux server; nothing
touches the real ~/.config/sw files.
"""

import json

import pytest
from textual.app import App, ComposeResult
from unittest.mock import MagicMock

from super_worker.models import AppState, Session, Worktree
from super_worker.services.tmux import SessionState
from super_worker.services.ui_state import (
    ProjectUIState,
    UIState,
    load_ui_state,
    save_ui_state,
)
from super_worker.widgets.project_view import ProjectView


# ── ui_state module unit tests ─────────────────────────────────────────────────


@pytest.fixture()
def ui_dir(tmp_path, monkeypatch):
    """Redirect ui_state.STATE_DIR to a temp dir for isolation."""
    d = tmp_path / "sw-state"
    d.mkdir()
    monkeypatch.setattr("super_worker.services.ui_state.STATE_DIR", d)
    return d


class TestUIStateRoundTrip:
    def test_save_load_roundtrip(self, ui_dir):
        state = UIState(
            open_projects=["/repo/a", "/repo/b"],
            projects={
                "/repo/a": ProjectUIState(worktree="feat", session="sw-feat-0"),
                "/repo/b": ProjectUIState(worktree="main", session=None),
            },
            sidebar_width=42,
        )
        save_ui_state(state)

        loaded = load_ui_state()
        assert loaded.open_projects == ["/repo/a", "/repo/b"]
        assert loaded.projects["/repo/a"].worktree == "feat"
        assert loaded.projects["/repo/a"].session == "sw-feat-0"
        assert loaded.projects["/repo/b"].session is None
        assert loaded.sidebar_width == 42

    def test_atomic_write_leaves_no_tmp_file(self, ui_dir):
        save_ui_state(UIState(sidebar_width=10))
        assert (ui_dir / "ui-state.json").exists()
        assert list(ui_dir.glob("*.tmp")) == [], "tmp file must be replaced, not left behind"

    def test_save_creates_missing_state_dir(self, tmp_path, monkeypatch):
        missing = tmp_path / "does-not-exist-yet"
        monkeypatch.setattr("super_worker.services.ui_state.STATE_DIR", missing)
        save_ui_state(UIState(sidebar_width=7))
        assert (missing / "ui-state.json").exists()


class TestUIStateRobustness:
    def test_missing_file_returns_empty_state(self, ui_dir):
        loaded = load_ui_state()
        assert loaded.open_projects == []
        assert loaded.projects == {}
        assert loaded.sidebar_width is None

    def test_corrupt_json_returns_empty_state(self, ui_dir):
        (ui_dir / "ui-state.json").write_text("{ this is not valid json ")
        loaded = load_ui_state()
        assert loaded.open_projects == []
        assert loaded.sidebar_width is None

    def test_wrong_shape_returns_empty_state(self, ui_dir):
        # A JSON array where an object is expected must not raise.
        (ui_dir / "ui-state.json").write_text(json.dumps(["a", "b"]))
        loaded = load_ui_state()
        assert loaded.open_projects == []

    def test_extra_keys_are_ignored(self, ui_dir):
        (ui_dir / "ui-state.json").write_text(
            json.dumps({"open_projects": ["/x"], "future_field": 123})
        )
        loaded = load_ui_state()
        assert loaded.open_projects == ["/x"]


# ── ProjectView restore tests ──────────────────────────────────────────────────


def _make_state(repo: str) -> AppState:
    """Two worktrees; 'main' has two sessions, 'feat' has one. Paths = repo (exists)."""
    main = Worktree(
        name="main", path=repo, branch="main",
        sessions=[
            Session(tmux_session_name="sw-main-0", label="m0"),
            Session(tmux_session_name="sw-main-1", label="m1"),
        ],
    )
    feat = Worktree(
        name="feat", path=repo, branch="sw-feat",
        sessions=[Session(tmux_session_name="sw-feat-0", label="f0")],
    )
    return AppState(repo_root=repo, worktree_base=repo, worktrees=[main, feat])


class _PVHost(App):
    def __init__(self, config, state, restore_worktree=None, restore_session=None):
        super().__init__()
        self._config = config
        self._state = state
        self._restore_worktree = restore_worktree
        self._restore_session = restore_session

    def compose(self) -> ComposeResult:
        yield ProjectView(
            self._config, self._state,
            restore_worktree=self._restore_worktree,
            restore_session=self._restore_session,
            id="pv-test",
        )


@pytest.fixture()
def pv_env(monkeypatch):
    """Mock the tmux + git boundaries so a ProjectView can mount deterministically."""
    mock_session = MagicMock()
    mock_session.session_name = "sw-main-0"
    mock_session.active_pane = MagicMock()
    mock_session.active_pane.capture_pane.return_value = ["output"]
    mock_session.show_environment.return_value = {}
    mock_server = MagicMock()
    mock_server.sessions = [mock_session]
    mock_server.new_session.return_value = mock_session
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

    monkeypatch.setattr(
        "super_worker.widgets.project_view.get_branch_status",
        lambda *a, **k: {"ahead": 0, "behind": 0},
    )
    monkeypatch.setattr(
        "super_worker.widgets.project_view.get_worktree_dirty", lambda *a, **k: False
    )
    monkeypatch.setattr(
        "super_worker.widgets.project_view.batch_detect_session_states",
        lambda names: {n: SessionState.RUNNING for n in names},
    )
    monkeypatch.setattr(
        "super_worker.services.state.get_current_branch", lambda *a, **k: "main"
    )


@pytest.mark.asyncio
async def test_restore_selects_nonfirst_worktree_and_session(pv_env, fake_config):
    """A persisted non-first worktree tab + session is adopted on mount."""
    state = _make_state(str(fake_config.repo_root))
    host = _PVHost(fake_config, state, restore_worktree="feat", restore_session="sw-feat-0")
    async with host.run_test(size=(120, 40)) as pilot:
        await pilot.pause(delay=1.0)
        pv = host.query_one(ProjectView)
        assert pv.active_worktree_name == "feat"
        assert pv.active_session_name == "sw-feat-0"


@pytest.mark.asyncio
async def test_restore_selects_session_in_first_worktree(pv_env, fake_config):
    """A persisted non-default session in the first worktree is adopted."""
    state = _make_state(str(fake_config.repo_root))
    host = _PVHost(fake_config, state, restore_worktree="main", restore_session="sw-main-1")
    async with host.run_test(size=(120, 40)) as pilot:
        await pilot.pause(delay=1.0)
        pv = host.query_one(ProjectView)
        assert pv.active_worktree_name == "main"
        assert pv.active_session_name == "sw-main-1"


@pytest.mark.asyncio
async def test_restore_stale_session_falls_back_to_first(pv_env, fake_config):
    """A restore session that no longer exists falls back to the worktree's first."""
    state = _make_state(str(fake_config.repo_root))
    host = _PVHost(fake_config, state, restore_worktree="feat", restore_session="sw-gone-9")
    async with host.run_test(size=(120, 40)) as pilot:
        await pilot.pause(delay=1.0)
        pv = host.query_one(ProjectView)
        assert pv.active_worktree_name == "feat"
        assert pv.active_session_name == "sw-feat-0"


@pytest.mark.asyncio
async def test_restore_stale_worktree_falls_back_to_first(pv_env, fake_config):
    """A restore worktree that no longer exists falls back to the first worktree."""
    state = _make_state(str(fake_config.repo_root))
    host = _PVHost(fake_config, state, restore_worktree="ghost", restore_session="sw-x-0")
    async with host.run_test(size=(120, 40)) as pilot:
        await pilot.pause(delay=1.0)
        pv = host.query_one(ProjectView)
        assert pv.active_worktree_name == "main"
        assert pv.active_session_name == "sw-main-0"
