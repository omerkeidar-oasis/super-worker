"""Tests for the trust-ledger verdict reader (verdict cockpit, slice a).

Pure logic only — no Textual, no git, no subprocess. Covers the three things the
badges hang on: ledger-line → badge-state mapping (latest-wins, malformed-skip,
branch filtering), in-tree vs foreign ledger-path resolution, and the markup the
tab / sidebar render.
"""

import json
from pathlib import Path

import pytest

from super_worker.services.verdict import (
    GateState,
    GateVerdicts,
    ledger_path_for_worktree,
    ledger_slug,
    read_verdicts,
    resolve_ledger_path,
    sidebar_badges_markup,
    tab_badge_markup,
)


def _write_ledger(path: Path, *objs: dict | str) -> None:
    """Write JSONL lines; a str entry is written verbatim (for malformed lines)."""
    lines = [o if isinstance(o, str) else json.dumps(o) for o in objs]
    path.write_text("\n".join(lines) + "\n")


def _verdict(gate: str, result: str, task: str = "sw-feat") -> dict:
    return {"ts": "2026-07-26T00:00:00Z", "repo": "r", "task": task,
            "event": "verdict", "gate": gate, "result": result}


# ── read_verdicts: ledger-line → badge-state mapping ────────────────────────

def test_result_green_maps_to_pass(tmp_path):
    led = tmp_path / "ledger.jsonl"
    _write_ledger(led, _verdict("deterministic", "green"))
    assert read_verdicts(led, "sw-feat").deterministic is GateState.PASS


def test_result_red_maps_to_fail(tmp_path):
    led = tmp_path / "ledger.jsonl"
    _write_ledger(led, _verdict("craftsmanship", "red"))
    assert read_verdicts(led, "sw-feat").craftsmanship is GateState.FAIL


def test_gate_with_no_verdict_is_none(tmp_path):
    led = tmp_path / "ledger.jsonl"
    _write_ledger(led, _verdict("deterministic", "green"))
    gv = read_verdicts(led, "sw-feat")
    assert gv.behavioral is GateState.NONE and gv.craftsmanship is GateState.NONE


def test_latest_verdict_per_gate_wins(tmp_path):
    """Append-only ⇒ the last line for a gate is the current state."""
    led = tmp_path / "ledger.jsonl"
    _write_ledger(
        led,
        _verdict("craftsmanship", "green"),
        _verdict("craftsmanship", "red"),   # later red overrides the earlier green
    )
    assert read_verdicts(led, "sw-feat").craftsmanship is GateState.FAIL


def test_latest_wins_red_then_green(tmp_path):
    led = tmp_path / "ledger.jsonl"
    _write_ledger(led, _verdict("behavioral", "red"), _verdict("behavioral", "green"))
    assert read_verdicts(led, "sw-feat").behavioral is GateState.PASS


def test_malformed_lines_are_skipped(tmp_path):
    led = tmp_path / "ledger.jsonl"
    _write_ledger(
        led,
        "not json at all {{{",
        _verdict("deterministic", "green"),
        "",                                  # blank line
        "   ",                               # whitespace-only line
        '["a","list","not","an","object"]',  # valid JSON, wrong shape
        _verdict("craftsmanship", "red"),
    )
    gv = read_verdicts(led, "sw-feat")
    assert gv.deterministic is GateState.PASS
    assert gv.craftsmanship is GateState.FAIL


def test_branch_filtering_ignores_other_tasks(tmp_path):
    led = tmp_path / "ledger.jsonl"
    _write_ledger(
        led,
        _verdict("deterministic", "green", task="sw-feat"),
        _verdict("behavioral", "red", task="sw-other"),   # different task — ignored
    )
    gv = read_verdicts(led, "sw-feat")
    assert gv.deterministic is GateState.PASS
    assert gv.behavioral is GateState.NONE


def test_non_verdict_events_are_ignored(tmp_path):
    led = tmp_path / "ledger.jsonl"
    _write_ledger(
        led,
        {"event": "hand_back", "gate": "behavioral", "task": "sw-feat"},
        {"event": "override", "task": "sw-feat"},
        {"event": "granted", "task": "sw-feat"},
    )
    assert read_verdicts(led, "sw-feat").is_empty()


def test_unknown_result_does_not_overwrite(tmp_path):
    led = tmp_path / "ledger.jsonl"
    _write_ledger(
        led,
        _verdict("deterministic", "green"),
        _verdict("deterministic", "purple"),  # unknown result — must not clobber green
    )
    assert read_verdicts(led, "sw-feat").deterministic is GateState.PASS


def test_unknown_gate_is_ignored(tmp_path):
    led = tmp_path / "ledger.jsonl"
    _write_ledger(led, _verdict("security", "red"))  # not one of the three gates
    assert read_verdicts(led, "sw-feat").is_empty()


def test_absent_file_is_empty(tmp_path):
    assert read_verdicts(tmp_path / "nope.jsonl", "sw-feat").is_empty()


def test_directory_path_is_empty(tmp_path):
    # A directory where a file is expected must not raise.
    assert read_verdicts(tmp_path, "sw-feat").is_empty()


def test_running_never_emitted_in_v0(tmp_path):
    """No ledger event opens a gate, so RUNNING is never produced from ledger data."""
    led = tmp_path / "ledger.jsonl"
    _write_ledger(
        led,
        _verdict("deterministic", "green"),
        _verdict("behavioral", "red"),
        {"event": "hand_back", "gate": "craftsmanship", "task": "sw-feat"},
    )
    gv = read_verdicts(led, "sw-feat")
    assert GateState.RUNNING not in (gv.deterministic, gv.behavioral, gv.craftsmanship)


def test_realistic_ledger_shape_from_ledger_sh(tmp_path):
    """A line exactly as ledger.sh emits it (extra fields present) parses fine."""
    led = tmp_path / "ledger.jsonl"
    _write_ledger(led, {
        "ts": "2026-07-26T10:00:00Z", "repo": "super-worker", "task": "sw-badges",
        "event": "verdict", "gate": "craftsmanship", "result": "red",
        "files": "widgets/sidebar.py | 12 +", "note": "magic number",
    })
    assert read_verdicts(led, "sw-badges").craftsmanship is GateState.FAIL


# ── Path resolution: in-tree vs foreign ─────────────────────────────────────

def test_resolve_in_tree_wins_when_present(tmp_path):
    wt = tmp_path / "wt"
    (wt / ".kinetic").mkdir(parents=True)
    in_tree = wt / ".kinetic" / "ledger.jsonl"
    in_tree.write_text("")
    # Even with a remote that looks foreign, an existing in-tree ledger wins.
    got = resolve_ledger_path(wt, "git@github.com:OasisSecurity/oasis.git", ledger_home=tmp_path / "lh")
    assert got == in_tree


def test_resolve_foreign_when_no_in_tree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    home = tmp_path / "lh"
    got = resolve_ledger_path(wt, "git@github.com:OasisSecurity/oasis.git", ledger_home=home)
    assert got == home / "ledgers" / "oasis.jsonl"


def test_resolve_foreign_no_remote_uses_basename(tmp_path):
    wt = tmp_path / "my-worktree"
    wt.mkdir()
    home = tmp_path / "lh"
    got = resolve_ledger_path(wt, None, ledger_home=home)
    assert got == home / "ledgers" / "my-worktree.jsonl"


def test_resolve_ledger_home_from_env(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    envhome = tmp_path / "envhome"
    monkeypatch.setenv("KINETIC_LEDGER_HOME", str(envhome))
    got = resolve_ledger_path(wt, "https://github.com/o/repo.git")
    assert got == envhome / "ledgers" / "repo.jsonl"


def test_resolve_ledger_home_defaults_under_home(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.delenv("KINETIC_LEDGER_HOME", raising=False)
    got = resolve_ledger_path(wt, "https://github.com/o/repo.git")
    assert got == Path.home() / ".kinetic" / "ledgers" / "repo.jsonl"


@pytest.mark.parametrize("url,slug", [
    ("git@github.com:OasisSecurity/oasis.git", "oasis"),
    ("https://github.com/owner/repo.git", "repo"),
    ("https://github.com/owner/repo", "repo"),
    ("ssh://git@host.com/owner/proj.git", "proj"),
    ("git@github.com:okeidar/super-worker.git", "super-worker"),
    ("", "fallback-dir"),      # empty → basename fallback
    (None, "fallback-dir"),    # missing → basename fallback
])
def test_ledger_slug_forms(url, slug):
    assert ledger_slug(url, "/some/path/fallback-dir") == slug


def test_ledger_path_for_worktree_in_tree(tmp_path):
    wt = tmp_path / "wt"
    (wt / ".kinetic").mkdir(parents=True)
    in_tree = wt / ".kinetic" / "ledger.jsonl"
    in_tree.write_text("")
    # In-tree present ⇒ no git lookup needed, returns the in-tree path.
    assert ledger_path_for_worktree(wt) == in_tree


def test_ledger_path_for_worktree_foreign(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    envhome = tmp_path / "lh"
    monkeypatch.setenv("KINETIC_LEDGER_HOME", str(envhome))
    # No in-tree ledger ⇒ origin URL is consulted; stub it (no real git repo).
    monkeypatch.setattr(
        "super_worker.services.verdict._origin_url",
        lambda p: "git@github.com:OasisSecurity/oasis.git",
    )
    assert ledger_path_for_worktree(wt) == envhome / "ledgers" / "oasis.jsonl"


# ── Markup builders ─────────────────────────────────────────────────────────

def test_tab_badge_empty_when_no_verdicts():
    assert tab_badge_markup(GateVerdicts()) == ""
    assert tab_badge_markup(None) == ""


def test_tab_badge_colored_letter_trigram():
    gv = GateVerdicts(
        deterministic=GateState.PASS,
        behavioral=GateState.NONE,
        craftsmanship=GateState.FAIL,
    )
    tb = tab_badge_markup(gv)
    assert tb == " [green]D[/][dim]B[/][red]C[/]"


def test_tab_badge_shows_all_three_gates_when_any_present():
    gv = GateVerdicts(deterministic=GateState.PASS)
    tb = tab_badge_markup(gv)
    # D present + B/C shown dim (the full gate set is visible once any verdict lands)
    assert "D" in tb and "B" in tb and "C" in tb


def test_sidebar_badges_dual_channel_glyphs():
    gv = GateVerdicts(
        deterministic=GateState.PASS,
        behavioral=GateState.NONE,
        craftsmanship=GateState.FAIL,
    )
    out = sidebar_badges_markup(gv)
    lines = out.split("\n")
    assert len(lines) == 3
    assert "[green]✓[/] deterministic" in lines[0]
    assert "[dim]·[/] behavioral" in lines[1]
    assert "[red]✗[/] craftsmanship" in lines[2]


# ── GateVerdicts helpers ────────────────────────────────────────────────────

def test_is_empty_true_for_all_none():
    assert GateVerdicts().is_empty()


def test_is_empty_false_when_any_set():
    assert not GateVerdicts(behavioral=GateState.PASS).is_empty()


def test_items_canonical_order():
    gates = [g for g, _ in GateVerdicts().items()]
    assert gates == ["deterministic", "behavioral", "craftsmanship"]


def test_gate_verdicts_equality():
    assert GateVerdicts(deterministic=GateState.PASS) == GateVerdicts(deterministic=GateState.PASS)
    assert GateVerdicts(deterministic=GateState.PASS) != GateVerdicts(deterministic=GateState.FAIL)
