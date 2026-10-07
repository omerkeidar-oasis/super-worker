"""Preview key forwarding + text selection (regressions from the overhaul).

The preview must stay interactive WITHOUT attaching: select text to copy,
pass control/meta keys through to the session, and insert newlines.
"""

import pytest
from textual.app import App, ComposeResult
from textual.events import Key
from textual.widgets import Static

from super_worker.widgets.terminal_pane import TerminalPane


class _Host(App):
    def compose(self) -> ComposeResult:
        yield TerminalPane()


def _record(pane) -> list:
    """Capture _send_keys_async calls instead of touching tmux."""
    calls: list = []
    pane._send_keys_async = lambda *a, **k: calls.append((a, k))
    return calls


async def _pane_with_session(app):
    p = app.query_one(TerminalPane)
    p.set_reactive(TerminalPane.active_session, "s")  # no watcher side effects
    return p


@pytest.mark.asyncio
async def test_preview_content_is_selectable():
    """Select-to-copy without attaching: content must allow text selection."""
    app = _Host()
    async with app.run_test():
        assert app.query_one("#terminal-content", Static).ALLOW_SELECT is True


@pytest.mark.asyncio
async def test_ctrl_key_forwarded_as_named_key():
    """Ctrl+B → tmux named key C-b (not the raw \\x02 byte, which arrives mangled)."""
    app = _Host()
    async with app.run_test():
        p = await _pane_with_session(app)
        calls = _record(p)
        p.on_key(Key("ctrl+b", "\x02"))
        assert (("C-b",), {}) in calls, calls


@pytest.mark.asyncio
async def test_alt_key_forwarded_as_meta():
    app = _Host()
    async with app.run_test():
        p = await _pane_with_session(app)
        calls = _record(p)
        p.on_key(Key("alt+b", None))
        assert (("M-b",), {}) in calls, calls


@pytest.mark.asyncio
async def test_printable_char_sent_literally():
    app = _Host()
    async with app.run_test():
        p = await _pane_with_session(app)
        calls = _record(p)
        p.on_key(Key("slash", "/"))
        assert (("/",), {"literal": True}) in calls, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["shift+enter", "alt+enter", "ctrl+enter"])
async def test_newline_keys_insert_newline(key):
    """Newline shortcuts forward as meta+enter (ESC, Enter), not submit."""
    app = _Host()
    async with app.run_test():
        p = await _pane_with_session(app)
        calls = _record(p)
        p.on_key(Key(key, None))
        assert (("Escape", "Enter"), {}) in calls, calls


@pytest.mark.asyncio
async def test_plain_enter_submits():
    """Plain Enter still submits (maps to Enter), distinct from a newline."""
    app = _Host()
    async with app.run_test():
        p = await _pane_with_session(app)
        calls = _record(p)
        p.on_key(Key("enter", "\r"))
        assert (("Enter",), {}) in calls, calls


@pytest.mark.asyncio
async def test_render_frozen_while_selecting(monkeypatch):
    """Live re-render must pause while text is selected, so the mouse-up
    auto-copy reads a stable selection instead of a cleared one."""
    from textual.worker import WorkerState
    from rich.text import Text as _Text

    app = _Host()
    async with app.run_test():
        p = await _pane_with_session(app)
        p._at_bottom = lambda: True
        content = app.query_one("#terminal-content", Static)
        updates: list = []
        content.update = lambda t=None, **k: updates.append(getattr(t, "plain", t))

        result = ("s", object(), _Text("LIVE"), None, None, 0, False, False)

        class _Evt:
            state = WorkerState.SUCCESS

            class worker:  # noqa: N801
                pass

        _Evt.worker.result = result

        # Selection active → content must NOT be mutated.
        monkeypatch.setattr(p, "_has_active_selection", lambda: True)
        p.on_worker_state_changed(_Evt())
        assert updates == [], "re-rendered despite an active selection"

        # No selection → content updates normally.
        monkeypatch.setattr(p, "_has_active_selection", lambda: False)
        p.on_worker_state_changed(_Evt())
        assert "LIVE" in updates, updates


@pytest.mark.asyncio
async def test_ctrl_c_interrupts_session():
    """Ctrl+C forwards an interrupt (C-c); copy is auto-on-select, not Ctrl+C."""
    app = _Host()
    async with app.run_test():
        p = await _pane_with_session(app)
        calls = _record(p)
        p.on_key(Key("ctrl+c", "\x03"))
        assert (("C-c",), {}) in calls, calls
