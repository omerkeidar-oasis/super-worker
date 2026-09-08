"""Persist and restore the workspace layout across app relaunches.

A single JSON file (alongside the other state in ``STATE_DIR``) records what was
open before — the list of open projects (in order), each project's last-active
worktree tab and session, and the sidebar divider width — so relaunching the app
restores the workspace.

Writes are atomic (tmp file + ``os.replace``). Reads NEVER raise: a missing or
corrupt file yields a fresh empty ``UIState`` rather than bricking startup.
"""

import json
import logging
import os

from pydantic import BaseModel, ConfigDict, Field

from super_worker.constants import STATE_DIR

logger = logging.getLogger(__name__)


class ProjectUIState(BaseModel):
    """Per-project UI state: which worktree tab / session was last active."""

    model_config = ConfigDict(extra="ignore")

    worktree: str | None = None  # last-active worktree name
    session: str | None = None   # last-active session's tmux_session_name


class UIState(BaseModel):
    """Workspace layout persisted between runs."""

    model_config = ConfigDict(extra="ignore")

    open_projects: list[str] = Field(default_factory=list)  # repo roots, in open order
    projects: dict[str, ProjectUIState] = Field(default_factory=dict)  # keyed by repo root
    sidebar_width: int | None = None


def _ui_state_file():
    return STATE_DIR / "ui-state.json"


def load_ui_state() -> UIState:
    """Read the UI-state file. Never raises — missing/corrupt → fresh state."""
    path = _ui_state_file()
    if not path.exists():
        return UIState()
    try:
        return UIState.model_validate(json.loads(path.read_text()))
    except Exception:
        # A malformed ui-state file must not brick startup — start fresh.
        logger.debug("UI-state file corrupt or invalid, starting fresh", exc_info=True)
        return UIState()


def save_ui_state(state: UIState) -> None:
    """Atomically write the UI-state file. Never raises."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _ui_state_file()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(state.model_dump_json(indent=2))
        os.replace(tmp, path)
    except Exception:
        logger.debug("Failed to write UI state", exc_info=True)
