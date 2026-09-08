import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from super_worker.models import AppState, Session, Worktree
from super_worker.services.state import (
    _migrate_data,
    _state_file_for,
    add_sessions_to_state_file,
    add_worktree_to_state_file,
    adopt_orphan_sw_sessions,
    load_projects_registry,
    load_state,
    reconcile_state,
    recover_dead_sessions,
    remove_session_from_state,
    remove_session_from_state_file,
    remove_worktree_from_state,
    remove_worktree_from_state_file,
    save_state,
    update_projects_registry,
    update_session_label_in_state_file,
)


@pytest.fixture()
def _redirect_state_dir(tmp_path, monkeypatch):
    """Redirect STATE_DIR to a temp directory for isolation."""
    state_dir = tmp_path / "sw-state"
    state_dir.mkdir()
    monkeypatch.setattr("super_worker.services.state.STATE_DIR", state_dir)
    return state_dir


class TestMigrateData:
    def test_renames_repo_path_to_repo_root(self):
        data = {"repo_path": "/old/path", "worktree_base": "/wt"}
        migrated = _migrate_data(data)
        assert migrated["repo_root"] == "/old/path"
        assert "repo_path" not in migrated

    def test_repo_root_takes_precedence_over_repo_path(self):
        data = {"repo_root": "/new", "repo_path": "/old", "worktree_base": "/wt"}
        migrated = _migrate_data(data)
        assert migrated["repo_root"] == "/new"


class TestLoadAndSaveState:
    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_load_fresh_state_when_no_file(self, fake_config):
        state = load_state(fake_config)
        assert state.repo_root == str(fake_config.repo_root)
        assert state.worktrees == []

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_save_and_load_roundtrip(self, fake_config):
        wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")
        session = Session(tmux_session_name="sw-feat-0", label="test")
        wt.sessions.append(session)

        state = AppState(
            repo_root=str(fake_config.repo_root),
            worktree_base=str(fake_config.base_dir),
            worktrees=[wt],
        )
        save_state(state, fake_config)

        loaded = load_state(fake_config)
        assert len(loaded.worktrees) == 1
        assert loaded.worktrees[0].name == "feat"
        assert loaded.worktrees[0].sessions[0].label == "test"

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_state_file_is_per_repo(self, fake_config):
        path = _state_file_for(fake_config)
        assert fake_config.state_hash in path.name


class TestRemoveFromState:
    def test_removes_matching_worktree(self):
        wt1 = Worktree(name="a", path="/a", branch="sw-a")
        wt2 = Worktree(name="b", path="/b", branch="sw-b")
        state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt1, wt2])
        result = remove_worktree_from_state(state, "a")
        assert [w.name for w in result.worktrees] == ["b"]

    def test_remove_worktree_no_op_for_missing(self):
        wt = Worktree(name="a", path="/a", branch="sw-a")
        state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt])
        result = remove_worktree_from_state(state, "nonexistent")
        assert len(result.worktrees) == 1

    def test_removes_matching_session(self):
        s1 = Session(id="aaa", tmux_session_name="sw-a-0", label="first")
        s2 = Session(id="bbb", tmux_session_name="sw-a-1", label="second")
        wt = Worktree(name="a", path="/a", branch="sw-a", sessions=[s1, s2])
        state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt])
        result = remove_session_from_state(state, "a", "aaa")
        assert [s.id for s in result.worktrees[0].sessions] == ["bbb"]

    def test_remove_session_no_op_for_missing_worktree(self):
        state = AppState(repo_root="/repo", worktree_base="/wt")
        result = remove_session_from_state(state, "nonexistent", "abc")
        assert result.worktrees == []


class TestReconcileState:
    def test_prunes_missing_paths(self, tmp_path, monkeypatch):
        existing_dir = tmp_path / "existing"
        existing_dir.mkdir()
        wt_existing = Worktree(name="exists", path=str(existing_dir), branch="sw-exists")
        wt_gone = Worktree(name="gone", path="/nonexistent/path", branch="sw-gone")
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[wt_existing, wt_gone],
        )
        monkeypatch.setattr("super_worker.services.state.prune_git_cache", lambda paths: None)
        changed = reconcile_state(state)
        assert changed is True
        assert [w.name for w in state.worktrees] == ["exists"]

    def test_no_change_when_all_exist(self, tmp_path, monkeypatch):
        for name in ("a", "b"):
            (tmp_path / name).mkdir()
        wt1 = Worktree(name="a", path=str(tmp_path / "a"), branch="sw-a")
        wt2 = Worktree(name="b", path=str(tmp_path / "b"), branch="sw-b")
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[wt1, wt2],
        )
        monkeypatch.setattr("super_worker.services.state.prune_git_cache", lambda paths: None)
        assert reconcile_state(state) is False
        assert len(state.worktrees) == 2


class TestProjectsRegistry:
    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_update_load_and_dedup(self, fake_config):
        update_projects_registry(fake_config)
        update_projects_registry(fake_config)  # duplicate
        projects = load_projects_registry()
        assert projects.count(str(fake_config.repo_root)) == 1

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_load_empty_registry(self):
        assert load_projects_registry() == []

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_load_corrupted_registry(self, _redirect_state_dir):
        registry = _redirect_state_dir / "projects.json"
        registry.write_text("not valid json{{{")
        assert load_projects_registry() == []


class TestRecoverDeadSessions:
    def test_no_dead_sessions_is_noop(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        s = Session(tmux_session_name="sw-feat-0", label="alive")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr("super_worker.services.state.is_session_alive", lambda name: True)

        assert recover_dead_sessions(state) is False
        assert state.worktrees[0].sessions[0].tmux_session_name == "sw-feat-0"

    def test_dead_sessions_respawned_in_place(self, tmp_path, monkeypatch):
        """Dead claude sessions with remain-on-exit are respawned in-place."""
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        s = Session(tmux_session_name="sw-feat-0", label="dead-one")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr("super_worker.services.state.is_session_alive", lambda name: False)
        monkeypatch.setattr("super_worker.services.state.respawn_pane", lambda name, cmd: True)

        assert recover_dead_sessions(state) is True
        # Original session preserved (respawned in-place)
        assert state.worktrees[0].sessions[0].tmux_session_name == "sw-feat-0"
        assert state.worktrees[0].sessions[0].label == "dead-one"

    def test_dead_sessions_recreated_when_respawn_fails(self, tmp_path, monkeypatch):
        """When respawn fails (session gone entirely), a new session is created."""
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        s = Session(tmux_session_name="sw-feat-0", label="dead-one")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr("super_worker.services.state.is_session_alive", lambda name: False)
        monkeypatch.setattr("super_worker.services.state.respawn_pane", lambda name, cmd: False)
        created_kwargs = []

        def fake_create(worktree, **kwargs):
            new_s = Session(tmux_session_name="sw-feat-1", label=kwargs.get("label", "new"))
            created_kwargs.append(kwargs)
            return new_s

        monkeypatch.setattr("super_worker.services.state.create_session", fake_create)

        assert recover_dead_sessions(state) is True
        assert state.worktrees[0].sessions[0].label == "(resumed)"
        assert created_kwargs[0]["resume"] is True

    def test_missing_worktree_path_skipped(self, monkeypatch):
        s = Session(tmux_session_name="sw-feat-0", label="dead")
        wt = Worktree(name="feat", path="/nonexistent/path", branch="sw-feat", sessions=[s])
        state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt])

        assert recover_dead_sessions(state) is False
        assert len(state.worktrees[0].sessions) == 1

    def test_alive_sessions_preserved_alongside_recovery(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        alive_s = Session(tmux_session_name="sw-feat-0", label="alive")
        dead_s = Session(tmux_session_name="sw-feat-1", label="dead")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[alive_s, dead_s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr(
            "super_worker.services.state.is_session_alive",
            lambda name: name == "sw-feat-0",
        )
        # Respawn succeeds — dead session kept in-place
        monkeypatch.setattr("super_worker.services.state.respawn_pane", lambda name, cmd: True)

        assert recover_dead_sessions(state) is True
        sessions = state.worktrees[0].sessions
        assert len(sessions) == 2
        assert sessions[0].tmux_session_name == "sw-feat-0"
        assert sessions[1].tmux_session_name == "sw-feat-1"

    def test_dead_terminal_sessions_dropped_without_resume(self, tmp_path, monkeypatch):
        """Dead terminal sessions are dropped — nothing to --continue."""
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        s = Session(tmux_session_name="sw-feat-0", label="my-shell", session_type="terminal")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr("super_worker.services.state.is_session_alive", lambda name: False)
        create_called = []
        monkeypatch.setattr("super_worker.services.state.create_session", lambda *a, **kw: create_called.append(1))

        assert recover_dead_sessions(state) is True
        assert state.worktrees[0].sessions == []
        assert create_called == [], "create_session should not be called for dead terminal sessions"

    def test_mixed_dead_claude_and_terminal(self, tmp_path, monkeypatch):
        """Dead claude sessions get respawned; dead terminal sessions are dropped."""
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        alive_s = Session(tmux_session_name="sw-feat-0", label="alive")
        dead_claude = Session(tmux_session_name="sw-feat-1", label="dead-cc")
        dead_term = Session(tmux_session_name="sw-feat-2", label="dead-term", session_type="terminal")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[alive_s, dead_claude, dead_term])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr(
            "super_worker.services.state.is_session_alive",
            lambda name: name == "sw-feat-0",
        )
        # Respawn succeeds for dead claude session
        monkeypatch.setattr("super_worker.services.state.respawn_pane", lambda name, cmd: True)

        assert recover_dead_sessions(state) is True
        sessions = state.worktrees[0].sessions
        assert len(sessions) == 2
        assert sessions[0].tmux_session_name == "sw-feat-0"  # alive kept
        assert sessions[1].tmux_session_name == "sw-feat-1"  # dead claude respawned


class TestMutateState:
    """mutate_state: load→mutate→save under one lock, reading fresh each time."""

    def test_sequential_mutations_all_persist(self, _redirect_state_dir, fake_config):
        from super_worker.services.state import mutate_state

        # Seed so the file exists.
        save_state(
            AppState(repo_root=str(fake_config.repo_root),
                     worktree_base=str(fake_config.base_dir)),
            fake_config,
        )
        with mutate_state(fake_config) as s:
            s.worktrees.append(Worktree(name="a", path="/tmp/a", branch="sw-a"))
        # A second mutate must SEE the first's write (fresh read) and add to it,
        # not clobber it — this is the lost-update guard.
        with mutate_state(fake_config) as s:
            assert {w.name for w in s.worktrees} == {"a"}
            s.worktrees.append(Worktree(name="b", path="/tmp/b", branch="sw-b"))

        loaded = load_state(fake_config)
        assert {w.name for w in loaded.worktrees} == {"a", "b"}

    def test_mutate_reads_external_change(self, _redirect_state_dir, fake_config):
        """A change written between mutations is visible to the next mutate."""
        from super_worker.services.state import mutate_state

        st = AppState(repo_root=str(fake_config.repo_root),
                      worktree_base=str(fake_config.base_dir),
                      worktrees=[Worktree(name="ext", path="/tmp/ext", branch="sw-ext")])
        save_state(st, fake_config)  # simulate another process's write

        with mutate_state(fake_config) as s:
            assert any(w.name == "ext" for w in s.worktrees), "mutate must read fresh state"
            s.worktrees.append(Worktree(name="mine", path="/tmp/mine", branch="sw-mine"))

        loaded = load_state(fake_config)
        assert {w.name for w in loaded.worktrees} == {"ext", "mine"}


class TestSessionNameDedup:
    """Duplicate tmux session names → wrong-conversation resume. Must be healed."""

    def test_dedupe_renames_duplicates(self, _redirect_state_dir):
        from super_worker.services.state import dedupe_session_names
        # Two sessions in one worktree share a tmux name (the real corruption seen)
        wt = Worktree(name="main", path="/repo/x", branch="main", sessions=[
            Session(tmux_session_name="sw-main-abc-3", label="a", claude_session_id="id-A"),
            Session(tmux_session_name="sw-main-abc-4", label="b", claude_session_id="id-B"),
            Session(tmux_session_name="sw-main-abc-3", label="c", claude_session_id="id-C"),
        ])
        state = AppState(repo_root="/repo/x", worktree_base="/repo", worktrees=[wt])

        assert dedupe_session_names(state) is True
        names = [s.tmux_session_name for s in wt.sessions]
        assert len(set(names)) == 3, "all session names must be unique after dedup"
        # conversation ids are preserved so recovery resumes the right ones
        assert {s.claude_session_id for s in wt.sessions} == {"id-A", "id-B", "id-C"}

    def test_dedupe_noop_when_unique(self, _redirect_state_dir):
        from super_worker.services.state import dedupe_session_names
        wt = Worktree(name="main", path="/repo/y", branch="main", sessions=[
            Session(tmux_session_name="sw-main-def-0", label="a"),
            Session(tmux_session_name="sw-main-def-1", label="b"),
        ])
        state = AppState(repo_root="/repo/y", worktree_base="/repo", worktrees=[wt])
        assert dedupe_session_names(state) is False


class TestFindAvailableNameNoDup:
    def test_avoids_sibling_state_names(self, monkeypatch):
        """A new session must not reuse a name already held by a sibling in state."""
        from unittest.mock import MagicMock
        import super_worker.services.tmux as tm
        server = MagicMock()
        server.sessions = []  # nothing live
        monkeypatch.setattr(tm, "_get_server", lambda: server)

        from super_worker.services.tmux import _find_available_session_name, _worktree_scope, tmux_session_name
        scope = _worktree_scope(Worktree(name="main", path="/r", branch="m"))
        wt = Worktree(name="main", path="/r", branch="m", sessions=[
            Session(tmux_session_name=tmux_session_name("main", 0, scope), label="a"),
            Session(tmux_session_name=tmux_session_name("main", 1, scope), label="b"),
        ])
        name = _find_available_session_name(wt)
        assert name not in {s.tmux_session_name for s in wt.sessions}

    def test_reserved_names_excluded(self, monkeypatch):
        from unittest.mock import MagicMock
        import super_worker.services.tmux as tm
        server = MagicMock(); server.sessions = []
        monkeypatch.setattr(tm, "_get_server", lambda: server)
        from super_worker.services.tmux import _find_available_session_name
        wt = Worktree(name="main", path="/r2", branch="m")
        first = _find_available_session_name(wt)
        second = _find_available_session_name(wt, reserved={first})
        assert second != first


class TestRecoveryNoWrongSession:
    """End-to-end: duplicate names + distinct conversations must recover 1:1."""

    def test_recover_gives_distinct_names_and_ids(self, _redirect_state_dir, monkeypatch, tmp_path):
        import super_worker.services.state as sm

        # All dead; two share a name; three distinct conversation ids.
        repo = tmp_path / "repo-z"
        repo.mkdir()
        wt = Worktree(name="main", path=str(repo), branch="main", sessions=[
            Session(tmux_session_name="sw-main-z-3", label="a", claude_session_id="conv-A"),
            Session(tmux_session_name="sw-main-z-4", label="b", claude_session_id="conv-B"),
            Session(tmux_session_name="sw-main-z-3", label="c", claude_session_id="conv-C"),
        ])
        state = AppState(repo_root=str(repo), worktree_base=str(tmp_path), worktrees=[wt])

        # dedupe first (as reconcile does), then recover
        sm.dedupe_session_names(state)

        monkeypatch.setattr(sm, "is_session_alive", lambda name: False)
        monkeypatch.setattr(sm, "_conversation_exists", lambda path, sid: True)  # ids are valid
        monkeypatch.setattr(sm, "respawn_pane", lambda name, cmd: False)  # force recreate path

        created = []
        def fake_create(worktree, **kw):
            # emulate real create_session naming: honor reserved + resume id
            from super_worker.services.tmux import _find_available_session_name
            reserved = kw.get("reserved_names")
            name = _find_available_session_name(worktree, reserved=reserved)
            s = Session(tmux_session_name=name, label=kw.get("label", "x"),
                        claude_session_id=kw.get("resume_session_id"))
            created.append(s)
            return s
        monkeypatch.setattr(sm, "create_session", fake_create)
        # no live sessions on the mock server for name-finding
        from unittest.mock import MagicMock
        import super_worker.services.tmux as tm
        srv = MagicMock(); srv.sessions = []
        monkeypatch.setattr(tm, "_get_server", lambda: srv)

        assert sm.recover_dead_sessions(state) is True
        names = [s.tmux_session_name for s in wt.sessions]
        ids = [s.claude_session_id for s in wt.sessions]
        assert len(set(names)) == 3, "each recovered session must have a unique tmux name"
        assert set(ids) == {"conv-A", "conv-B", "conv-C"}, "each conversation resumed exactly once"


class TestResumePerSession:
    """Each session resumes ITS OWN conversation — never a blanket --continue,
    which would collapse multiple sessions onto the latest conversation."""

    def test_present_conversation_uses_resume(self, _redirect_state_dir, monkeypatch, tmp_path):
        import super_worker.services.state as sm
        repo = tmp_path / "repo-p"; repo.mkdir()
        wt = Worktree(name="main", path=str(repo), branch="main", sessions=[
            Session(tmux_session_name="sw-main-p-0", label="a", claude_session_id="real-id"),
        ])
        state = AppState(repo_root=str(repo), worktree_base=str(tmp_path), worktrees=[wt])
        monkeypatch.setattr(sm, "is_session_alive", lambda name: False)
        monkeypatch.setattr(sm, "_conversation_exists", lambda path, sid: True)
        cap = {}
        monkeypatch.setattr(sm, "respawn_pane", lambda name, cmd: cap.setdefault("cmd", cmd) or True)
        assert sm.recover_dead_sessions(state) is True
        assert "--resume real-id" in cap["cmd"]
        assert "--continue" not in cap["cmd"]

    def test_id_without_conversation_uses_session_id_not_continue(self, _redirect_state_dir, monkeypatch, tmp_path):
        """A pinned id with no file yet → fresh --session-id <id> (keeps its id,
        no hijacking the latest conversation). Must NOT be --continue."""
        import super_worker.services.state as sm
        repo = tmp_path / "repo-m"; repo.mkdir()
        wt = Worktree(name="main", path=str(repo), branch="main", sessions=[
            Session(tmux_session_name="sw-main-m-0", label="a", claude_session_id="ghost-id"),
        ])
        state = AppState(repo_root=str(repo), worktree_base=str(tmp_path), worktrees=[wt])
        monkeypatch.setattr(sm, "is_session_alive", lambda name: False)
        monkeypatch.setattr(sm, "_conversation_exists", lambda path, sid: False)
        cap = {}
        monkeypatch.setattr(sm, "respawn_pane", lambda name, cmd: cap.setdefault("cmd", cmd) or True)
        assert sm.recover_dead_sessions(state) is True
        assert "--session-id ghost-id" in cap["cmd"]
        assert "--continue" not in cap["cmd"]
        assert "--resume" not in cap["cmd"]

    def test_multiple_sessions_each_resume_their_own(self, _redirect_state_dir, monkeypatch, tmp_path):
        """THE core case: 3 sessions in one worktree, all with real conversations,
        each recovers via --resume of its OWN id — none via --continue."""
        import super_worker.services.state as sm
        repo = tmp_path / "repo-multi"; repo.mkdir()
        wt = Worktree(name="main", path=str(repo), branch="main", sessions=[
            Session(tmux_session_name="sw-main-x-0", label="a", claude_session_id="conv-A"),
            Session(tmux_session_name="sw-main-x-1", label="b", claude_session_id="conv-B"),
            Session(tmux_session_name="sw-main-x-2", label="c", claude_session_id="conv-C"),
        ])
        state = AppState(repo_root=str(repo), worktree_base=str(tmp_path), worktrees=[wt])
        monkeypatch.setattr(sm, "is_session_alive", lambda name: False)
        monkeypatch.setattr(sm, "_conversation_exists", lambda path, sid: True)  # all real
        cmds = []
        monkeypatch.setattr(sm, "respawn_pane", lambda name, cmd: cmds.append(cmd) or True)
        assert sm.recover_dead_sessions(state) is True
        joined = "\n".join(cmds)
        for cid in ("conv-A", "conv-B", "conv-C"):
            assert f"--resume {cid}" in joined, f"{cid} must resume itself"
        assert "--continue" not in joined, "no session may use --continue when it has its own id"

    def test_legacy_no_id_uses_continue(self, _redirect_state_dir, monkeypatch, tmp_path):
        """Only a legacy session with NO stored id may fall back to --continue."""
        import super_worker.services.state as sm
        repo = tmp_path / "repo-legacy"; repo.mkdir()
        wt = Worktree(name="main", path=str(repo), branch="main", sessions=[
            Session(tmux_session_name="sw-main-l-0", label="a", claude_session_id=None),
        ])
        state = AppState(repo_root=str(repo), worktree_base=str(tmp_path), worktrees=[wt])
        monkeypatch.setattr(sm, "is_session_alive", lambda name: False)
        cap = {}
        monkeypatch.setattr(sm, "respawn_pane", lambda name, cmd: cap.setdefault("cmd", cmd) or True)
        assert sm.recover_dead_sessions(state) is True
        assert "--continue" in cap["cmd"]


class TestForeignSessionsExcluded:
    """Foreign (adopted, non-sw) sessions are display-only: never persisted,
    never recovered, never renamed by dedupe."""

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_save_state_excludes_foreign(self, fake_config):
        wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat", sessions=[
            Session(tmux_session_name="sw-feat-0", label="mine"),
            Session(tmux_session_name="ext-claude", label="ext", foreign=True),
        ])
        state = AppState(
            repo_root=str(fake_config.repo_root),
            worktree_base=str(fake_config.base_dir),
            worktrees=[wt],
        )
        save_state(state, fake_config)

        # In-memory objects are untouched (filtered into a copy, not mutated).
        assert len(wt.sessions) == 2

        loaded = load_state(fake_config)
        names = {s.tmux_session_name for s in loaded.worktrees[0].sessions}
        assert names == {"sw-feat-0"}, "foreign session must not be persisted"

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_serialized_state_omits_foreign_field(self, fake_config):
        from super_worker.services.state import _serialize_state
        wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat", sessions=[
            Session(tmux_session_name="sw-feat-0", label="mine"),
        ])
        state = AppState(repo_root="/r", worktree_base="/b", worktrees=[wt])
        # The on-disk shape stays exactly as before — no stray "foreign" key.
        assert '"foreign"' not in _serialize_state(state)

    def test_recover_skips_foreign(self, tmp_path, monkeypatch):
        import super_worker.services.state as sm
        repo = tmp_path / "repo"; repo.mkdir()
        wt = Worktree(name="main", path=str(repo), branch="main", sessions=[
            Session(tmux_session_name="ext-claude", label="ext", foreign=True),
            Session(tmux_session_name="sw-main-0", label="mine", claude_session_id="c0"),
        ])
        state = AppState(repo_root=str(repo), worktree_base=str(tmp_path), worktrees=[wt])
        checked = []
        monkeypatch.setattr(sm, "is_session_alive", lambda name: checked.append(name) or False)
        monkeypatch.setattr(sm, "_conversation_exists", lambda path, sid: True)
        monkeypatch.setattr(sm, "respawn_pane", lambda name, cmd: True)

        sm.recover_dead_sessions(state)

        # The foreign session is never alive-checked, never recovered, and stays.
        assert "ext-claude" not in checked
        assert any(s.tmux_session_name == "ext-claude" and s.foreign for s in wt.sessions)

    def test_dedupe_skips_foreign(self, _redirect_state_dir):
        from super_worker.services.state import dedupe_session_names
        wt = Worktree(name="main", path="/tmp/x", branch="main", sessions=[
            Session(tmux_session_name="ext-claude", label="ext", foreign=True),
            Session(tmux_session_name="ext-claude", label="ext2", foreign=True),
        ])
        state = AppState(repo_root="/r", worktree_base="/b", worktrees=[wt])
        # Two foreign sessions can share a real tmux name — sw must NOT rename them.
        assert dedupe_session_names(state) is False
        assert [s.tmux_session_name for s in wt.sessions] == ["ext-claude", "ext-claude"]


class TestMergeWriters:
    """The TUI's merge-oriented writers apply only their delta under the lock,
    never clobbering another process's concurrent changes to the shared file."""

    def _seed(self, fake_config, sessions):
        wt = Worktree(name="W", path="/tmp/W", branch="sw-W", sessions=sessions)
        save_state(
            AppState(repo_root=str(fake_config.repo_root),
                     worktree_base=str(fake_config.base_dir), worktrees=[wt]),
            fake_config,
        )
        return wt

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_add_session_preserves_concurrent_session(self, fake_config):
        # Another process wrote W with session S.
        self._seed(fake_config, [Session(tmux_session_name="sw-W-x-0", label="S")])
        # The (stale) TUI, unaware of S, adds session T via the merge writer.
        add_sessions_to_state_file(
            fake_config, "W", [Session(tmux_session_name="sw-W-x-1", label="T")]
        )
        names = {s.tmux_session_name for s in load_state(fake_config).worktrees[0].sessions}
        assert names == {"sw-W-x-0", "sw-W-x-1"}, "S must survive — merge, not overwrite"

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_add_session_skips_duplicate(self, fake_config):
        self._seed(fake_config, [Session(tmux_session_name="sw-W-x-0", label="S")])
        add_sessions_to_state_file(
            fake_config, "W", [Session(tmux_session_name="sw-W-x-0", label="dup")]
        )
        sessions = load_state(fake_config).worktrees[0].sessions
        assert len(sessions) == 1 and sessions[0].label == "S"

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_add_session_never_persists_foreign(self, fake_config):
        self._seed(fake_config, [Session(tmux_session_name="sw-W-x-0", label="S")])
        add_sessions_to_state_file(
            fake_config, "W", [Session(tmux_session_name="ext", label="e", foreign=True)]
        )
        names = {s.tmux_session_name for s in load_state(fake_config).worktrees[0].sessions}
        assert names == {"sw-W-x-0"}

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_add_worktree_preserves_others_and_merges_sessions(self, fake_config):
        self._seed(fake_config, [Session(tmux_session_name="sw-W-x-0", label="S")])
        # A brand-new worktree from another action.
        new = Worktree(name="V", path="/tmp/V", branch="sw-V",
                       sessions=[Session(tmux_session_name="sw-V-y-0", label="v0")])
        add_worktree_to_state_file(fake_config, new)
        loaded = load_state(fake_config)
        assert {w.name for w in loaded.worktrees} == {"W", "V"}
        # Re-adding W with an extra session merges (doesn't duplicate W or drop S).
        w2 = Worktree(name="W", path="/tmp/W", branch="sw-W", sessions=[
            Session(tmux_session_name="sw-W-x-0", label="S"),
            Session(tmux_session_name="sw-W-x-1", label="T"),
        ])
        add_worktree_to_state_file(fake_config, w2)
        loaded = load_state(fake_config)
        w = loaded.get_worktree("W")
        assert {s.tmux_session_name for s in w.sessions} == {"sw-W-x-0", "sw-W-x-1"}
        assert len([x for x in loaded.worktrees if x.name == "W"]) == 1

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_remove_session_preserves_concurrent_session(self, fake_config):
        self._seed(fake_config, [
            Session(id="s0", tmux_session_name="sw-W-x-0", label="S"),
            Session(id="s1", tmux_session_name="sw-W-x-1", label="T"),
        ])
        remove_session_from_state_file(fake_config, "W", "s0")
        names = {s.tmux_session_name for s in load_state(fake_config).worktrees[0].sessions}
        assert names == {"sw-W-x-1"}

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_update_label_merges(self, fake_config):
        self._seed(fake_config, [Session(id="s0", tmux_session_name="sw-W-x-0", label="old")])
        update_session_label_in_state_file(fake_config, "W", "s0", "new")
        assert load_state(fake_config).worktrees[0].sessions[0].label == "new"

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_remove_worktree_merges(self, fake_config):
        wt = Worktree(name="W", path="/tmp/W", branch="sw-W")
        wt2 = Worktree(name="V", path="/tmp/V", branch="sw-V")
        save_state(
            AppState(repo_root=str(fake_config.repo_root),
                     worktree_base=str(fake_config.base_dir), worktrees=[wt, wt2]),
            fake_config,
        )
        remove_worktree_from_state_file(fake_config, "W")
        assert {w.name for w in load_state(fake_config).worktrees} == {"V"}


class TestAdoptOrphanSwSessions:
    """Live sw sessions that fell out of state are re-adopted as real sessions."""

    def _mock_live(self, monkeypatch, names):
        import super_worker.services.state as sm
        server = MagicMock()
        server.sessions = [MagicMock(session_name=n) for n in names]
        monkeypatch.setattr(sm, "_get_server", lambda: server)

    def test_adopts_matching_live_session(self, monkeypatch):
        from super_worker.services.tmux import _worktree_scope, tmux_session_name
        wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")  # sessionless
        name = tmux_session_name("feat", 0, _worktree_scope(wt))
        self._mock_live(monkeypatch, [name])
        state = AppState(repo_root="/r", worktree_base="/b", worktrees=[wt])

        adopted = adopt_orphan_sw_sessions(state)

        assert [n for _, n in [(w, s.tmux_session_name) for w, s in adopted]] == [name]
        assert len(wt.sessions) == 1
        s = wt.sessions[0]
        assert s.tmux_session_name == name
        assert s.foreign is False and s.claude_session_id is None

    def test_does_not_adopt_other_worktrees_session(self, monkeypatch):
        from super_worker.services.tmux import _worktree_scope, tmux_session_name
        other = Worktree(name="feat", path="/tmp/OTHER", branch="sw-feat")
        foreign_name = tmux_session_name("feat", 0, _worktree_scope(other))  # different scope
        wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")
        self._mock_live(monkeypatch, [foreign_name])
        state = AppState(repo_root="/r", worktree_base="/b", worktrees=[wt])
        assert adopt_orphan_sw_sessions(state) == []
        assert wt.sessions == []

    def test_does_not_double_count_tracked(self, monkeypatch):
        from super_worker.services.tmux import _worktree_scope, tmux_session_name
        wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")
        name = tmux_session_name("feat", 0, _worktree_scope(wt))
        wt.sessions.append(Session(tmux_session_name=name, label="already"))
        self._mock_live(monkeypatch, [name])
        state = AppState(repo_root="/r", worktree_base="/b", worktrees=[wt])
        assert adopt_orphan_sw_sessions(state) == []
        assert len(wt.sessions) == 1


@pytest.mark.skipif(__import__("shutil").which("tmux") is None, reason="tmux not installed")
class TestAdoptOrphanRealTmux:
    """End-to-end orphan adoption against a REAL tmux on an isolated socket."""

    @pytest.fixture
    def tmux_server(self, monkeypatch):
        import libtmux
        import super_worker.services.tmux as tmux_mod
        server = libtmux.Server(socket_name="sw-test-orphan-adopt")
        monkeypatch.setattr(tmux_mod, "_server", server)
        yield server
        try:
            server.kill()
        except Exception:
            pass

    def test_live_sw_session_adopted_into_sessionless_worktree(self, tmux_server, tmp_path):
        from super_worker.services.tmux import _worktree_scope, tmux_session_name
        wt_dir = tmp_path / "feat"
        wt_dir.mkdir()
        wt = Worktree(name="feat", path=str(wt_dir), branch="sw-feat")  # sessionless in state
        name = tmux_session_name("feat", 0, _worktree_scope(wt))
        tmux_server.new_session(session_name=name, start_directory=str(wt_dir))
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        adopted = adopt_orphan_sw_sessions(state)

        assert len(adopted) == 1
        assert wt.sessions[0].tmux_session_name == name
        assert wt.sessions[0].foreign is False
