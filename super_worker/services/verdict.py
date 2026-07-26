"""Trust-ledger verdict reader (P3.1 verdict cockpit, slice a — read-only).

The *read* half of the cockpit: parse a worktree's trust ledger and project the
latest gate verdicts into badge states. This is a parallel copy of the
``tmux.py`` state-reader path (``read_state_file`` → ``SessionState``): here
``read_verdicts`` → ``GateVerdicts``, watched via ``PaneWatcher`` exactly as
state files are.

Hard rule (PLAN A2): sw is **not a second source of truth**. This module only
*reads* the ledger the gate hooks / ``ledger.sh`` write — it never recomputes
trust, never derives graduation, never writes. A ``verdict`` line is produced by
``ledger.sh log verdict --gate <g> --result <green|red> --task <t>`` (see
design-test-strategy §7); we read the latest one per gate for the worktree's
task and map ``green|red`` → pass/fail.
"""

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# The three gates, in canonical display order (design-test-strategy §1).
GATES: tuple[str, ...] = ("deterministic", "behavioral", "craftsmanship")
_GATES_SET = frozenset(GATES)


class GateState(Enum):
    """Verdict state of a single gate — the parallel of ``SessionState``.

    ``RUNNING`` is part of the design's badge vocabulary (a hook-opened gate
    with no terminal verdict yet, design §3a [PROPOSED] #4) but is **never
    emitted in v0**: the ledger enum has no "gate opened" event, so an in-flight
    gate is not cleanly derivable from ledger data alone. Per the brief, v0
    ships pass/fail/none and leaves ``RUNNING`` reserved for a later slice that
    supplies the open-gate signal. [PROPOSED]
    """

    NONE = "none"
    PASS = "pass"
    FAIL = "fail"
    RUNNING = "running"


# green|red are the only ``result`` values a verdict line carries (§7). Anything
# else (or a missing result) leaves the gate untouched — we never invent a state.
_RESULT_MAP = {"green": GateState.PASS, "red": GateState.FAIL}

# Default trust-ledger home for foreign repos, matching ledger.sh's
# ``LEDGER_HOME="${KINETIC_LEDGER_HOME:-$HOME/.kinetic}"``.
_DEFAULT_LEDGER_DIRNAME = ".kinetic"


@dataclass(frozen=True)
class GateVerdicts:
    """Latest verdict per gate for one worktree's task.

    All-``NONE`` means either no ledger, or a ledger with no verdict yet for this
    task — the render layer treats both the same (badges hidden), so callers do
    not need to distinguish "absent" from "present but empty".
    """

    deterministic: GateState = GateState.NONE
    behavioral: GateState = GateState.NONE
    craftsmanship: GateState = GateState.NONE

    def is_empty(self) -> bool:
        """True when no gate has a verdict — the signal to hide all badges."""
        return all(s is GateState.NONE for s in (self.deterministic, self.behavioral, self.craftsmanship))

    def items(self) -> list[tuple[str, GateState]]:
        """(gate, state) pairs in canonical order."""
        return [
            ("deterministic", self.deterministic),
            ("behavioral", self.behavioral),
            ("craftsmanship", self.craftsmanship),
        ]


# ── Ledger path resolution ────────────────────────────────────────────────────
#
# The cockpit reads whichever file ledger.sh *wrote* (design §1 / test-strategy
# §7): owned repos keep the ledger in-tree at ``<repo>/.kinetic/ledger.jsonl``;
# foreign repos keep it at ``$KINETIC_LEDGER_HOME/ledgers/<slug>.jsonl``. Rather
# than replicate ledger.sh's remote-ownership test (a git call per resolution),
# the reader keys on existence: an in-tree ledger wins if present, else the
# foreign slug path. This is the brief's "in-tree first, else foreign" and
# inherits P0.1 §10's answer for the finer owned/foreign distinction. [PROPOSED]


def ledger_slug(remote_url: str | None, worktree_path: str | os.PathLike) -> str:
    """Repo slug for the foreign ledger filename, mirroring ledger.sh.

    ledger.sh derives the slug from ``git remote get-url origin`` — the repo
    name component of the URL (``owner/repo`` → ``repo``), across https, ssh,
    and scp (``git@host:owner/repo``) forms. With no usable remote it falls back
    to the working-directory basename.
    """
    if remote_url:
        p = remote_url.strip()
        if p.endswith(".git"):
            p = p[:-4]
        if "://" in p:
            p = p.split("://", 1)[1]          # strip scheme://
        if "@" in p:
            p = p.split("@", 1)[1]            # strip user@
        p = p.replace(":", "/", 1)           # scp host:owner/repo -> host/owner/repo
        slug = p.rstrip("/").split("/")[-1]
        if slug:
            return slug
    return Path(worktree_path).name


def resolve_ledger_path(
    worktree_path: str | os.PathLike,
    remote_url: str | None = None,
    ledger_home: str | os.PathLike | None = None,
) -> Path:
    """Resolve a worktree's ledger path — pure (no git, no filesystem writes).

    In-tree ``<worktree>/.kinetic/ledger.jsonl`` wins when it exists; otherwise
    the foreign path ``<ledger_home>/ledgers/<slug>.jsonl``. ``ledger_home``
    defaults to ``$KINETIC_LEDGER_HOME`` then ``~/.kinetic``. Injecting
    ``remote_url``/``ledger_home`` keeps this unit-testable without a git repo.
    """
    in_tree = Path(worktree_path) / _DEFAULT_LEDGER_DIRNAME / "ledger.jsonl"
    if in_tree.exists():
        return in_tree
    if ledger_home is None:
        ledger_home = os.environ.get("KINETIC_LEDGER_HOME") or (Path.home() / _DEFAULT_LEDGER_DIRNAME)
    slug = ledger_slug(remote_url, worktree_path)
    return Path(ledger_home) / "ledgers" / f"{slug}.jsonl"


def ledger_path_for_worktree(worktree_path: str | os.PathLike) -> Path:
    """Convenience resolver used by the UI: resolve, looking up the git remote
    only when needed (i.e. no in-tree ledger). Best-effort — never raises."""
    in_tree = Path(worktree_path) / _DEFAULT_LEDGER_DIRNAME / "ledger.jsonl"
    if in_tree.exists():
        return in_tree
    return resolve_ledger_path(worktree_path, _origin_url(worktree_path))


def _origin_url(worktree_path: str | os.PathLike) -> str | None:
    """Read the ``origin`` remote URL (read-only, best-effort). Returns None on
    any failure — a detached/remoteless/foreign checkout just falls back to the
    directory-basename slug."""
    try:
        import git as gitpython

        repo = gitpython.Repo(worktree_path, search_parent_directories=True)
        try:
            return repo.remotes.origin.url
        except (AttributeError, ValueError):
            remotes = list(repo.remotes)
            return remotes[0].url if remotes else None
    except Exception:
        return None


# ── Ledger reading ──────────────────────────────────────────────────────────


def read_verdicts(ledger_path: str | os.PathLike, task: str) -> GateVerdicts:
    """Latest ``verdict`` per gate for ``task`` in the ledger at ``ledger_path``.

    Append-only JSONL, so the last matching line per gate is the latest. Mirrors
    ``tmux.read_state_file``: pure, no subprocess, swallows I/O errors.

    Robustness contract (design §3a):
      * file absent / unreadable  → all-``NONE`` (badges hidden)
      * malformed / non-JSON line → skipped
      * non-``verdict`` event      → ignored (hand_back/agreed/… are other slices)
      * verdict for another task   → ignored (task = the worktree's branch)
      * unknown / missing result   → the gate is left as-is (never invented)
    """
    try:
        text = Path(ledger_path).read_text()
    except OSError:
        return GateVerdicts()

    latest: dict[str, GateState] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # malformed line — skip, keep scanning
        if not isinstance(obj, dict):
            continue
        if obj.get("event") != "verdict" or obj.get("task") != task:
            continue
        gate = obj.get("gate")
        if gate not in _GATES_SET:
            continue
        state = _RESULT_MAP.get(obj.get("result"))
        if state is None:
            continue  # verdict with no/unknown result — don't overwrite
        latest[gate] = state  # append-only ⇒ last write wins ⇒ latest verdict

    return GateVerdicts(
        deterministic=latest.get("deterministic", GateState.NONE),
        behavioral=latest.get("behavioral", GateState.NONE),
        craftsmanship=latest.get("craftsmanship", GateState.NONE),
    )


# ── Presentation (pure markup builders, kept beside the data for testability) ──
#
# Deliberately distinct from the session-state dot (``sidebar._state_dot`` paints
# a colored ``●``): gate badges use gate LETTERS (tab) and ✓/✗ glyphs (sidebar),
# so "blocked on me" (a dot) and "gate red" (a badge) never read as the same
# mark. Color mirrors the established dot idiom (green=pass, red=fail); the
# sidebar adds a glyph channel so state is legible without color too. [PROPOSED]

_STATE_GLYPH = {
    GateState.PASS: "✓",
    GateState.FAIL: "✗",
    GateState.RUNNING: "◐",
    GateState.NONE: "·",
}
_STATE_COLOR = {
    GateState.PASS: "green",
    GateState.FAIL: "red",
    GateState.RUNNING: "yellow",
    GateState.NONE: "dim",
}
_GATE_LETTER = {"deterministic": "D", "behavioral": "B", "craftsmanship": "C"}


def tab_badge_markup(verdicts: GateVerdicts | None) -> str:
    """Compact colored-letter trigram for a worktree tab label (e.g. a green
    ``D``, dim ``B``, red ``C``). Empty string when there are no verdicts, so a
    non-onboarded worktree's tab is visually unchanged. Leading space separates
    it from the worktree name."""
    if verdicts is None or verdicts.is_empty():
        return ""
    cells = "".join(
        f"[{_STATE_COLOR[state]}]{_GATE_LETTER[gate]}[/]" for gate, state in verdicts.items()
    )
    return f" {cells}"


def sidebar_badges_markup(verdicts: GateVerdicts) -> str:
    """Three labeled, glyph+color badge lines for the sidebar Gates section."""
    return "\n".join(
        f" [{_STATE_COLOR[state]}]{_STATE_GLYPH[state]}[/] {gate}"
        for gate, state in verdicts.items()
    )
