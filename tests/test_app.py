"""App-level integration tests using Textual's run_test framework.

These tests start the real app in headless mode and drive it via Pilot.
Only the tmux server is mocked (external boundary) — all internal functions
run for real against a redirected state directory.
"""

import asyncio
import shutil

import git as gitpython
import pytest
from unittest.mock import MagicMock

from textual.widgets import Input

from super_worker.app import SuperWorkerApp
from super_worker.screens import (
    CommitMessageScreen,
    ConfigScreen,
    ConfirmDeleteScreen,
    NewSessionScreen,
    NewWorktreeScreen,
    RenameSessionScreen,
)
from super_worker.models import Session, Worktree
from super_worker.services.state import load_state, save_state
from super_worker.services.ui_state import UIState, load_ui_state, save_ui_state
from super_worker.widgets.project_view import WorktreeTabContent
from super_worker.widgets.sidebar import SessionDeleted, SidebarDivider
from super_worker.widgets.terminal_pane import TerminalPane


class _FakeMouse:
    """Minimal stand-in for a Textual MouseEvent (only what the divider reads)."""

    def __init__(self, screen_x: int) -> None:
        self.screen_x = screen_x

    def stop(self) -> None:
        pass


def _make_mock_server():
    """Create a mock libtmux server that satisfies all tmux operations."""
    mock_session = MagicMock()
    mock_session.session_name = "sw-test-0"
    mock_session.active_pane = MagicMock()
    mock_session.active_pane.capture_pane.return_value = ["test output"]
    mock_session.show_environment.return_value = {}

    mock_server = MagicMock()
    mock_server.sessions = [mock_session]
    mock_server.new_session.return_value = mock_session
    return mock_server


@pytest.fixture(autouse=True)
def isolate_externals(tmp_path, monkeypatch):
    """Mock only the tmux server and redirect state dir — everything else is real."""
    from super_worker.widgets.sidebar import SidebarDivider

    state_dir = tmp_path / "sw-state"
    state_dir.mkdir()
    monkeypatch.setattr("super_worker.services.state.STATE_DIR", state_dir)
    # Redirect the workspace UI-state file too, so tests never read/write the
    # real ~/.config/sw/ui-state.json (which would also reopen the user's real
    # projects on startup).
    monkeypatch.setattr("super_worker.services.ui_state.STATE_DIR", state_dir)

    # Sidebar width is class-level state seeded from ui-state — reset it so a
    # value from another test can't leak in.
    monkeypatch.setattr(SidebarDivider, "_shared_width", None)

    mock_server = _make_mock_server()
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)


def _pv(app: SuperWorkerApp):
    """Shorthand: get the active ProjectView."""
    return app._active_project_view


@pytest.mark.asyncio
async def test_app_starts():
    """App starts without import errors or initialization crashes."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        assert app.is_running
        pv = _pv(app)
        assert pv is not None
        assert len(pv._state.worktrees) >= 1


@pytest.mark.asyncio
async def test_new_worktree_modal_open_and_cancel():
    """Ctrl+N opens NewWorktreeScreen, Escape dismisses it."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, NewWorktreeScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, NewWorktreeScreen)


@pytest.mark.asyncio
async def test_new_worktree_creates_tab(monkeypatch):
    """Submitting NewWorktreeScreen creates a worktree and adds a tab."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        wt_dir = pv._config.base_dir / f"{pv._config.worktree_prefix}-test-feat"

        def fake_worktree_cmd(*args):
            if args[0] == "add":
                wt_dir.mkdir(parents=True, exist_ok=True)

        mock_repo = MagicMock()
        mock_repo.git.rev_parse.side_effect = gitpython.GitCommandError("rev-parse", 1)
        mock_repo.git.worktree.side_effect = fake_worktree_cmd
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        initial_count = len(pv._state.worktrees)

        await pilot.press("ctrl+n")
        await pilot.pause()
        app.screen.query_one("#wt-name", Input).value = "test-feat"
        await pilot.press("enter")
        await pilot.pause(delay=2.0)

        assert len(pv._state.worktrees) == initial_count + 1
        assert pv._active_worktree is not None
        assert pv._active_worktree.name == "test-feat"

        if wt_dir.exists():
            shutil.rmtree(wt_dir)


@pytest.mark.asyncio
async def test_new_session_creates_and_selects():
    """Creating a session adds it, activates it, and selects it in sidebar."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        wt = pv._state.worktrees[0]
        pv._active_worktree = wt
        initial_count = len(wt.sessions)

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, NewSessionScreen)

        await pilot.press("enter")
        await pilot.pause(delay=2.0)

        assert len(wt.sessions) == initial_count + 1
        assert pv._active_session_name == wt.sessions[-1].tmux_session_name

        # Terminal shows the new session
        wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
        terminal = wtc.query_one(TerminalPane)
        assert terminal.active_session == wt.sessions[-1].tmux_session_name


@pytest.mark.asyncio
async def test_new_session_cancel():
    """Escape dismisses NewSessionScreen without creating a session."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        wt = pv._state.worktrees[0]
        pv._active_worktree = wt
        initial_sessions = len(wt.sessions)

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, NewSessionScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, NewSessionScreen)
        assert len(wt.sessions) == initial_sessions


@pytest.mark.asyncio
async def test_rename_session():
    """F2 opens RenameSessionScreen and renaming updates the label."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        wt = pv._state.worktrees[0]
        session = wt.sessions[0]
        pv._active_worktree = wt
        pv._active_session_name = session.tmux_session_name

        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(app.screen, RenameSessionScreen)

        app.screen.query_one("#rename-input", Input).value = "renamed"
        await pilot.press("enter")
        await pilot.pause()

        assert session.label == "renamed"


@pytest.mark.asyncio
async def test_delete_main_worktree_blocked():
    """Cannot delete the main worktree."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        _pv(app)._active_worktree = _pv(app)._state.worktrees[0]
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmDeleteScreen)


@pytest.mark.asyncio
async def test_delete_worktree_opens_confirm():
    """Ctrl+D opens ConfirmDeleteScreen for non-main worktrees."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        wt = Worktree(name="feature", path=str(pv._config.repo_root), branch="sw-feature")
        pv._state.worktrees.append(wt)
        pv._active_worktree = wt

        await pilot.press("ctrl+d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDeleteScreen)


@pytest.mark.asyncio
async def test_delete_only_session_clears_terminal():
    """Deleting the sole session removes it from state and clears the terminal."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        wt = pv._state.worktrees[0]
        session = wt.sessions[0]
        pv._active_worktree = wt
        pv._active_session_name = session.tmux_session_name

        wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
        terminal = wtc.query_one(TerminalPane)
        terminal.active_session = session.tmux_session_name
        await pilot.pause()

        pv.post_message(SessionDeleted(wt, session))
        await pilot.pause(delay=1.0)

        assert len(wt.sessions) == 0
        assert terminal.active_session is None
        assert pv._active_session_name is None


@pytest.mark.asyncio
async def test_delete_session_auto_selects_another():
    """Deleting a session when others remain auto-selects the first remaining session."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        wt = pv._state.worktrees[0]
        pv._active_worktree = wt

        # Create a second session
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(delay=2.0)

        assert len(wt.sessions) == 2
        first_session = wt.sessions[0]
        second_session = wt.sessions[1]

        wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
        terminal = wtc.query_one(TerminalPane)
        terminal.active_session = first_session.tmux_session_name
        pv._active_session_name = first_session.tmux_session_name

        pv.post_message(SessionDeleted(wt, first_session))
        await pilot.pause(delay=1.0)

        assert len(wt.sessions) == 1
        assert pv._active_session_name == second_session.tmux_session_name
        assert terminal.active_session == second_session.tmux_session_name


@pytest.mark.asyncio
async def test_commit_dialog_opens():
    """Git commit action opens the CommitMessageScreen."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        wt = pv._state.worktrees[0]
        pv._git_commit(wt)
        await pilot.pause()
        assert isinstance(app.screen, CommitMessageScreen)


@pytest.mark.asyncio
async def test_settings_modal_opens():
    """Ctrl+E opens ConfigScreen."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert isinstance(app.screen, ConfigScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,active_wt,active_session,screen_type", [
    ("f2", True, False, RenameSessionScreen),
    ("ctrl+a", False, False, None),
])
async def test_no_active_session_does_not_crash(key, active_wt, active_session, screen_type):
    """Actions requiring an active session warn gracefully."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        if not active_wt:
            pv._active_worktree = None
        pv._active_session_name = None
        await pilot.press(key)
        await pilot.pause()
        if screen_type:
            assert not isinstance(app.screen, screen_type)


@pytest.mark.asyncio
async def test_remove_active_project_unmounts_and_shows_placeholder():
    """Removing the active project unmounts its view and shows the placeholder.

    Regression: the ProjectView used to stay mounted (interactive behind the
    switcher), and reopening the project crashed the app with DuplicateIds.
    """
    from textual.widgets import ContentSwitcher, Static
    from super_worker.widgets.project_drawer import ProjectRemoved
    from super_worker.widgets.project_view import ProjectView

    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        path = str(pv.config.repo_root)
        pv_id = f"pv-{pv.config.state_hash}"
        app._open_configs = [pv.config]

        app.on_project_removed(ProjectRemoved(path))
        await pilot.pause(delay=0.5)

        assert not list(app.query(f"#{pv_id}")), "ProjectView must be unmounted"
        assert app._active_project_view is None
        switcher = app.query_one("#project-switcher", ContentSwitcher)
        assert switcher.current == "no-project", "placeholder must be shown"
        placeholder = app.query_one("#no-project", Static)
        assert placeholder.display, "placeholder must be visible"


@pytest.mark.asyncio
async def test_placeholder_visible_without_initial_project(monkeypatch, tmp_path):
    """With no project, the 'No project open' message actually renders.

    Regression: ContentSwitcher(initial=None) hides ALL children, so the
    placeholder text never displayed.
    """
    from textual.widgets import ContentSwitcher, Static
    import super_worker.app as app_mod

    def raise_no_repo(*a, **kw):
        raise RuntimeError("not a git repo")

    monkeypatch.setattr(app_mod, "load_config", raise_no_repo)
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        switcher = app.query_one("#project-switcher", ContentSwitcher)
        assert switcher.current == "no-project"
        assert app.query_one("#no-project", Static).display


@pytest.mark.asyncio
async def test_active_session_set_after_async_startup():
    """The default session is created off __init__ (async) yet still adopted as active.

    Regression: moving `create_session` out of ProjectView.__init__ (to avoid
    a blocking tmux call on the event loop) must not leave Ctrl+A/Ctrl+S
    without an active session right after open.
    """
    app = SuperWorkerApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(delay=1.0)
        pv = _pv(app)
        wt = pv._state.worktrees[0]
        assert wt.sessions, "default worktree should have a lazily-created session"
        assert pv._active_session_name == wt.sessions[0].tmux_session_name
        wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
        assert wtc.query_one(TerminalPane).active_session == wt.sessions[0].tmux_session_name


@pytest.mark.asyncio
async def test_delete_gone_worktree_still_closes_tab(monkeypatch):
    """A worktree deleted outside sw (git-removed) must still close its tab.

    Regression: remove_worktree used to raise for an already-gone worktree and
    the handler bailed, so the stale tab could never be closed.
    """
    import super_worker.widgets.project_view as pvmod

    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        wt = Worktree(name="feat", path="/nonexistent/gone-wt", branch="sw-feat")
        pv._state.worktrees.append(wt)
        await pv._add_worktree_tab(wt)
        await pilot.pause()
        pv._active_worktree = wt
        assert list(pv.query("#wt-feat")), "tab should exist before delete"

        # Simulate git cleanup failing exactly like an already-removed worktree.
        def raise_gone(*a, **k):
            raise RuntimeError("fatal: 'gone-wt' is not a working tree")
        monkeypatch.setattr(pvmod, "remove_worktree", raise_gone)

        # Auto-confirm the delete dialog (user presses Delete, keep branch).
        def auto_confirm(screen, callback=None):
            if callback:
                callback(False)
        monkeypatch.setattr(app, "push_screen", auto_confirm)

        pv.do_delete_worktree()
        await pilot.pause(delay=1.0)

        assert pv._state.get_worktree("feat") is None, "worktree removed from state despite git error"
        assert not list(pv.query("#wt-feat")), "tab closed despite git cleanup failing"
        assert app.is_running


# ── Workspace persistence ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workspace_state_saved_on_startup():
    """Startup persists the launch project into the workspace UI-state file."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        await pilot.pause(delay=1.0)
        repo = str(_pv(app).config.repo_root)
        ui = load_ui_state()
        assert repo in ui.open_projects
        assert repo in ui.projects


@pytest.mark.asyncio
async def test_sidebar_width_persisted_on_drag_end():
    """Releasing a divider drag writes the chosen width to the UI-state file."""
    app = SuperWorkerApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(delay=1.0)
        divider = app.query_one(SidebarDivider)
        divider.on_mouse_down(_FakeMouse(50))
        divider.on_mouse_move(_FakeMouse(50))
        await pilot.pause()
        divider.on_mouse_up(_FakeMouse(50))
        await pilot.pause()

        ui = load_ui_state()
        assert ui.sidebar_width is not None
        assert ui.sidebar_width == SidebarDivider._shared_width


@pytest.mark.asyncio
async def test_sidebar_width_restored_on_startup():
    """A persisted sidebar width is applied to the sidebar on the next launch."""
    save_ui_state(UIState(sidebar_width=40))
    app = SuperWorkerApp()
    # __init__ seeds the class-level width from the UI-state file.
    assert SidebarDivider._shared_width == 40
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(delay=1.0)
        from super_worker.widgets.sidebar import SessionSidebar
        assert app.query_one(SessionSidebar).region.width == 40


@pytest.mark.asyncio
async def test_restore_skips_nonexistent_project():
    """A previously-open project whose path is gone is skipped, not reopened."""
    save_ui_state(UIState(open_projects=["/nonexistent/repo-xyz"]))
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        await pilot.pause(delay=1.0)
        assert app.is_running
        open_paths = {str(c.repo_root) for c in app._open_configs}
        assert "/nonexistent/repo-xyz" not in open_paths
# ── Live-sync from the shared state file (F5 / periodic) ──────────────────────


@pytest.mark.asyncio
async def test_f5_syncs_new_worktree_from_disk(tmp_path):
    """A worktree written to the shared state file by another sw run shows up
    on refresh, as a new tab, WITHOUT stealing the active tab."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        await pilot.pause(delay=0.5)  # let startup's own state save settle first
        active_before = pv._active_worktree.name if pv._active_worktree else None

        # Simulate another sw process adding a worktree to the shared file.
        other_dir = tmp_path / "other-wt"
        other_dir.mkdir()
        disk = load_state(pv._config)
        disk.worktrees.append(Worktree(name="fromdisk", path=str(other_dir), branch="sw-fromdisk"))
        save_state(disk, pv._config)

        await pilot.press("f5")
        await pilot.pause(delay=1.0)

        assert pv._state.get_worktree("fromdisk") is not None
        assert list(pv.query("#wt-fromdisk")), "a tab should have been added"
        # Focus/active tab must NOT have been hijacked by the discovery.
        assert (pv._active_worktree.name if pv._active_worktree else None) == active_before


@pytest.mark.asyncio
async def test_refresh_syncs_new_session_into_existing_worktree():
    """A session added to an existing worktree's file entry appears in its sidebar."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        await pilot.pause(delay=0.5)  # let startup's own state save settle first
        main = pv._state.worktrees[0]
        before = {s.tmux_session_name for s in main.sessions}

        disk = load_state(pv._config)
        disk_main = disk.get_worktree(main.name)
        injected = Session(tmux_session_name="sw-main-injected-9", label="external-run")
        disk_main.sessions.append(injected)
        save_state(disk, pv._config)

        pv.do_refresh()
        await pilot.pause(delay=1.0)

        names = {s.tmux_session_name for s in main.sessions}
        assert "sw-main-injected-9" in names
        assert names > before


@pytest.mark.asyncio
async def test_refresh_drops_worktree_whose_path_is_gone():
    """A worktree tracked in memory but absent from the file AND whose path no
    longer exists is dropped on refresh."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        ghost = Worktree(name="ghost", path="/nonexistent/ghost-wt", branch="sw-ghost")
        pv._state.worktrees.append(ghost)
        await pv._add_worktree_tab(ghost, activate=False)
        await pilot.pause(delay=0.5)
        assert list(pv.query("#wt-ghost")), "tab exists before refresh"

        # Adding the tab persisted ghost to disk; simulate another process
        # having removed it from the shared file (its dir is already gone).
        disk = load_state(pv._config)
        disk.worktrees = [w for w in disk.worktrees if w.name != "ghost"]
        save_state(disk, pv._config)

        pv.do_refresh()
        await pilot.pause(delay=1.0)

        assert pv._state.get_worktree("ghost") is None
        assert not list(pv.query("#wt-ghost")), "gone worktree's tab was dropped"


@pytest.mark.asyncio
async def test_refresh_keeps_unpersisted_local_session():
    """Refresh must NOT drop an in-memory session that isn't in the file yet
    (it may just not be persisted — dropping it would race the write)."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        main = pv._state.worktrees[0]
        local = Session(tmux_session_name="sw-main-local-77", label="not-yet-saved")
        main.sessions.append(local)

        pv.do_refresh()
        await pilot.pause(delay=1.0)

        names = {s.tmux_session_name for s in main.sessions}
        assert "sw-main-local-77" in names


# ── Adopting foreign (non-sw) tmux sessions ───────────────────────────────────


def _inject_foreign_session(worktree_path: str, name: str = "cc-manual"):
    """Append a mock non-sw 'claude' tmux session in ``worktree_path`` to the
    shared mock server and return it."""
    import super_worker.services.tmux as tmux_mod
    server = tmux_mod._get_server()
    foreign = MagicMock()
    foreign.session_name = name
    pane = MagicMock()
    pane.pane_current_path = worktree_path
    pane.pane_current_command = "claude"
    foreign.active_pane = pane
    foreign.show_environment.return_value = {}
    server.sessions.append(foreign)
    return server, foreign


@pytest.mark.asyncio
async def test_foreign_session_surfaced_tagged_and_not_persisted():
    """A non-sw claude session in a worktree dir is surfaced, tagged [ext],
    not double-counted on re-scan, and never written to the state file."""
    from rich.text import Text
    from super_worker.widgets.sidebar import SessionSidebar
    from textual.widgets import Label, ListView

    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        await pilot.pause(delay=0.5)
        main = pv._state.worktrees[0]
        _inject_foreign_session(str(main.path))

        pv.do_refresh()
        await pilot.pause(delay=1.0)

        foreign = [s for s in main.sessions if s.foreign]
        assert len(foreign) == 1
        assert foreign[0].tmux_session_name == "cc-manual"

        # Re-scan must not double-count (existing foreign object is reused).
        pv.do_refresh()
        await pilot.pause(delay=1.0)
        assert len([s for s in main.sessions if s.foreign]) == 1

        # Sidebar renders it with the [ext] tag.
        wtc = pv.query_one(f"#wtc-{main.name}", WorktreeTabContent)
        sidebar = wtc.query_one(SessionSidebar)
        labels = []
        for lbl in sidebar.query_one("#session-list", ListView).query(Label):
            r = lbl.render()
            labels.append(r.plain if hasattr(r, "plain") else Text.from_markup(str(r)).plain)
        assert any("ext" in text for text in labels), labels

        # Foreign sessions are DISPLAY-ONLY — never persisted to the state file.
        disk = load_state(pv._config)
        disk_names = {s.tmux_session_name for w in disk.worktrees for s in w.sessions}
        assert "cc-manual" not in disk_names


@pytest.mark.asyncio
async def test_foreign_session_dropped_when_gone():
    """A foreign session is re-discovered each scan and dropped once it's gone."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        await pilot.pause(delay=0.5)
        main = pv._state.worktrees[0]
        server, foreign = _inject_foreign_session(str(main.path))

        pv.do_refresh()
        await pilot.pause(delay=1.0)
        assert any(s.foreign for s in main.sessions)

        # The foreign tmux session ends — next scan must drop it.
        server.sessions.remove(foreign)
        pv.do_refresh()
        await pilot.pause(delay=1.0)
        assert not any(s.foreign for s in main.sessions)


# ── Orphan sw-session adoption (repairs the clobber, prevents empty-on-open) ───


@pytest.mark.asyncio
async def test_sessionless_worktree_adopts_live_sw_session_not_empty():
    """A worktree that's sessionless in state but has a LIVE sw session adopts it
    on open (as a real, foreign=False session) instead of spawning an empty one."""
    import super_worker.services.tmux as tmux_mod
    from super_worker.services.tmux import _worktree_scope, tmux_session_name

    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        await pilot.pause(delay=0.5)

        wt = Worktree(name="adopt", path=str(pv._config.repo_root), branch="sw-adopt")
        live_name = tmux_session_name("adopt", 0, _worktree_scope(wt))
        server = tmux_mod._get_server()
        live = MagicMock()
        live.session_name = live_name
        live.active_pane = MagicMock()
        live.show_environment.return_value = {}
        server.sessions.append(live)

        pv._state.worktrees.append(wt)
        await pv._add_worktree_tab(wt, activate=False)
        await pilot.pause(delay=1.0)

        # Adopted the live sw session; did NOT create a redundant empty one.
        assert len(wt.sessions) == 1
        assert wt.sessions[0].tmux_session_name == live_name
        assert wt.sessions[0].foreign is False


@pytest.mark.asyncio
async def test_tui_persist_preserves_concurrently_added_session():
    """End-to-end clobber regression: a session another process added to the
    shared file survives a subsequent TUI write (merge, not overwrite)."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        pv = _pv(app)
        await pilot.pause(delay=0.5)
        main = pv._state.worktrees[0]

        # Another process appends session S to main in the shared file; the TUI's
        # in-memory state does NOT know about S.
        disk = load_state(pv._config)
        disk.get_worktree(main.name).sessions.append(
            Session(tmux_session_name="sw-main-concurrent-0", label="S")
        )
        save_state(disk, pv._config)
        assert all(s.tmux_session_name != "sw-main-concurrent-0" for s in main.sessions)

        # The TUI now persists a change of its own (rename its existing session).
        renamed = main.sessions[0]
        from super_worker.services.state import update_session_label_in_state_file
        await asyncio.to_thread(
            update_session_label_in_state_file, pv._config, main.name, renamed.id, "renamed-by-tui"
        )

        after = load_state(pv._config).get_worktree(main.name)
        names = {s.tmux_session_name for s in after.sessions}
        assert "sw-main-concurrent-0" in names, "concurrent session S must not be clobbered"
        assert any(s.id == renamed.id and s.label == "renamed-by-tui" for s in after.sessions)
