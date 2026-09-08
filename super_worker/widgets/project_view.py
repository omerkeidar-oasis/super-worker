"""Per-project widget: worktree tabs, session management, git actions."""

import asyncio
import logging
import shlex
import subprocess
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static, TabPane, TabbedContent

from super_worker.config import ResolvedConfig, SWConfig, load_config, save_project_config
from super_worker.constants import DEFAULT_WORKTREE_NAME
from super_worker.models import AppState, Session, Worktree
from super_worker.screens import (
    BranchExistsScreen,
    CommitMessageScreen,
    ConfigScreen,
    ConfirmDeleteScreen,
    NewSessionScreen,
    NewWorktreeScreen,
    RenameSessionScreen,
)
from super_worker.services.state import (
    add_sessions_to_state_file,
    add_worktree_to_state_file,
    adopt_orphan_sw_sessions,
    adopt_orphans_for_worktree,
    ensure_default_worktree,
    load_state,
    remove_session_from_state,
    remove_session_from_state_file,
    remove_worktree_from_state,
    remove_worktree_from_state_file,
    update_session_label_in_state_file,
)
from super_worker.services.tmux import (
    SessionState,
    batch_check_alive,
    batch_detect_session_states,
    cleanup_state_file,
    create_session,
    enable_mouse,
    has_waiting_approval,
    is_session_alive,
    kill_all_sessions,
    kill_session,
    list_foreign_claude_sessions,
    open_external_terminal,
    read_all_state_files,
    read_state_file,
    set_window_size,
    verify_waiting_approval,
)
from super_worker.services.worktree import (
    BranchExistsError,
    create_worktree,
    get_branch_status,
    get_worktree_dirty,
    git_commit,
    git_create_pr,
    git_pull,
    git_push,
    invalidate_git_cache,
    remove_worktree,
)
from super_worker.widgets.sidebar import (
    GitAction,
    SessionDeleted,
    SessionSelected,
    SessionSidebar,
    SidebarDivider,
)
from super_worker.widgets.terminal_pane import TerminalPane

logger = logging.getLogger(__name__)


class WorktreeTabContent(Horizontal):
    """Sidebar + terminal for a single worktree tab."""

    class Initialized(Message):
        """Posted when a worktree tab finishes wiring up its first session.

        Carries the first session's name so ProjectView can adopt it as active
        (the session is created asynchronously here, after ProjectView.on_mount
        has already run), and ``new_session_names`` — the tmux names of sessions
        this init CREATED or ADOPTED — so ProjectView can merge-persist exactly
        those into the shared state file (never a blind whole-state overwrite).
        """

        def __init__(
            self, worktree_name: str, first_session_name: str | None,
            new_session_names: list[str],
        ) -> None:
            self.worktree_name = worktree_name
            self.first_session_name = first_session_name
            self.new_session_names = new_session_names
            super().__init__()

    DEFAULT_CSS = """
    WorktreeTabContent {
        height: 1fr;
        width: 1fr;
    }
    """

    def __init__(self, worktree: Worktree, remote: str = "origin", main_branch: str = "main") -> None:
        super().__init__(id=f"wtc-{worktree.name}")
        self.worktree = worktree
        self._remote = remote
        self._main_branch = main_branch

    def compose(self) -> ComposeResult:
        yield SessionSidebar(remote=self._remote, main_branch=self._main_branch)
        yield SidebarDivider()
        yield TerminalPane()

    def on_mount(self) -> None:
        async def _init_sidebar() -> None:
            new_session_names: list[str] = []
            if not self.worktree.sessions:
                # First, re-adopt any LIVE sw session already running for this
                # worktree (e.g. an `sw new`/`sw add` whose state was clobbered)
                # so we don't spawn a redundant EMPTY session beside the real one.
                adopted = await asyncio.to_thread(adopt_orphans_for_worktree, self.worktree)
                new_session_names.extend(s.tmux_session_name for s in adopted)
            if not self.worktree.sessions:
                session = await asyncio.to_thread(create_session, self.worktree)
                self.worktree.sessions.append(session)
                new_session_names.append(session.tmux_session_name)

            session_names = [s.tmux_session_name for s in self.worktree.sessions]
            states = await asyncio.to_thread(batch_detect_session_states, session_names) if session_names else {}
            status = await asyncio.to_thread(get_branch_status, self.worktree.path, self._remote, self._main_branch)
            dirty = await asyncio.to_thread(get_worktree_dirty, self.worktree.path)
            sidebar = self.query_one(SessionSidebar)
            sidebar.show_worktree(self.worktree, states=states, git_status=status, git_dirty=dirty)

            first_name = None
            if self.worktree.sessions:
                first = self.worktree.sessions[0]
                first_name = first.tmux_session_name
                terminal = self.query_one(TerminalPane)
                terminal.active_session = first_name

            # Let ProjectView (which owns state+config) adopt the active
            # session and merge-persist any newly created/adopted sessions.
            self.post_message(self.Initialized(self.worktree.name, first_name, new_session_names))

        # Widget-node worker: auto-cancelled if this tab unmounts mid-init,
        # and immune to exclusive workers on the App node.
        self.run_worker(_init_sidebar, exclusive=False)


class ProjectView(Widget):
    """Self-contained per-project widget: worktree tabs + session/git management."""

    class AttentionChanged(Message):
        """Posted when the project's attention state (any session waiting approval) changes."""

        def __init__(self, path: str, needs_attention: bool) -> None:
            self.path = path
            self.needs_attention = needs_attention
            super().__init__()

    DEFAULT_CSS = """
    ProjectView {
        width: 1fr;
        height: 1fr;
    }
    TabbedContent {
        height: 1fr;
    }
    #empty-state {
        width: 100%;
        height: 100%;
        content-align: center middle;
        text-style: italic;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        config: ResolvedConfig,
        state: AppState,
        restore_worktree: str | None = None,
        restore_session: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._config = config
        self._state = state
        self._active_worktree: Worktree | None = None
        self._active_session_name: str | None = None
        self._cached_session_states: dict[str, SessionState] = {}
        # One-shot workspace-restore hints: the worktree tab + session that were
        # active last run. Consumed once when that worktree is first activated.
        self._restore_worktree = restore_worktree
        self._restore_session = restore_session
        self._refresh_tick = 0  # drives every-other-tick foreign-session scans
        # Only ensure the "main" worktree EXISTS in state (cheap, needed before
        # compose builds the tabs). Its tmux session is created lazily and
        # off the event loop by WorktreeTabContent.on_mount — creating it here
        # ran a blocking `tmux new-session` + state write during __init__,
        # janking project open.
        ensure_default_worktree(self._state, self._config)

    @property
    def config(self) -> ResolvedConfig:
        return self._config

    @property
    def state(self) -> AppState:
        return self._state

    @property
    def active_worktree_name(self) -> str | None:
        return self._active_worktree.name if self._active_worktree else None

    @property
    def active_session_name(self) -> str | None:
        return self._active_session_name

    def _save_workspace(self) -> None:
        """Ask the app to persist the workspace UI-state (best-effort)."""
        save = getattr(self.app, "save_workspace_state", None)
        if save is not None:
            try:
                save()
            except Exception:
                logger.debug("save_workspace_state failed", exc_info=True)

    def compose(self) -> ComposeResult:
        if self._state.worktrees:
            # Open directly on the last-active worktree tab (workspace restore) so
            # the framework never activates the first tab and then clobbers it —
            # `initial` is the tab shown on mount. Falls back to the first tab.
            initial = ""
            if self._restore_worktree and self._state.get_worktree(self._restore_worktree):
                initial = f"wt-{self._restore_worktree}"
            with TabbedContent(id="tabs", initial=initial):
                for wt in self._state.worktrees:
                    with TabPane(self._tab_label(wt), id=f"wt-{wt.name}"):
                        yield WorktreeTabContent(wt, self._config.remote, self._config.main_branch)
        else:
            yield Static("No worktrees. Press Ctrl+N to create one.", id="empty-state")

    def on_mount(self) -> None:
        if self._state.worktrees:
            # Restore the last-active worktree tab if it still exists; else the first.
            target = (
                self._state.get_worktree(self._restore_worktree)
                if self._restore_worktree else None
            )
            wt = target or self._state.worktrees[0]
            self._active_worktree = wt
            # Drop a restore-session hint that no longer exists in the worktree.
            if self._restore_session and not any(
                s.tmux_session_name == self._restore_session for s in wt.sessions
            ):
                self._restore_session = None
            if wt.sessions:
                self._active_session_name = self._restore_session or wt.sessions[0].tmux_session_name

            async def _initial_refresh():
                await self._refresh_sidebar(wt)
                # TabActivated for the initial tab also runs _set_active_worktree
                # (consuming the restore session); calling it here too is safe and
                # covers the no-restore case. preserve-current-session avoids clobber.
                self._set_active_worktree(wt, session_name=self._consume_restore_session(wt))
                self._start_state_watching()

            self.run_worker(_initial_refresh, exclusive=False)

    def _consume_restore_session(self, wt: Worktree) -> str | None:
        """Return the one-shot restore session for ``wt`` (if any), then clear it."""
        if self._restore_worktree == wt.name and self._restore_session:
            session = self._restore_session
            self._restore_worktree = None
            self._restore_session = None
            return session
        return None

    def _tab_label(self, wt: Worktree, git_data: tuple[dict, bool] | None = None) -> str:
        wt_states = {s.tmux_session_name: self._cached_session_states.get(s.tmux_session_name, SessionState.RUNNING) for s in wt.sessions}
        attention = " 🔔" if has_waiting_approval(wt_states) else ""
        if git_data is None:
            return f"{wt.name}{attention}"
        status, dirty = git_data
        dirty_marker = " *" if dirty else ""
        return f"{wt.name} (↑{status['ahead']} ↓{status['behind']}){dirty_marker}{attention}"

    def _update_app_subtitle(self, session_label: str | None = None) -> None:
        """Update the app subtitle to include the active session label."""
        try:
            base = str(self._config.repo_root)
            if session_label:
                self.app.sub_title = f"{base} · {session_label}"
            else:
                self.app.sub_title = base
        except Exception:
            pass

    def _start_state_watching(self) -> None:
        """Start kqueue watches on state files for ALL sessions in this project.

        Called once on mount and whenever sessions change. The active worktree's
        TerminalPane hosts the watchers for all sessions across all worktrees,
        so attention indicators update instantly for the entire project.
        """
        # Foreign sessions have no sw state file (no hook), so there's nothing
        # to kqueue-watch for them — exclude them from the watch list.
        all_names = [
            s.tmux_session_name
            for wt in self._state.worktrees
            for s in wt.sessions
            if not s.foreign
        ]
        if not all_names or not self._active_worktree:
            return
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            terminal.start_watching_states(all_names)
        except Exception:
            pass

    def pause_watching(self) -> None:
        """Pause terminal capture when this project becomes inactive.

        The kqueue state-file watchers keep running so attention indicators
        (bell icon) still update for background projects.
        """
        if not self._active_worktree:
            return
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            wtc.query_one(TerminalPane).pause_watching()
        except Exception:
            pass

    def resume_watching(self) -> None:
        """Resume terminal capture when this project becomes active again."""
        if not self._active_worktree:
            return
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            wtc.query_one(TerminalPane).resume_watching()
        except Exception:
            pass

    def focus_terminal(self) -> None:
        """Focus the active worktree's terminal pane.

        Called when this project becomes active (or the drawer closes) so
        keystrokes reach the visible session — without it, focus can stay on
        a hidden widget and input is silently misrouted.
        """
        if not self._active_worktree:
            return
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            wtc.query_one(TerminalPane).focus()
        except Exception:
            pass

    def _set_active_worktree(self, wt: Worktree, session_name: str | None = None) -> None:
        # Pause the old worktree's terminal captures (keeps content + state watches)
        old_wt = self._active_worktree
        if old_wt and old_wt.name != wt.name:
            try:
                old_wtc = self.query_one(f"#wtc-{old_wt.name}", WorktreeTabContent)
                old_wtc.query_one(TerminalPane).pause_watching()
            except Exception:
                pass
        self._active_worktree = wt
        if wt.sessions:
            # Prefer an explicitly requested session (workspace restore); else keep
            # the currently-active one if it lives in this worktree (so a redundant
            # re-activation doesn't reset the selection); else fall back to the first.
            chosen = None
            if session_name:
                chosen = next((s for s in wt.sessions if s.tmux_session_name == session_name), None)
            if chosen is None and self._active_session_name:
                chosen = next((s for s in wt.sessions if s.tmux_session_name == self._active_session_name), None)
            first = chosen or wt.sessions[0]
            self._active_session_name = first.tmux_session_name
            self._activate_terminal(wt.name, first.tmux_session_name)
            self._update_app_subtitle(first.label)

    def _activate_terminal(self, wt_name: str, tmux_session_name: str) -> None:
        """Set the active session on a worktree's terminal pane.

        Uses call_after_refresh so it works even when the widget tree
        hasn't fully composed yet (e.g. initial mount race).
        """
        def _do_activate() -> None:
            try:
                wtc = self.query_one(f"#wtc-{wt_name}", WorktreeTabContent)
                wtc.query_one(SessionSidebar).select_session(tmux_session_name)
                terminal = wtc.query_one(TerminalPane)
                terminal.active_session = tmux_session_name
                terminal.resume_watching()
                terminal.focus()
            except Exception:
                pass
        self.call_after_refresh(_do_activate)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = event.pane.id
        if tab_id and tab_id.startswith("wt-"):
            name = tab_id[3:]
            wt = self._state.get_worktree(name)
            if wt:
                self._set_active_worktree(wt, session_name=self._consume_restore_session(wt))
                self._save_workspace()

    def on_worktree_tab_content_initialized(
        self, event: "WorktreeTabContent.Initialized"
    ) -> None:
        """A worktree tab finished init — adopt its session and persist if new."""
        event.stop()
        # Adopt the session as active if the active worktree still has none
        # (its session was created async, after on_mount ran) — without this
        # Ctrl+A/Ctrl+S right after open would wrongly report no active session.
        if (
            self._active_worktree
            and self._active_worktree.name == event.worktree_name
            and not self._active_session_name
            and event.first_session_name
        ):
            self._active_session_name = event.first_session_name
            self._update_app_subtitle()
        if event.new_session_names:
            # Merge-persist this tab's worktree + the sessions it created/adopted
            # — never a blind whole-state save, which would clobber another sw
            # run's work. add_worktree_to_state_file adds the worktree if it's
            # not in the file yet (the auto-created "main" worktree lives only in
            # memory until now) and otherwise merges only the new sessions. The
            # file lock serializes concurrent tab inits, so no exclusivity group
            # is needed (and none is wanted — it would cancel a pending persist).
            wt = self._state.get_worktree(event.worktree_name)
            if wt is not None:
                self.run_worker(
                    lambda w=wt: add_worktree_to_state_file(self._config, w),
                    thread=True, group="persist-state",
                )
        self._start_state_watching()

    def on_terminal_pane_state_changed(self, event: TerminalPane.StateChanged) -> None:
        """Session state changed (via kqueue on state file) — update UI instantly."""
        name = event.session_name
        new_state = read_state_file(name)
        old_state = self._cached_session_states.get(name)
        if new_state == old_state:
            return
        # Scope attention check to current sessions only — stale cache entries
        # for deleted sessions could keep the bell icon on indefinitely.
        current_names = {s.tmux_session_name for wt in self._state.worktrees for s in wt.sessions}
        current_states = {k: v for k, v in self._cached_session_states.items() if k in current_names}
        old_attention = has_waiting_approval(current_states)
        self._cached_session_states[name] = new_state
        current_states[name] = new_state
        new_attention = has_waiting_approval(current_states)
        if old_attention != new_attention:
            self.post_message(self.AttentionChanged(
                str(self._config.repo_root), new_attention
            ))
        for wt in self._state.worktrees:
            self._refresh_tab_label(wt, git_data=None)
        wt = self._active_worktree
        if wt:
            try:
                wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
                sidebar = wtc.query_one(SessionSidebar)
                # State-change events are frequent (kqueue) — don't run git
                # subprocesses on the event loop here; periodic_refresh owns
                # git status. This only repaints the session dots.
                sidebar.show_worktree(wt, states=self._cached_session_states, refresh_git=False)
            except Exception:
                pass

    def on_session_selected(self, event: SessionSelected) -> None:
        self._active_worktree = event.worktree
        self._active_session_name = event.session.tmux_session_name
        try:
            wtc = self.query_one(f"#wtc-{event.worktree.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            terminal.active_session = event.session.tmux_session_name
            terminal.focus()
        except Exception:
            logger.debug("Failed to activate session in terminal pane", exc_info=True)
        self._update_app_subtitle(event.session.label)
        self._save_workspace()

    async def on_session_deleted(self, event: SessionDeleted) -> None:
        wt = event.worktree
        session = event.session
        tmux_name = session.tmux_session_name

        try:
            wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            if terminal.active_session == tmux_name:
                terminal.active_session = None
        except Exception:
            pass
        if self._active_session_name == tmux_name:
            self._active_session_name = None

        self._state = remove_session_from_state(self._state, wt.name, session.id)
        try:
            wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            sidebar = wtc.query_one(SessionSidebar)
            sidebar._prev_session_snapshot = "__deleted__"
            sidebar.show_worktree(wt, states={}, git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        except Exception:
            pass

        if wt.sessions:
            next_session = wt.sessions[0]
            self._active_session_name = next_session.tmux_session_name
            try:
                wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
                wtc.query_one(TerminalPane).active_session = next_session.tmux_session_name
            except Exception:
                pass
            self._update_app_subtitle(next_session.label)
        else:
            self._update_app_subtitle()

        self.app.notify(f"Deleted session: {session.label}")
        if not session.foreign:
            # Foreign (external) sessions are display-only — sw never kills them
            # and they were never persisted; removing from the in-memory view is
            # enough (the next scan re-discovers them while they're still live).
            await asyncio.to_thread(kill_session, tmux_name)
            cleanup_state_file(tmux_name)
            # Merge-remove only THIS session from the shared file.
            await asyncio.to_thread(
                remove_session_from_state_file, self._config, wt.name, session.id
            )
        self._start_state_watching()  # Update watch list
        self._save_workspace()

    def on_git_action(self, event: GitAction) -> None:
        wt = event.worktree
        if event.action == "commit":
            self._git_commit(wt)
        elif event.action == "push":
            self._git_push(wt)
        elif event.action == "pull":
            self._git_pull(wt)
        elif event.action == "pr":
            self._git_create_pr(wt)

    # ── Public delegation API ─────────────────────────────────────────────────

    def do_new_worktree(self) -> None:
        def handle_result(result: tuple[str, str | None, str | None, bool, bool, bool] | None) -> None:
            if result is None:
                return
            name, branch, prompt, detach, skip_perms, use_existing = result
            if self._state.get_worktree(name):
                self.app.notify(f"Worktree '{name}' already exists", severity="error")
                return
            self._create_worktree(name, prompt, branch=branch, use_existing_branch=use_existing, detach=detach, skip_permissions=skip_perms)

        # No synchronous git here — NewWorktreeScreen doesn't need a branch list.
        self.app.push_screen(NewWorktreeScreen(self._config), callback=handle_result)

    def _create_worktree(
        self,
        name: str,
        prompt: str | None,
        branch: str | None = None,
        use_existing_branch: bool = False,
        detach: bool = False,
        skip_permissions: bool = False,
    ) -> None:
        async def _create() -> None:
            try:
                wt = await asyncio.to_thread(
                    create_worktree, self._config, name,
                    branch=branch, use_existing_branch=use_existing_branch, detach=detach,
                    worktree_index=len(self._state.worktrees),
                )
            except BranchExistsError as e:
                def handle_branch(choice: str) -> None:
                    if choice == "use":
                        self._create_worktree(name, prompt, branch=branch, use_existing_branch=True, detach=detach, skip_permissions=skip_permissions)
                self.app.push_screen(BranchExistsScreen(e.branch), callback=handle_branch)
                return
            except Exception as e:
                self.app.notify(str(e), severity="error")
                return

            self._state.worktrees.append(wt)
            if prompt or skip_permissions:
                session = await asyncio.to_thread(
                    create_session, wt, prompt=prompt, label=prompt,
                    skip_permissions=skip_permissions,
                )
                wt.sessions.append(session)
            # Merge-add just this worktree (+ its session) into the shared file.
            await asyncio.to_thread(add_worktree_to_state_file, self._config, wt)
            await self._add_worktree_tab(wt)
            self._start_state_watching()  # Watch new worktree's sessions
            self.app.notify(f"Created worktree: {name}")

        self.run_worker(_create, exclusive=False)

    async def _add_worktree_tab(self, wt: Worktree, activate: bool = True) -> None:
        """Add a tab for ``wt``.

        With ``activate=False`` the tab is added WITHOUT stealing focus or
        changing the current tab — used when the worktree was discovered from
        the shared state file (created by another sw run), where forcing focus
        would yank the user off whatever they were doing.
        """
        try:
            empty = self.query_one("#empty-state", Static)
            await empty.remove()
            tabs = TabbedContent(id="tabs")
            await self.mount(tabs)
        except Exception:
            tabs = self.query_one("#tabs", TabbedContent)

        pane = TabPane(self._tab_label(wt), id=f"wt-{wt.name}")
        pane.compose_add_child(WorktreeTabContent(wt, self._config.remote, self._config.main_branch))
        await tabs.add_pane(pane)
        if activate:
            tabs.active = f"wt-{wt.name}"
            self._set_active_worktree(wt)

    def do_new_session(self) -> None:
        if not self._active_worktree:
            self.app.notify("Select a worktree first", severity="warning")
            return
        wt = self._active_worktree

        def handle_result(result: tuple[str, str | None, str | None, bool] | None) -> None:
            if result is None:
                return
            session_type, prompt, label, skip_perms = result

            async def _create_session() -> None:
                try:
                    session = await asyncio.to_thread(
                        create_session, wt, prompt=prompt, label=label,
                        skip_permissions=skip_perms, session_type=session_type,
                    )
                    wt.sessions.append(session)
                    # Merge-add just this new session into the shared file.
                    await asyncio.to_thread(
                        add_sessions_to_state_file, self._config, wt.name, [session]
                    )
                except Exception as e:
                    self.app.notify(str(e), severity="error")
                    return

                self._active_session_name = session.tmux_session_name
                await self._refresh_sidebar(wt)
                self._activate_terminal(wt.name, session.tmux_session_name)
                self._start_state_watching()  # Watch new session's state file
                self._save_workspace()
                self.app.notify(f"Created session: {session.label}")

            self.run_worker(_create_session, exclusive=False)

        self.app.push_screen(NewSessionScreen(), callback=handle_result)

    def do_rename_session(self) -> None:
        if not self._active_worktree or not self._active_session_name:
            self.app.notify("No active session to rename", severity="warning")
            return
        wt = self._active_worktree
        session = next((s for s in wt.sessions if s.tmux_session_name == self._active_session_name), None)
        if not session:
            return

        def handle_rename(new_label: str | None) -> None:
            if not new_label:
                return
            session.label = new_label

            async def _save_and_refresh() -> None:
                # Merge-update only this session's label in the shared file.
                await asyncio.to_thread(
                    update_session_label_in_state_file,
                    self._config, wt.name, session.id, new_label,
                )
                await self._refresh_sidebar(wt)
                self.app.notify(f"Renamed session to: {new_label}")

            self.run_worker(_save_and_refresh, exclusive=False)

        self.app.push_screen(RenameSessionScreen(session.label), callback=handle_rename)

    def do_full_attach(self) -> None:
        if not self._active_worktree or not self._active_session_name:
            self.app.notify("No active session to attach", severity="warning")
            return
        session_name = self._active_session_name
        # Don't suspend the whole TUI to attach to a session that isn't
        # running — the user would just see tmux flash "no such session".
        if not is_session_alive(session_name):
            self.app.notify("Session is not running (dead pane).", severity="warning")
            return
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            terminal.active_session = None
        except Exception:
            logger.debug("Failed to pause terminal before attach", exc_info=True)
        enable_mouse(session_name)
        # Let the real attach fill the attaching terminal. When the preview
        # resumes (active_session set below), it flips back to manual sizing.
        set_window_size(session_name, "latest")
        with self.app.suspend():
            q = shlex.quote(session_name)
            subprocess.run([
                "bash", "-c",
                "printf '\\e[?1000l\\e[?1003l\\e[?1015l\\e[?1006l' && "
                f"tmux attach-session -t {q}",
            ])
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            terminal.active_session = session_name
        except Exception:
            logger.debug("Failed to resume terminal after attach", exc_info=True)
        # Refresh sidebar so session list and selection are restored after suspend
        wt = self._active_worktree
        if wt:
            self.run_worker(self._refresh_sidebar(wt), exclusive=False)

    def do_open_terminal(self) -> None:
        if not self._active_session_name:
            self.app.notify("No active session to open", severity="warning")
            return
        session_name = self._active_session_name

        async def _open() -> None:
            await asyncio.to_thread(enable_mouse, session_name)
            # Let the external terminal window dictate the session size —
            # otherwise it stays pinned at the preview's manual size.
            await asyncio.to_thread(set_window_size, session_name, "latest")
            opened = await asyncio.to_thread(open_external_terminal, session_name)
            if not opened:
                self.app.notify("No terminal emulator found. Use Ctrl+A to attach.", severity="warning")

        self.run_worker(_open, exclusive=False)

    def do_edit_settings(self) -> None:
        def handle_config(result: SWConfig | None) -> None:
            if result is None:
                return
            save_project_config(self._config.repo_root, result)
            self._config = load_config(self._config.repo_root)
            self.app.notify("Settings saved. Some changes take effect on next worktree creation.")

        self.app.push_screen(ConfigScreen(self._config), callback=handle_config)

    def do_delete_worktree(self) -> None:
        if not self._active_worktree:
            self.app.notify("No worktree selected", severity="warning")
            return
        wt = self._active_worktree
        if wt.name == DEFAULT_WORKTREE_NAME:
            self.app.notify("Cannot delete the main worktree", severity="warning")
            return

        wt_name = wt.name

        def handle_confirm(del_branch: bool | None) -> None:
            if del_branch is None:
                return

            async def _delete() -> None:
                target = self._state.get_worktree(wt_name)
                if not target:
                    return
                await asyncio.to_thread(kill_all_sessions, target)
                # Git cleanup is best-effort: a worktree deleted outside Super
                # Worker (merged + pruned) can't be git-removed, but the user
                # must still be able to close the stale tab. So NEVER let a git
                # error block removal from state / closing the tab.
                git_err = None
                try:
                    await asyncio.to_thread(
                        remove_worktree, self._state, wt_name,
                        force=True, delete_branch=del_branch,
                        remote=self._config.remote,
                    )
                except Exception as e:
                    git_err = str(e)
                    logger.debug("git cleanup failed removing worktree %s", wt_name, exc_info=True)

                self._state = remove_worktree_from_state(self._state, wt_name)
                # Merge-remove just this worktree from the shared file.
                await asyncio.to_thread(remove_worktree_from_state_file, self._config, wt_name)
                self._active_worktree = None
                self._active_session_name = None
                await self._remove_worktree_tab(wt.name)
                self._save_workspace()
                if git_err:
                    self.app.notify(
                        f"Removed '{wt.name}' from Super Worker (git cleanup skipped: {git_err[:80]})",
                        severity="warning",
                    )
                else:
                    self.app.notify(f"Deleted worktree: {wt.name}")

            self.run_worker(_delete, exclusive=False)

        self.app.push_screen(ConfirmDeleteScreen(wt.name, wt.branch), callback=handle_confirm)

    async def _remove_worktree_tab(self, name: str) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            await tabs.remove_pane(f"wt-{name}")
            if not self._state.worktrees:
                await tabs.remove()
                await self.mount(Static("No worktrees. Press Ctrl+N to create one.", id="empty-state"))
            else:
                active_tab = tabs.active
                if active_tab and active_tab.startswith("wt-"):
                    wt_name = active_tab[3:]
                    wt = self._state.get_worktree(wt_name)
                    if wt:
                        self._set_active_worktree(wt)
                        return
                self._set_active_worktree(self._state.worktrees[0])
        except Exception:
            logger.debug("Failed to remove worktree tab", exc_info=True, extra={"worktree": name})

    # ── Periodic refresh ──────────────────────────────────────────────────────

    async def check_attention(self) -> None:
        """Crash-proof wrapper: this runs in a 5s worker with exit_on_error=True."""
        try:
            await self._check_attention_impl()
        except Exception:
            logger.debug("check_attention failed", exc_info=True)

    async def _check_attention_impl(self) -> None:
        """Lightweight state-only check for non-active projects.

        Reads state files (no subprocess calls) for instant attention detection.
        """
        old_attention = has_waiting_approval(self._cached_session_states)
        all_session_names = [s.tmux_session_name for wt in self._state.worktrees for s in wt.sessions]
        if all_session_names:
            self._cached_session_states = read_all_state_files(all_session_names)
        else:
            self._cached_session_states = {}
        new_attention = has_waiting_approval(self._cached_session_states)
        if old_attention != new_attention:
            self.post_message(self.AttentionChanged(
                str(self._config.repo_root), new_attention
            ))

    async def periodic_refresh(self) -> None:
        """Crash-proof wrapper: this runs in a 5s worker with exit_on_error=True."""
        try:
            await self._periodic_refresh_impl()
        except Exception:
            logger.debug("periodic_refresh failed", exc_info=True)

    async def _periodic_refresh_impl(self) -> None:
        """Fetch git data and detect dead sessions. Called by app timer.

        State detection is event-driven via kqueue on state files (see
        on_terminal_pane_state_changed). This method only handles:
        - Live-sync of worktrees/sessions from the shared state file (other sw runs)
        - Git status (no event source, must poll)
        - Dead session detection (lightweight alive check, no show_environment)
        - Syncing state cache from state files for sessions without kqueue watches
        """
        # Merge anything created by another sw run into the live state first, so
        # the git/session passes below see the freshly-added worktrees/sessions
        # in this same tick.
        await self._sync_from_disk()

        # Foreign-session discovery is a few extra tmux calls — run it every
        # OTHER tick to keep the common path light. (do_refresh runs it eagerly.)
        self._refresh_tick += 1
        if self._refresh_tick % 2 == 0:
            await self._discover_foreign_sessions()

        all_session_names = [s.tmux_session_name for wt in self._state.worktrees for s in wt.sessions]
        if all_session_names:
            # Lightweight: single list-sessions + pane_dead check (no show_environment)
            dead_names = await asyncio.to_thread(batch_check_alive, all_session_names)
            # Read state from files (no subprocess) for live sessions
            file_states = read_all_state_files(all_session_names)

            old_attention = has_waiting_approval(self._cached_session_states)
            # Rebuild cache from current sessions only — prune stale entries
            # for deleted sessions whose last state might have been waiting_approval.
            new_cache: dict[str, SessionState] = {}
            for name in all_session_names:
                if name in dead_names:
                    new_cache[name] = SessionState.DEAD
                else:
                    new_cache[name] = file_states.get(name, SessionState.UNKNOWN)
            self._cached_session_states = new_cache

            # Cross-check sessions showing waiting_approval — state files can
            # be stale for sessions started before the latest hook was installed.
            suspect = [n for n, s in new_cache.items() if s == SessionState.WAITING_APPROVAL]
            if suspect:
                corrections = await asyncio.to_thread(verify_waiting_approval, suspect)
                for name, real_state in corrections.items():
                    self._cached_session_states[name] = real_state

            new_attention = has_waiting_approval(self._cached_session_states)
            if old_attention != new_attention:
                self.post_message(self.AttentionChanged(
                    str(self._config.repo_root), new_attention
                ))

        git_data: dict[str, tuple[dict, bool]] = {}
        if self._state.worktrees:
            tasks = []
            for wt in self._state.worktrees:
                tasks.append(asyncio.to_thread(get_branch_status, wt.path, self._config.remote, self._config.main_branch))
                tasks.append(asyncio.to_thread(get_worktree_dirty, wt.path))
            results = await asyncio.gather(*tasks)
            for i, wt in enumerate(self._state.worktrees):
                git_data[wt.name] = (results[i * 2], results[i * 2 + 1])

        if self._active_worktree:
            try:
                wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
                sidebar = wtc.query_one(SessionSidebar)
                gd = git_data.get(self._active_worktree.name)
                sidebar.show_worktree(
                    self._active_worktree,
                    states=self._cached_session_states,
                    git_status=gd[0] if gd else None,
                    git_dirty=gd[1] if gd else None,
                )
            except Exception:
                logger.debug("Failed to refresh active worktree sidebar", exc_info=True)

        for wt in self._state.worktrees:
            self._refresh_tab_label(wt, git_data=git_data.get(wt.name))

    # ── Live sync from the shared state file ──────────────────────────────────

    async def _sync_from_disk(self) -> dict[str, int]:
        """Merge worktrees/sessions from the shared state file into live state.

        The persisted state file is the source of truth shared across every sw
        process, but the running TUI only read it at startup — so a worktree or
        session created by another ``sw`` run never appeared until reopen. This
        reloads it (off the event loop) and merges ADDITIVELY:

        - a worktree in the file we don't track → append it + add a tab WITHOUT
          stealing focus (and start watching its sessions);
        - a session in a worktree we DO track, not in memory by tmux name →
          append it so the sidebar shows it;
        - a worktree we track that's gone from the file AND whose path no longer
          exists → drop its tab.

        In-memory sessions absent from the file are deliberately KEPT: a session
        we just created may not be persisted yet, and dropping it here would race
        that write. Dead sessions are handled elsewhere (recovery / dead check).
        """
        summary = {"worktrees_added": 0, "sessions_added": 0, "worktrees_removed": 0}
        try:
            disk = await asyncio.to_thread(load_state, self._config)
        except Exception:
            logger.debug("Failed to reload shared state for live-sync", exc_info=True)
            return summary

        disk_by_name = {wt.name: wt for wt in disk.worktrees}
        mem_names = {wt.name for wt in self._state.worktrees}
        changed = False

        # NEW worktrees on disk → append + add a tab without changing focus.
        for name, dwt in disk_by_name.items():
            if name not in mem_names:
                self._state.worktrees.append(dwt)
                await self._add_worktree_tab(dwt, activate=False)
                summary["worktrees_added"] += 1
                changed = True

        # NEW sessions inside worktrees we already track.
        for mwt in self._state.worktrees:
            dwt = disk_by_name.get(mwt.name)
            if dwt is None:
                continue
            known = {s.tmux_session_name for s in mwt.sessions}
            for s in dwt.sessions:
                if s.tmux_session_name not in known:
                    mwt.sessions.append(s)
                    known.add(s.tmux_session_name)
                    summary["sessions_added"] += 1
                    changed = True

        # REMOVED worktrees: tracked, absent from the file, AND path is gone.
        for mwt in list(self._state.worktrees):
            if mwt.name in disk_by_name:
                continue
            if await asyncio.to_thread(lambda p=mwt.path: Path(p).exists()):
                continue  # not in the file yet, but still on disk — keep it
            if self._active_worktree and self._active_worktree.name == mwt.name:
                self._active_worktree = None
                self._active_session_name = None
            self._state.worktrees = [w for w in self._state.worktrees if w.name != mwt.name]
            await self._remove_worktree_tab(mwt.name)
            summary["worktrees_removed"] += 1
            changed = True

        # Re-adopt live sw sessions that fell out of the file (crash / past
        # clobber) as REAL sessions, and merge-persist them so they're durable
        # and recoverable. Runs AFTER the disk merge above, so anything already
        # in the file is tracked and won't be re-adopted.
        adopted = await asyncio.to_thread(adopt_orphan_sw_sessions, self._state)
        if adopted:
            summary["sessions_added"] += len(adopted)
            changed = True
            by_wt: dict[str, list] = {}
            for wt_name, sess in adopted:
                by_wt.setdefault(wt_name, []).append(sess)
            for wt_name, sessions in by_wt.items():
                await asyncio.to_thread(
                    add_sessions_to_state_file, self._config, wt_name, sessions
                )

        if changed:
            self._start_state_watching()
        return summary

    async def _discover_foreign_sessions(self) -> int:
        """Surface live non-sw tmux 'claude' sessions running in our worktree dirs.

        These are DISPLAY-ONLY: shown in the sidebar tagged ``[ext]`` and
        previewable, but never persisted, resumed, or killed by sw. Existing
        foreign Session objects are REUSED across scans (so a stable one keeps
        its id and doesn't churn the sidebar); any that vanished from tmux are
        dropped, and newly-seen ones are appended. Returns the count added.
        """
        if not self._state.worktrees:
            return 0
        wt_paths = {wt.path for wt in self._state.worktrees}
        # Never re-adopt sw's own sessions; foreign names are excluded from
        # ``known`` so they CAN be re-discovered each scan.
        known = {
            s.tmux_session_name
            for wt in self._state.worktrees
            for s in wt.sessions
            if not s.foreign
        }
        try:
            found = await asyncio.to_thread(list_foreign_claude_sessions, wt_paths, known)
        except Exception:
            logger.debug("Foreign session discovery failed", exc_info=True)
            return 0

        added = 0
        for wt in self._state.worktrees:
            names = found.get(wt.path, [])
            existing = {s.tmux_session_name: s for s in wt.sessions if s.foreign}
            # Drop foreign sessions that are no longer live.
            gone = [n for n in existing if n not in names]
            if gone:
                wt.sessions = [
                    s for s in wt.sessions if not s.foreign or s.tmux_session_name in names
                ]
            # Append newly-seen foreign sessions (reuse existing objects).
            present = {s.tmux_session_name for s in wt.sessions}
            for name in names:
                if name not in present:
                    wt.sessions.append(Session(
                        tmux_session_name=name,
                        label=name,
                        session_type="claude",
                        foreign=True,
                    ))
                    present.add(name)
                    added += 1
        return added

    def do_refresh(self) -> None:
        """Manually run the live-sync now and notify the result (F5)."""
        async def _refresh() -> None:
            summary = await self._sync_from_disk()
            foreign = await self._discover_foreign_sessions()
            # Repaint git/session state so newly-merged rows render immediately.
            await self._periodic_refresh_impl()
            self.app.notify(self._refresh_summary(summary, foreign=foreign))

        self.run_worker(_refresh, exclusive=False)

    @staticmethod
    def _refresh_summary(summary: dict[str, int], foreign: int) -> str:
        added_wt = summary["worktrees_added"]
        added_sess = summary["sessions_added"]
        removed_wt = summary["worktrees_removed"]
        if not (added_wt or added_sess or removed_wt or foreign):
            return "Refreshed — nothing new"
        bits = []
        if added_wt:
            bits.append(f"+{added_wt} worktree{'s' if added_wt != 1 else ''}")
        if added_sess:
            bits.append(f"+{added_sess} session{'s' if added_sess != 1 else ''}")
        if foreign:
            bits.append(f"{foreign} external")
        if removed_wt:
            bits.append(f"-{removed_wt} worktree{'s' if removed_wt != 1 else ''}")
        return "Refreshed: " + ", ".join(bits)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _refresh_sidebar(self, wt: Worktree) -> None:
        session_names = [s.tmux_session_name for s in wt.sessions]
        states, status, dirty = await asyncio.gather(
            asyncio.to_thread(batch_detect_session_states, session_names) if session_names else asyncio.sleep(0, result={}),
            asyncio.to_thread(get_branch_status, wt.path, self._config.remote, self._config.main_branch),
            asyncio.to_thread(get_worktree_dirty, wt.path),
        )
        try:
            wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            wtc.query_one(SessionSidebar).show_worktree(wt, states=states, git_status=status, git_dirty=dirty)
        except Exception:
            logger.debug("Failed to refresh sidebar", exc_info=True, extra={"worktree": wt.name})

    async def _refresh_git_ui(self, wt: Worktree) -> None:
        invalidate_git_cache(wt.path)
        status = await asyncio.to_thread(get_branch_status, wt.path, self._config.remote, self._config.main_branch)
        dirty = await asyncio.to_thread(get_worktree_dirty, wt.path)
        try:
            wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            wtc.query_one(SessionSidebar)._refresh_git_status(wt, status=status, dirty=dirty)
        except Exception:
            logger.debug("Failed to refresh sidebar git status", exc_info=True, extra={"worktree": wt.name})
        self._refresh_tab_label(wt, git_data=(status, dirty))

    def _refresh_tab_label(self, wt: Worktree, git_data: tuple[dict, bool] | None = None) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            tab = tabs.get_tab(f"wt-{wt.name}")
            tab.label = self._tab_label(wt, git_data=git_data)
        except Exception:
            logger.debug("Failed to refresh tab label", exc_info=True, extra={"worktree": wt.name})

    # ── Git actions ───────────────────────────────────────────────────────────

    def _git_push(self, wt: Worktree) -> None:
        async def _push() -> None:
            err = await asyncio.to_thread(git_push, wt.path, self._config.remote, wt.branch)
            if err:
                self.app.notify(f"Push failed: {err[:100]}", severity="error")
            else:
                self.app.notify(f"Pushed to {self._config.remote}")
            await self._refresh_git_ui(wt)

        self.run_worker(_push, exclusive=False)

    def _git_pull(self, wt: Worktree) -> None:
        async def _pull() -> None:
            err = await asyncio.to_thread(git_pull, wt.path, self._config.remote, self._config.main_branch)
            if err:
                self.app.notify(f"Pull failed: {err[:100]}", severity="error")
            else:
                self.app.notify(f"Pulled latest from {self._config.main_branch}")
            await self._refresh_git_ui(wt)

        self.run_worker(_pull, exclusive=False)

    def _git_create_pr(self, wt: Worktree) -> None:
        async def _pr() -> None:
            ok, result = await asyncio.to_thread(git_create_pr, wt.path, wt.branch)
            if ok:
                self.app.notify(f"PR created: {result}")
            else:
                self.app.notify(f"PR failed: {result[:100]}", severity="error")

        self.run_worker(_pr, exclusive=False)

    def _git_commit(self, wt: Worktree) -> None:
        def handle_message(msg: str | None) -> None:
            if msg is None:
                return

            async def _commit() -> None:
                exclude = list(self._config.copies) + list(self._config.symlinks)
                err = await asyncio.to_thread(git_commit, wt.path, msg, exclude)
                if err:
                    self.app.notify(f"Commit failed: {err[:100]}", severity="error")
                else:
                    self.app.notify("Committed")
                await self._refresh_git_ui(wt)

            self.run_worker(_commit, exclusive=False)

        self.app.push_screen(CommitMessageScreen(self._config.commit_placeholder), callback=handle_message)
