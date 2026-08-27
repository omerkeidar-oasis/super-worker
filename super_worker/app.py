import asyncio
import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Footer, Header, Static

from super_worker.config import ResolvedConfig, load_config
from super_worker.constants import SIDEBAR_REFRESH_S
from super_worker.services.hooks import install_hooks
from super_worker.services.state import (
    load_and_reconcile,
    load_projects_registry,
    remove_from_projects_registry,
)
from super_worker.services.ui_state import (
    ProjectUIState,
    UIState,
    load_ui_state,
    save_ui_state,
)
from super_worker.widgets.project_drawer import (
    DockToggled,
    ProjectDrawer,
    ProjectRemoved,
    ProjectSelected,
    ProjectTabBar,
)
from super_worker.widgets.project_view import ProjectView
from super_worker.widgets.sidebar import SidebarDivider

logger = logging.getLogger(__name__)


class SuperWorkerApp(App):
    """Super Worker — Claude Code Instance Manager TUI."""

    TITLE = "Super Worker"

    DEFAULT_CSS = """
    #main-area {
        height: 1fr;
    }
    #project-switcher {
        width: 1fr;
        height: 1fr;
    }
    #no-project {
        width: 100%;
        height: 100%;
        content-align: center middle;
        text-style: italic;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("ctrl+n", "new_worktree", "New Worktree"),
        Binding("ctrl+s", "new_session", "New Session"),
        Binding("ctrl+a", "full_attach", "Full Attach"),
        Binding("ctrl+t", "open_terminal", "Open Terminal"),
        # F2, not Ctrl+R: Ctrl+R is forwarded to Claude Code (transcript view —
        # the only way to browse a CC session's history; CC never writes
        # terminal scrollback).
        Binding("f2", "rename_session", "Rename Session"),
        Binding("ctrl+d", "delete_worktree", "Delete Worktree"),
        Binding("ctrl+o", "toggle_project_drawer", "Projects"),
        Binding("ctrl+shift+left", "prev_project", "Prev Project", key_display="ctrl+⇧◀"),
        Binding("ctrl+shift+right", "next_project", "Next Project", key_display="ctrl+⇧▶"),
        Binding("ctrl+e", "edit_settings", "Settings"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f12", "debug_screenshot", "Screenshot", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        install_hooks()
        self._active_project_view: ProjectView | None = None
        self._open_configs: list[ResolvedConfig] = []
        self._opening: set[str] = set()  # paths with an in-flight open worker
        self._initial_project: tuple[ResolvedConfig, object] | None = None
        self._attention_paths: set[str] = set()

        # Workspace UI-state (open projects, per-project active worktree/session,
        # sidebar width) persisted from the last run. Saves are gated on this flag
        # so a mid-restore write can't persist a half-open project list.
        self._ui_state: UIState = load_ui_state()
        self._workspace_ready = False
        # Seed the sidebar width BEFORE composing so freshly-mounted dividers
        # (which read this class attribute on mount) adopt the restored width.
        if self._ui_state.sidebar_width is not None:
            SidebarDivider._shared_width = self._ui_state.sidebar_width

        try:
            config = load_config()
            state = load_and_reconcile(config)
            self._initial_project = (config, state)
            self._open_configs.append(config)
        except RuntimeError:
            pass  # Started outside a git repo; drawer will prompt

    def compose(self) -> ComposeResult:
        yield Header()
        # Docked mode: horizontal tab strip sits here, above worktree tabs.
        # Hidden by default; shown when user presses the drawer's pin button.
        yield ProjectTabBar(id="project-tab-bar")
        with Horizontal(id="main-area"):
            # Overlay mode: left-side drawer, hidden by default (Ctrl+O to toggle).
            yield ProjectDrawer(id="project-drawer")
            # The placeholder is ALWAYS mounted: ContentSwitcher(initial=None)
            # hides every child (so the message never showed), and removing
            # the last open project needs something to switch back to.
            initial_id = (
                f"pv-{self._initial_project[0].state_hash}"
                if self._initial_project else "no-project"
            )
            with ContentSwitcher(id="project-switcher", initial=initial_id):
                yield Static(
                    "No project open.\nPress Ctrl+O to open a project.",
                    id="no-project",
                )
                if self._initial_project:
                    config, state = self._initial_project
                    saved = self._ui_state.projects.get(str(config.repo_root))
                    yield ProjectView(
                        config, state,
                        restore_worktree=saved.worktree if saved else None,
                        restore_session=saved.session if saved else None,
                        id=f"pv-{config.state_hash}",
                    )
        yield Footer()

    def on_mount(self) -> None:
        if self._initial_project:
            config, _ = self._initial_project
            try:
                pv = self.query_one(f"#pv-{config.state_hash}", ProjectView)
                self._active_project_view = pv
                self.sub_title = str(config.repo_root)
            except Exception:
                pass

        self._refresh_drawer()
        self.query_one(ProjectTabBar).show()
        self.set_interval(SIDEBAR_REFRESH_S, self._periodic_refresh)
        # Reopen the projects that were open last run (skips the launch project,
        # already mounted, and any paths that no longer exist). Runs once, in a
        # worker so heavy project loads stay off the mount path.
        self.run_worker(self._restore_workspace, exclusive=False)

    async def _restore_workspace(self) -> None:
        """Reopen previously-open projects, then unlock workspace persistence."""
        initial_path = (
            str(self._initial_project[0].repo_root) if self._initial_project else None
        )
        reopened_any = False
        for path in self._ui_state.open_projects:
            if path == initial_path or not Path(path).exists():
                continue
            try:
                await self._open_or_switch_project(path)
                reopened_any = True
            except Exception:
                logger.debug("Failed to restore project %s", path, exc_info=True)
        # Keep the launch (CWD) project focused if there was one; otherwise, when
        # nothing was restored, prompt the user to pick a project.
        if self._initial_project and reopened_any:
            await self._activate_project(self._initial_project[0])
        elif not self._initial_project and not self._open_configs:
            try:
                self.query_one(ProjectDrawer).open()
            except Exception:
                pass
        self._workspace_ready = True
        self.save_workspace_state()

    def save_workspace_state(self) -> None:
        """Persist open projects, per-project active worktree/session, sidebar width.

        Called on discrete user actions (project open/close, worktree/session
        switch, divider drag end) — never on a hot path. Gated on
        ``_workspace_ready`` so a mid-restore write can't truncate open_projects.
        """
        if not self._workspace_ready:
            return
        try:
            projects: dict[str, ProjectUIState] = {}
            for cfg in self._open_configs:
                try:
                    pv = self.query_one(f"#pv-{cfg.state_hash}", ProjectView)
                except Exception:
                    continue
                projects[str(cfg.repo_root)] = ProjectUIState(
                    worktree=pv.active_worktree_name,
                    session=pv.active_session_name,
                )
            state = UIState(
                open_projects=[str(cfg.repo_root) for cfg in self._open_configs],
                projects=projects,
                sidebar_width=SidebarDivider._shared_width,
            )
            save_ui_state(state)
        except Exception:
            logger.debug("Failed to persist workspace UI-state", exc_info=True)

    # ── Periodic refresh ──────────────────────────────────────────────────────

    def _periodic_refresh(self) -> None:
        if self._active_project_view:
            # group= scopes exclusivity: without it, exclusive=True lands in
            # the DEFAULT worker group and silently cancels unrelated workers
            # on the App node — most damagingly the _open/_reactivate project
            # loads, which then never complete and show no error.
            self.run_worker(
                self._active_project_view.periodic_refresh,
                exclusive=True,
                group="periodic-refresh",
                name="periodic-refresh",
            )
        # Check states for non-active projects so attention indicators update.
        # exclusive=True per project: if a previous check is still running when
        # the next 5s tick fires, cancel it rather than letting workers pile up.
        for cfg in self._open_configs:
            pv_id = f"pv-{cfg.state_hash}"
            try:
                pv = self.query_one(f"#{pv_id}", ProjectView)
                if pv is not self._active_project_view:
                    self.run_worker(
                        pv.check_attention,
                        exclusive=True,
                        group=f"ca-{pv_id}",
                    )
            except Exception:
                pass

    # ── Project drawer / tab bar ──────────────────────────────────────────────

    def _refresh_drawer(self) -> None:
        projects = load_projects_registry()
        current = str(self._active_project_view.config.repo_root) if self._active_project_view else None
        open_paths = {str(cfg.repo_root) for cfg in self._open_configs}
        try:
            self.query_one(ProjectDrawer).refresh_projects(
                projects, current=current, open_paths=open_paths,
                attention_paths=self._attention_paths,
            )
            self.query_one(ProjectTabBar).refresh_projects(
                all_projects=projects, open_paths=open_paths, current=current,
                attention_paths=self._attention_paths,
            )
        except Exception:
            pass

    async def action_quit(self) -> None:
        """Quit, first releasing manual sizing on all known sessions.

        The preview pins sessions to its widget size (window-size manual);
        without this, a plain `tmux attach` after quitting shows a small
        fixed-size window instead of filling the terminal.
        """
        from super_worker.services.tmux import set_window_size

        def _release_all() -> None:
            for pv in self.query(ProjectView):
                for wt in pv.state.worktrees:
                    for s in wt.sessions:
                        set_window_size(s.tmux_session_name, "latest")

        try:
            await asyncio.to_thread(_release_all)
        except Exception:
            logger.debug("Failed to release manual window sizing on quit", exc_info=True)
        await super().action_quit()

    def action_toggle_project_drawer(self) -> None:
        self.query_one(ProjectDrawer).toggle()

    def on_dock_toggled(self, event: DockToggled) -> None:
        """Switch between overlay drawer and docked tab bar."""
        drawer = self.query_one(ProjectDrawer)
        tab_bar = self.query_one(ProjectTabBar)
        if event.docked:
            drawer.close()
            tab_bar.show()
            self._refresh_drawer()
        else:
            tab_bar.hide()
            drawer.open()

    def on_project_view_attention_changed(self, event: ProjectView.AttentionChanged) -> None:
        """A project's attention state changed — update drawer/tab bar."""
        if event.needs_attention:
            self._attention_paths.add(event.path)
        else:
            self._attention_paths.discard(event.path)
        self._refresh_drawer()

    def on_project_selected(self, event: ProjectSelected) -> None:
        async def _open():
            await self._open_or_switch_project(event.path)

        self.run_worker(_open, exclusive=False)

    def on_project_removed(self, event: ProjectRemoved) -> None:
        remove_from_projects_registry(event.path)
        removed = [c for c in self._open_configs if str(c.repo_root) == event.path]
        self._open_configs = [c for c in self._open_configs if str(c.repo_root) != event.path]
        was_active = bool(
            self._active_project_view
            and str(self._active_project_view.config.repo_root) == event.path
        )
        # Unmount the widget — leaving it mounted keeps its UI interactive
        # behind the switcher and crashes with DuplicateIds if the project
        # is ever reopened (same widget id would be mounted twice).
        for cfg in removed:
            try:
                self.query_one(f"#pv-{cfg.state_hash}", ProjectView).remove()
            except Exception:
                logger.debug("ProjectView already gone for %s", event.path)
        if was_active:
            self._active_project_view = None
            self.sub_title = ""
            if self._open_configs:
                cfg = self._open_configs[-1]

                async def _reactivate():
                    await self._activate_project(cfg)

                self.run_worker(_reactivate, exclusive=False)
            else:
                try:
                    switcher = self.query_one("#project-switcher", ContentSwitcher)
                    switcher.current = "no-project"
                except Exception:
                    logger.debug("Failed to show no-project placeholder", exc_info=True)
        self._refresh_drawer()
        self.save_workspace_state()
        self.notify(f"Removed: {Path(event.path).name}")

    async def _open_or_switch_project(self, path: str) -> None:
        """Switch to an already-open project or load a new one."""
        # Already open?
        for cfg in self._open_configs:
            if str(cfg.repo_root) == path:
                await self._activate_project(cfg)
                return

        # In-flight guard: two rapid selections of the same project would
        # both pass the check above and mount duplicate widget ids (crash).
        if path in self._opening:
            return
        self._opening.add(path)
        try:
            await self._load_project(path)
        finally:
            self._opening.discard(path)

    async def _load_project(self, path: str) -> None:
        try:
            new_config = await asyncio.to_thread(load_config, Path(path))
        except RuntimeError as e:
            self.notify(str(e), severity="error")
            return

        new_state = await asyncio.to_thread(load_and_reconcile, new_config)

        pv_id = f"pv-{new_config.state_hash}"
        saved = self._ui_state.projects.get(str(new_config.repo_root))
        pv = ProjectView(
            new_config, new_state,
            restore_worktree=saved.worktree if saved else None,
            restore_session=saved.session if saved else None,
            id=pv_id,
        )

        switcher = self.query_one("#project-switcher", ContentSwitcher)

        # Pause the outgoing project before mounting the new one
        if self._active_project_view:
            self._active_project_view.pause_watching()

        await switcher.mount(pv)
        switcher.current = pv_id
        self._active_project_view = pv
        self._open_configs.append(new_config)
        self.sub_title = str(new_config.repo_root)
        self._refresh_drawer()
        self.save_workspace_state()
        self.notify(f"Opened: {new_config.repo_root.name}")

    async def _activate_project(self, config: ResolvedConfig) -> None:
        """Switch focus to an already-mounted ProjectView."""
        pv_id = f"pv-{config.state_hash}"
        try:
            # Pause the outgoing project's capture timer before switching.
            # The kqueue state-file watchers are not paused — they keep running
            # so background projects still update their attention indicators.
            if self._active_project_view:
                self._active_project_view.pause_watching()

            switcher = self.query_one("#project-switcher", ContentSwitcher)
            switcher.current = pv_id
            self._active_project_view = self.query_one(f"#{pv_id}", ProjectView)
            self._active_project_view.resume_watching()
            # Move focus off the outgoing (now hidden) project's terminal —
            # otherwise keystrokes keep going to the invisible session.
            self._active_project_view.focus_terminal()
            self.sub_title = str(config.repo_root)
            self._refresh_drawer()
        except Exception:
            logger.debug("Failed to activate project", exc_info=True)

    # ── Action delegation to active ProjectView ───────────────────────────────

    def action_new_worktree(self) -> None:
        if pv := self._active_project_view:
            pv.do_new_worktree()
        else:
            self.notify("Open a project first (Ctrl+O)", severity="warning")

    def action_new_session(self) -> None:
        if pv := self._active_project_view:
            pv.do_new_session()
        else:
            self.notify("Open a project first (Ctrl+O)", severity="warning")

    def action_rename_session(self) -> None:
        if pv := self._active_project_view:
            pv.do_rename_session()

    def action_full_attach(self) -> None:
        if pv := self._active_project_view:
            pv.do_full_attach()

    def action_open_terminal(self) -> None:
        if pv := self._active_project_view:
            pv.do_open_terminal()

    def action_edit_settings(self) -> None:
        if pv := self._active_project_view:
            pv.do_edit_settings()
        else:
            self.notify("Open a project first (Ctrl+O)", severity="warning")

    def action_delete_worktree(self) -> None:
        if pv := self._active_project_view:
            pv.do_delete_worktree()

    def action_debug_screenshot(self) -> None:
        """Save an SVG screenshot for debugging (F12)."""
        import time
        out = Path("/private/tmp/sw-pilot-screenshots")
        out.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        path = out / f"screenshot_{ts}.svg"
        svg = self.export_screenshot(title=f"sw-{ts}")
        path.write_text(svg)
        self.notify(f"Screenshot: {path.name}")

    def action_prev_project(self) -> None:
        self._cycle_project(-1)

    def action_next_project(self) -> None:
        self._cycle_project(1)

    def _cycle_project(self, direction: int) -> None:
        projects = load_projects_registry()
        if not projects:
            return
        current = str(self._active_project_view.config.repo_root) if self._active_project_view else None
        if current and current in projects:
            idx = (projects.index(current) + direction) % len(projects)
        else:
            idx = 0
        path = projects[idx]

        async def _switch():
            await self._open_or_switch_project(path)

        self.run_worker(_switch, exclusive=False)
