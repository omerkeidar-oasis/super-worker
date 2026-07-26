"""Pilot tests for gate-badge rendering + live ledger watching (slice a).

Widget-level, using Textual's run_test (the test_sidebar.py convention) and a
real kqueue watch (the test_pane_watcher.py convention). Together they prove the
design's two verify mechanisms: badges render from a seeded ledger file, and
appending a verdict to a watched ledger fires a VerdictChanged within a kqueue
tick.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from super_worker.services.verdict import GateState, GateVerdicts, read_verdicts
from super_worker.widgets.sidebar import SessionSidebar
from super_worker.widgets.terminal_pane import TerminalPane


async def _wait_for(condition, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return False


def _seed_ledger(path: Path, *verdicts: tuple[str, str], task: str = "sw-feat") -> None:
    """Write a ledger file with (gate, result) verdict lines for ``task``."""
    lines = [
        json.dumps({"ts": "2026-07-26T00:00:00Z", "repo": "r", "task": task,
                    "event": "verdict", "gate": gate, "result": result})
        for gate, result in verdicts
    ]
    path.write_text("\n".join(lines) + "\n")


# ── Sidebar badge rendering ─────────────────────────────────────────────────

class SidebarApp(App):
    def compose(self) -> ComposeResult:
        yield SessionSidebar()


@pytest.mark.asyncio
async def test_gates_section_hidden_by_default():
    """A freshly mounted sidebar shows no Gates section (no verdicts yet)."""
    app = SidebarApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        sidebar = app.query_one(SessionSidebar)
        assert sidebar.query_one("#gates-section", Static).display is False
        assert sidebar.query_one("#gate-badges", Static).display is False


@pytest.mark.asyncio
async def test_badges_render_from_seeded_ledger(tmp_path):
    """Seed a ledger file → read_verdicts → render → the Gates section shows the
    parsed verdicts (green deterministic, red craftsmanship)."""
    led = tmp_path / "ledger.jsonl"
    _seed_ledger(led, ("deterministic", "green"), ("craftsmanship", "red"))
    verdicts = read_verdicts(led, "sw-feat")

    app = SidebarApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)
        sidebar.render_gate_badges(verdicts)
        await pilot.pause()

        section = sidebar.query_one("#gates-section", Static)
        badges = sidebar.query_one("#gate-badges", Static)
        assert section.display is True
        assert badges.display is True
        content = str(badges.content)
        assert "deterministic" in content and "craftsmanship" in content
        assert "✓" in content  # pass glyph (deterministic)
        assert "✗" in content  # fail glyph (craftsmanship)


@pytest.mark.asyncio
async def test_badges_hidden_when_verdicts_go_empty(tmp_path):
    """Rendering an all-none GateVerdicts re-hides a previously shown section."""
    led = tmp_path / "ledger.jsonl"
    _seed_ledger(led, ("behavioral", "green"))
    app = SidebarApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)
        sidebar.render_gate_badges(read_verdicts(led, "sw-feat"))
        await pilot.pause()
        assert sidebar.query_one("#gate-badges", Static).display is True

        sidebar.render_gate_badges(GateVerdicts())  # empty
        await pilot.pause()
        assert sidebar.query_one("#gates-section", Static).display is False
        assert sidebar.query_one("#gate-badges", Static).display is False


@pytest.mark.asyncio
async def test_badges_hidden_when_none():
    """None (no ledger) hides the section."""
    app = SidebarApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)
        sidebar.render_gate_badges(None)
        await pilot.pause()
        assert sidebar.query_one("#gate-badges", Static).display is False


# ── Live ledger watching → VerdictChanged ───────────────────────────────────

class LedgerWatchApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.verdict_paths: list[str] = []

    def compose(self) -> ComposeResult:
        yield TerminalPane()

    def on_terminal_pane_verdict_changed(self, event: TerminalPane.VerdictChanged) -> None:
        self.verdict_paths.append(event.ledger_path)


@pytest.mark.asyncio
async def test_append_verdict_posts_verdict_changed(tmp_path):
    """Design verify recipe: append a red craftsmanship verdict to a watched
    ledger → VerdictChanged fires within a kqueue tick, and the appended line
    reads back as craftsmanship-fail."""
    led = tmp_path / ".kinetic" / "ledger.jsonl"
    led.parent.mkdir(parents=True)
    _seed_ledger(led, ("deterministic", "green"), task="sw-x")

    app = LedgerWatchApp()
    async with app.run_test() as pilot:
        pane = app.query_one(TerminalPane)
        pane.start_watching_paths([str(led)])
        await pilot.pause()

        with open(led, "a") as f:
            f.write(json.dumps({"event": "verdict", "gate": "craftsmanship",
                                "result": "red", "task": "sw-x"}) + "\n")
            f.flush()
            os.fsync(f.fileno())

        met = await _wait_for(lambda: bool(app.verdict_paths), timeout=5.0)
        assert met, "VerdictChanged not posted within 5s of a ledger append"
        assert app.verdict_paths[0] == str(led)
        assert read_verdicts(led, "sw-x").craftsmanship is GateState.FAIL


@pytest.mark.asyncio
async def test_start_watching_paths_skips_absent_then_arms_on_appearance(tmp_path):
    """An absent ledger is not watched (retried on the next sweep); once it
    exists, the next call arms the watch."""
    led = tmp_path / "ledger.jsonl"  # absent
    app = LedgerWatchApp()
    async with app.run_test() as pilot:
        pane = app.query_one(TerminalPane)
        pane.start_watching_paths([str(led)])
        await pilot.pause()
        assert str(led) not in pane._watched_ledger_paths

        led.write_text("")  # ledger appears
        pane.start_watching_paths([str(led)])
        await pilot.pause()
        assert str(led) in pane._watched_ledger_paths


@pytest.mark.asyncio
async def test_start_watching_paths_drops_stale(tmp_path):
    """A path no longer requested is unwatched."""
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_text("")
    b.write_text("")
    app = LedgerWatchApp()
    async with app.run_test() as pilot:
        pane = app.query_one(TerminalPane)
        pane.start_watching_paths([str(a), str(b)])
        await pilot.pause()
        assert pane._watched_ledger_paths == {str(a), str(b)}

        pane.start_watching_paths([str(a)])  # drop b
        await pilot.pause()
        assert pane._watched_ledger_paths == {str(a)}
