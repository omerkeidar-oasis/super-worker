"""Visual regression (SVG) tests for the terminal preview render.

These use ``pytest-textual-snapshot``'s ``snap_compare`` fixture: each test
renders a small host app that mounts a :class:`TerminalPane`, drives it into a
deterministic state, and compares the rendered SVG against a committed baseline
under ``tests/__snapshots__/``.

Regenerate baselines after an intentional render change with::

    pytest tests/test_preview_snapshots.py --snapshot-update

HERMETIC: no real tmux server and no network. Content is fed via
``render_snapshot`` / ``_append_history`` directly; where a session is set the
tmux entry points are monkeypatched to no-ops (mirrors
``test_terminal_render.py::test_history_swaps_on_session_switch``).
"""

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

import super_worker.widgets.terminal_pane as tp_mod
from super_worker.services.tmux import PaneSnapshot
from super_worker.widgets.terminal_pane import TerminalPane, render_snapshot

# Fixed terminal geometry so the SVG baselines are stable across machines.
_SIZE = (100, 30)


class _HostApp(App):
    """Minimal app that hosts a single TerminalPane (see test_terminal_render)."""

    def compose(self) -> ComposeResult:
        yield TerminalPane()


# ── (a) Colored output: both foreground AND background must survive ──────────

# Black-on-green "ADDED" and white-on-red "REMOVED" (diff-style), plus a
# foreground-only line — the backgrounds must render, not be stripped.
_DIFF_SNAPSHOT = PaneSnapshot(
    text=(
        "diff --git a/app.py b/app.py\n"
        "\x1b[1;34m@@ -1,4 +1,4 @@\x1b[0m\n"
        "\x1b[30;42m+ ADDED a brand new line\x1b[0m\n"
        "\x1b[37;41m- REMOVED the old line\x1b[0m\n"
        "\x1b[33m  context line stays yellow\x1b[0m\n"
        "\x1b[32mgreen foreground only, no background\x1b[0m"
    ),
    cursor_visible=False,
)


def test_preview_colors(snap_compare):
    async def run_before(pilot):
        pane = pilot.app.query_one(TerminalPane)
        pane.query_one("#terminal-content", Static).update(render_snapshot(_DIFF_SNAPSHOT))
        await pilot.pause()

    assert snap_compare(_HostApp(), terminal_size=_SIZE, run_before=run_before)


# ── (b) Reverse-video cursor overlay on a prompt line ────────────────────────

_PROMPT = "user@host project % "
_PROMPT_SNAPSHOT = PaneSnapshot(
    text=(
        "$ python -m pytest\n"
        "\x1b[32m.....\x1b[0m 5 passed\n"
        f"{_PROMPT}"
    ),
    cursor_x=len(_PROMPT),  # one cell past the trimmed prompt end
    cursor_y=2,
    pane_height=3,
    cursor_visible=True,
)


def test_preview_cursor_overlay(snap_compare):
    async def run_before(pilot):
        pane = pilot.app.query_one(TerminalPane)
        pane.query_one("#terminal-content", Static).update(render_snapshot(_PROMPT_SNAPSHOT))
        await pilot.pause()

    assert snap_compare(_HostApp(), terminal_size=_SIZE, run_before=run_before)


# ── (c) Populated multi-line history / scrollback ────────────────────────────

def _hist_batch(start: int, n: int) -> Text:
    lines = [f"[{i:02d}] step {i}: \x1b[32mok\x1b[0m done" for i in range(start, start + n)]
    return Text.from_ansi("\n".join(lines))


def test_preview_history_scrollback(snap_compare):
    async def run_before(pilot):
        pane = pilot.app.query_one(TerminalPane)
        # Clear the placeholder so the live row below the scrollback is blank.
        pane.query_one("#terminal-content", Static).update(Text(""))
        for start in range(0, 40, 10):  # 4 batches -> 40 history lines
            pane._append_history("hist", _hist_batch(start, 10))
        await pilot.pause()
        # Follow the tail, exactly like the live preview does.
        pane.query_one("#terminal-scroll", VerticalScroll).scroll_end(animate=False)
        await pilot.pause()

    assert snap_compare(_HostApp(), terminal_size=_SIZE, run_before=run_before)


# ── (d) Session switch: B shows its own history, not A's ──────────────────────

def test_preview_session_switch(snap_compare, monkeypatch):
    # Setting active_session fires the real watch_active_session ->
    # _remount_history wiring, which polls/resizes tmux; stub those out.
    monkeypatch.setattr(tp_mod, "capture_pane_snapshot", lambda name: PaneSnapshot(text=""))
    monkeypatch.setattr(tp_mod, "set_window_size", lambda *a, **k: None)
    monkeypatch.setattr(tp_mod, "resize_window", lambda *a, **k: None)

    async def run_before(pilot):
        pane = pilot.app.query_one(TerminalPane)
        pane._paused = False

        pane.active_session = "A"  # normal assignment -> fires the watcher
        await pilot.pause(delay=0.1)
        pane._append_history("A", Text("\n".join(f"A-line {i}" for i in range(50))))
        await pilot.pause(delay=0.1)

        # Switch to B: A's chunks leave the DOM; B starts with its own history.
        pane.active_session = "B"
        await pilot.pause(delay=0.1)
        pane._append_history("B", Text("\n".join(f"B-log entry {i}" for i in range(15))))
        await pilot.pause(delay=0.1)
        pane.query_one("#terminal-scroll", VerticalScroll).scroll_end(animate=False)
        await pilot.pause(delay=0.1)

    assert snap_compare(_HostApp(), terminal_size=_SIZE, run_before=run_before)


# ── (e) Empty / placeholder state ────────────────────────────────────────────

def test_preview_placeholder(snap_compare):
    # A freshly-mounted pane with no session shows the placeholder from compose().
    assert snap_compare(_HostApp(), terminal_size=_SIZE)
