from pathlib import Path

import pytest

from super_worker.config import ResolvedConfig
import super_worker.services.tmux as _tmux_mod


@pytest.fixture(autouse=True)
def _reset_tmux_caches():
    """Reset tmux module-level caches between tests."""
    _tmux_mod._server = None
    _tmux_mod._pane_cache.clear()
    yield
    _tmux_mod._server = None
    _tmux_mod._pane_cache.clear()


@pytest.fixture(autouse=True)
def _isolate_ui_state(tmp_path, monkeypatch):
    """Redirect the workspace UI-state file to a temp dir and reset the
    class-level sidebar width for every test.

    Without this, app-level tests write the real ``~/.config/sw/ui-state.json``
    and leak a restorable project/session into later tests (e.g. an "active
    session" appearing where a test expected none).
    """
    monkeypatch.setattr(
        "super_worker.services.ui_state.STATE_DIR", tmp_path / "sw-ui-state"
    )
    from super_worker.widgets.sidebar import SidebarDivider

    SidebarDivider._shared_width = None
    yield
    SidebarDivider._shared_width = None


@pytest.fixture()
def fake_config(tmp_path: Path) -> ResolvedConfig:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    base_dir = tmp_path / "worktrees"
    base_dir.mkdir()
    return ResolvedConfig(
        repo_root=repo_root,
        worktree_prefix="test-proj",
        branch_prefix="sw-",
        base_dir=base_dir,
        symlinks=[".venv"],
        copies=[],
        post_create_hook="",
        main_branch="main",
        remote="origin",
        commit_placeholder="Brief description",
        name_placeholder="feature-name",
        branch_placeholder="sw-<name>",
    )
