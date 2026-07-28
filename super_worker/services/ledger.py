"""Trust-ledger capture (P3.1 verdict cockpit, slice d — write-only).

Shells the trust-ledger CLI to append one judgment event during calibration.
This is the *write* half of the cockpit only: no reader, no watcher, no badge
rendering (those are other slices). It never recomputes trust or parses the
ledger — it appends exactly one event and reports success/failure.

The CLI is resolved from ``ResolvedConfig.ledger_cmd`` (``[ledger] cmd`` in
``.sw.toml``, default ``ledger.sh`` on PATH) so the location of the program's
``scripts/ledger.sh`` stays a per-user config, not a hard-coded path.
"""

import logging
import shlex
import subprocess

logger = logging.getLogger(__name__)

# The four judgment events captured during a calibration read (test-strategy §7).
# verdict/hand_back/catch/granted are logged by gate hooks / the weekly review,
# not by this quick-capture flow, so they are deliberately absent here.
LEDGER_EVENTS: tuple[str, ...] = ("agreed", "override", "false_alarm", "escape")

LEDGER_EVENT_LABELS: dict[str, str] = {
    "agreed": "Agreed — gate passed it correctly",
    "override": "Override — gate missed it (resets clean window)",
    "false_alarm": "False alarm — gate cried wolf",
    "escape": "Escape — defect discovered (attribute to a task)",
}

DEFAULT_LEDGER_CMD = "ledger.sh"

# A ledger append is a couple of `git`/`jq` calls — fast, but bound so a hung
# invocation can never wedge the worker (mirrors worktree.py's git_create_pr).
_LEDGER_TIMEOUT_S = 15.0


def build_ledger_argv(
    ledger_cmd: str,
    event: str,
    task: str,
    note: str | None = None,
    attributed_task: str | None = None,
) -> list[str]:
    """Build the argv for ``<ledger_cmd> log <event> --task <task> [flags]``.

    ``ledger_cmd`` is shell-split so a config value like ``bash /path/ledger.sh``
    or ``scripts/ledger.sh`` works. ``--task`` is always sent explicitly (the
    worktree's branch is sw's authoritative task key, per the design §1) rather
    than relying on the CLI's git-branch default. ``--attributed-task`` is only
    valid for ``escape`` events (the CLI rejects it elsewhere); it defaults to
    the task itself — "I found this while working here" — when not overridden.
    """
    argv = [*shlex.split(ledger_cmd or DEFAULT_LEDGER_CMD), "log", event, "--task", task]
    if note:
        argv += ["--note", note]
    if event == "escape":
        argv += ["--attributed-task", attributed_task or task]
    return argv


def log_ledger_event(
    ledger_cmd: str,
    event: str,
    repo_dir: str,
    task: str,
    note: str | None = None,
    attributed_task: str | None = None,
) -> tuple[bool, str]:
    """Append one judgment event to the ledger of the repo at ``repo_dir``.

    Runs the ledger CLI with ``cwd=repo_dir`` so the script auto-detects the
    right per-repo ledger. Returns ``(ok, message)`` and never raises — a
    missing binary, a timeout, or a non-zero exit all come back as ``(False,
    reason)`` for the caller to surface as a toast. ``message`` on success is
    the CLI's own confirmation line (which names the repo + task + file).
    """
    if event not in LEDGER_EVENTS:
        return False, f"Unknown ledger event: {event!r}"

    argv = build_ledger_argv(ledger_cmd, event, task, note, attributed_task)
    try:
        result = subprocess.run(
            argv, cwd=repo_dir, capture_output=True, text=True, timeout=_LEDGER_TIMEOUT_S,
        )
    except FileNotFoundError:
        return False, (
            f"Ledger command not found: {argv[0]!r}. "
            "Set [ledger] cmd in .sw.toml (e.g. an absolute path to scripts/ledger.sh)."
        )
    except subprocess.TimeoutExpired:
        return False, "Ledger command timed out."
    except OSError as e:  # e.g. non-executable file — must not crash the worker
        return False, f"Ledger command failed to start: {e}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ledger exited non-zero").strip()
        return False, detail[:200]
    return True, (result.stdout or "").strip()[:200]
