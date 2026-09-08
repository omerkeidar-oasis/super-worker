import fcntl
import json
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path

from super_worker.config import ResolvedConfig, detect_repo_root
from super_worker.constants import STATE_DIR
from super_worker.models import AppState, Session, Worktree
from super_worker.services.tmux import build_process_cmd, build_session_env_cmd, create_session, is_session_alive, respawn_pane, _get_server
from super_worker.services.worktree import discover_worktrees, get_current_branch, prune_git_cache

logger = logging.getLogger(__name__)


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _file_lock(path: Path, exclusive: bool = True):
    """Acquire a file lock (shared or exclusive) with automatic cleanup."""
    lock_file = path.with_suffix(".lock")
    with open(lock_file, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _state_file_for(config: ResolvedConfig) -> Path:
    """Per-repo state file keyed by repo root path hash."""
    return STATE_DIR / f"state-{config.state_hash}.json"


def _migrate_data(data: dict) -> dict:
    """Handle backward-compatible field renames."""
    if "repo_path" in data and "repo_root" not in data:
        data["repo_root"] = data.pop("repo_path")
    return data


def _resolve_state_file(config: ResolvedConfig) -> Path:
    """Pick the state file to read: per-repo, or a matching legacy state.json."""
    state_file = _state_file_for(config)
    legacy_file = STATE_DIR / "state.json"
    if not state_file.exists() and legacy_file.exists():
        try:
            legacy_data = json.loads(legacy_file.read_text())
            legacy_root = legacy_data.get("repo_root") or legacy_data.get("repo_path", "")
            if str(config.repo_root) == legacy_root:
                return legacy_file
        except (json.JSONDecodeError, OSError):
            logger.debug("Failed to read legacy state file, starting fresh")
    return state_file


def _read_state_unlocked(config: ResolvedConfig) -> AppState:
    """Read + validate state (caller holds the lock). Falls back on corruption."""
    state_file = _resolve_state_file(config)
    if not state_file.exists():
        return AppState(
            repo_root=str(config.repo_root),
            worktree_base=str(config.base_dir),
        )

    def _parse(path: Path) -> AppState:
        # Raises on ANY corruption (bad JSON *or* wrong shape, e.g. `{}`).
        return AppState.model_validate(_migrate_data(json.loads(path.read_text())))

    try:
        return _parse(state_file)
    except Exception:
        logger.warning("State file corrupted or invalid, falling back to backup", exc_info=True)
    bak = state_file.with_suffix(".bak")
    if bak.exists():
        try:
            return _parse(bak)
        except Exception:
            logger.warning("Backup also corrupted, starting fresh", exc_info=True)
    return AppState(
        repo_root=str(config.repo_root),
        worktree_base=str(config.base_dir),
    )


def _serialize_state(state: AppState) -> str:
    """Serialize state to JSON, EXCLUDING display-only foreign sessions.

    Foreign (non-sw) sessions are re-discovered live on every scan; persisting
    them would resurrect stale entries and let recovery/dedupe touch sessions
    sw doesn't own. Filter into a plain-dict COPY so the live in-memory Session
    objects are never mutated. The transient ``foreign`` flag is dropped from
    the remaining sessions too, keeping the on-disk format unchanged.
    """
    data = state.model_dump()
    for wt in data.get("worktrees", []):
        kept = []
        for s in wt.get("sessions", []):
            if s.get("foreign", False):
                continue
            s.pop("foreign", None)
            kept.append(s)
        wt["sessions"] = kept
    return json.dumps(data, indent=2)


def _write_state_unlocked(state: AppState, config: ResolvedConfig) -> None:
    """Atomically write state (caller holds the lock)."""
    state_file = _state_file_for(config)
    if state_file.exists():
        shutil.copy2(state_file, state_file.with_suffix(".bak"))
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(_serialize_state(state))
    tmp.rename(state_file)


def load_state(config: ResolvedConfig) -> AppState:
    _ensure_state_dir()
    with _file_lock(_state_file_for(config), exclusive=False):
        return _read_state_unlocked(config)


def save_state(state: AppState, config: ResolvedConfig) -> None:
    _ensure_state_dir()
    with _file_lock(_state_file_for(config)):
        _write_state_unlocked(state, config)


@contextmanager
def mutate_state(config: ResolvedConfig):
    """Load → mutate → save under ONE held exclusive lock.

    Prevents lost updates when multiple `sw` processes touch the same
    project's state (e.g. two `sw new` in parallel, or `sw add` while the
    TUI saves): the read and the write can't interleave with another
    writer's cycle. Yields the freshly-read AppState; mutate it in place.
    """
    _ensure_state_dir()
    with _file_lock(_state_file_for(config)):
        state = _read_state_unlocked(config)
        yield state
        _write_state_unlocked(state, config)


def _conversation_exists(worktree_path: str, session_id: str) -> bool:
    """Does Claude Code have a stored conversation with this id for this cwd?

    Claude Code stores each conversation at
    ``~/.claude/projects/<cwd-with-slashes-as-dashes>/<session_id>.jsonl``.
    A stored session_id can point at NOTHING — e.g. a session that was created
    (``--session-id <uuid>``) but never actually used, so Claude never wrote a
    file. Resuming that with ``--resume <uuid>`` opens an empty conversation
    ("wrong session"); callers should fall back to ``--continue`` instead.
    """
    if not session_id:
        return False
    try:
        munged = str(Path(worktree_path).resolve()).replace("/", "-")
        conv = Path.home() / ".claude" / "projects" / munged / f"{session_id}.jsonl"
        return conv.exists()
    except OSError:
        return False


def remove_worktree_from_state(state: AppState, name: str) -> AppState:
    state.worktrees = [wt for wt in state.worktrees if wt.name != name]
    return state


def remove_session_from_state(state: AppState, worktree_name: str, session_id: str) -> AppState:
    wt = state.get_worktree(worktree_name)
    if wt:
        wt.sessions = [s for s in wt.sessions if s.id != session_id]
    return state


# ── Merge-oriented writers (the TUI MUST use these, never a blind save) ────────
#
# The TUI keeps a long-lived in-memory AppState. Persisting it with a blind
# save_state OVERWRITES the shared state file, silently wiping anything another
# process (e.g. `sw new`/`sw add`) merged in since the TUI last read it. Each
# writer below re-reads the current file under the exclusive lock (mutate_state)
# and applies ONLY its specific delta, so concurrent writers are never lost.
# Foreign (display-only) sessions are never written — mutate_state's freshly-read
# state can't contain them, and _serialize_state filters them regardless.


def add_sessions_to_state_file(
    config: ResolvedConfig, worktree_name: str, sessions: list[Session]
) -> None:
    """Merge-append sessions to a worktree in the shared file (skip dups/foreign)."""
    with mutate_state(config) as state:
        wt = state.get_worktree(worktree_name)
        if wt is None:
            return  # worktree removed elsewhere — don't resurrect it
        existing = {s.tmux_session_name for s in wt.sessions}
        for s in sessions:
            if s.foreign or s.tmux_session_name in existing:
                continue
            wt.sessions.append(s)
            existing.add(s.tmux_session_name)


def add_worktree_to_state_file(config: ResolvedConfig, worktree: Worktree) -> None:
    """Merge-add a worktree (and its non-foreign sessions) to the shared file."""
    with mutate_state(config) as state:
        existing = state.get_worktree(worktree.name)
        if existing is None:
            # New worktree — append it. _serialize_state drops any foreign
            # sessions on write, so the shared object can be appended as-is.
            state.worktrees.append(worktree)
            return
        # Raced with another writer that already added this worktree — merge
        # only our new sessions into the existing entry.
        names = {s.tmux_session_name for s in existing.sessions}
        for s in worktree.sessions:
            if s.foreign or s.tmux_session_name in names:
                continue
            existing.sessions.append(s)
            names.add(s.tmux_session_name)


def update_session_label_in_state_file(
    config: ResolvedConfig, worktree_name: str, session_id: str, label: str
) -> None:
    """Set a session's label in the shared file (found by id)."""
    with mutate_state(config) as state:
        wt = state.get_worktree(worktree_name)
        if wt is None:
            return
        for s in wt.sessions:
            if s.id == session_id:
                s.label = label
                return


def remove_session_from_state_file(
    config: ResolvedConfig, worktree_name: str, session_id: str
) -> None:
    """Remove a session from the shared file, preserving everything else."""
    with mutate_state(config) as state:
        remove_session_from_state(state, worktree_name, session_id)


def remove_worktree_from_state_file(config: ResolvedConfig, name: str) -> None:
    """Remove a worktree from the shared file, preserving everything else."""
    with mutate_state(config) as state:
        remove_worktree_from_state(state, name)


def recover_dead_sessions(state: AppState) -> bool:
    """Recover dead sessions by respawning or recreating with `claude --continue`.

    For claude sessions where the tmux session still exists (remain-on-exit),
    respawn the pane in-place to preserve scrollback. Otherwise, create a new
    session. Dead terminal sessions are dropped (nothing to resume).

    Returns True if any sessions were recovered.
    """
    if state.ui_mode == "fast":
        # Fast mode: panes are ephemeral. Dead panes are cleaned up on next launch.
        return False
    changed = False
    for wt in state.worktrees:
        if not Path(wt.path).exists():
            continue
        alive = []
        dead_claude = []
        dead_other = []
        for s in wt.sessions:
            if s.foreign:
                # sw doesn't own foreign sessions — never respawn/recreate them.
                # Preserve as-is (they're re-discovered on the next scan).
                alive.append(s)
            elif is_session_alive(s.tmux_session_name):
                alive.append(s)
            elif s.session_type == "claude":
                dead_claude.append(s)
            else:
                dead_other.append(s)
        if not dead_claude and not dead_other:
            continue

        logger.info(
            "Recovering dead sessions in worktree",
            extra={"worktree": wt.name, "dead_claude": len(dead_claude), "dead_other": len(dead_other), "alive": len(alive)},
        )

        # Resume dead claude sessions; drop dead terminal sessions (nothing to resume)
        new_sessions = list(alive)
        # Names already handed out this batch (alive + recreated), so a fresh
        # recreate can't collide with a sibling — the bug that produced two
        # sessions sharing one tmux name and resuming the wrong conversation.
        reserved = {s.tmux_session_name for s in alive}
        for s in dead_claude:
            # Decide how to bring THIS session back — per-session, never a
            # blanket --continue (which would give every session in the
            # worktree the same latest conversation).
            sid = s.claude_session_id
            if sid and _conversation_exists(wt.path, sid):
                mode = "resume"        # its conversation exists → --resume <id>
            elif sid:
                mode = "fresh_pinned"  # id but no conversation yet → --session-id <id>
            else:
                mode = "continue"      # legacy session with no id → best-effort --continue

            if mode == "resume":
                process_cmd = build_process_cmd(
                    session_type=s.session_type, skip_permissions=s.skip_permissions,
                    resume=True, session_id=sid,
                )
            elif mode == "fresh_pinned":
                process_cmd = build_process_cmd(
                    session_type=s.session_type, skip_permissions=s.skip_permissions,
                    resume=False, session_id=sid,
                )
            else:
                process_cmd = build_process_cmd(
                    session_type=s.session_type, skip_permissions=s.skip_permissions,
                    resume=True, session_id=None,
                )
            resume_cmd = build_session_env_cmd(s.tmux_session_name, process_cmd)

            # Only respawn in place if this dead name is unique in the batch;
            # a duplicated name must be recreated under a fresh unique name.
            if s.tmux_session_name not in reserved and respawn_pane(s.tmux_session_name, resume_cmd):
                logger.info("Respawned dead pane in-place", extra={"session": s.tmux_session_name})
                reserved.add(s.tmux_session_name)
                new_sessions.append(s)
            else:
                # Session gone entirely (or its name collided) — recreate it
                # under a fresh unique name, in the same mode, preserving its
                # conversation id and skip-permissions choice.
                if mode == "resume":
                    resumed = create_session(
                        wt, label="(resumed)", skip_permissions=s.skip_permissions,
                        resume=True, resume_session_id=sid, reserved_names=reserved,
                    )
                elif mode == "fresh_pinned":
                    resumed = create_session(
                        wt, label="(resumed)", skip_permissions=s.skip_permissions,
                        resume=False, force_session_id=sid, reserved_names=reserved,
                    )
                else:
                    resumed = create_session(
                        wt, label="(resumed)", skip_permissions=s.skip_permissions,
                        resume=True, resume_session_id=None, reserved_names=reserved,
                    )
                reserved.add(resumed.tmux_session_name)
                new_sessions.append(resumed)
        wt.sessions = new_sessions
        changed = True
    return changed


def _ensure_remain_on_exit(state: AppState) -> None:
    """Retroactively set remain-on-exit on all existing tmux sessions."""
    try:
        server = _get_server()
        live = {s.session_name: s for s in server.sessions}
    except Exception:
        return
    for wt in state.worktrees:
        for s in wt.sessions:
            if s.foreign:
                continue  # never mutate options on a session sw doesn't own
            tmux_sess = live.get(s.tmux_session_name)
            if tmux_sess is not None:
                try:
                    tmux_sess.set_option("remain-on-exit", "on")
                except Exception:
                    pass


def ensure_default_worktree(state: AppState, config: ResolvedConfig) -> bool:
    """Ensure the 'main' worktree exists, pointing at the repo root.

    Returns True if a new worktree was created (i.e. state changed).
    Both TUI and fast mode call this; callers can add sessions afterward.
    """
    from super_worker.constants import DEFAULT_WORKTREE_NAME
    from super_worker.models import Worktree

    existing = state.get_worktree(DEFAULT_WORKTREE_NAME)
    if existing:
        existing.branch = get_current_branch(str(config.repo_root))
        return False

    branch = get_current_branch(str(config.repo_root))
    wt = Worktree(name=DEFAULT_WORKTREE_NAME, path=str(config.repo_root), branch=branch)
    state.worktrees.insert(0, wt)
    return True


def dedupe_session_names(state: AppState) -> bool:
    """Give every session a unique tmux_session_name. Returns True if changed.

    Two sessions sharing one name (a past bug) both map to the same tmux
    session, so the preview/resume can't tell them apart. Renaming the
    duplicate is safe: it becomes "not alive", so recovery recreates it under
    the fresh name and resumes its OWN conversation via claude_session_id.
    """
    from super_worker.services.tmux import _worktree_scope, tmux_session_name

    seen: set[str] = set()
    changed = False
    for wt in state.worktrees:
        scope = _worktree_scope(wt)
        for s in wt.sessions:
            if s.foreign:
                # Not sw's session — never rename it (its tmux name is real and
                # owned elsewhere); it also can't be persisted or resumed.
                continue
            if s.tmux_session_name not in seen:
                seen.add(s.tmux_session_name)
                continue
            idx = len(wt.sessions)
            while True:
                cand = tmux_session_name(wt.name, idx, scope)
                if cand not in seen:
                    break
                idx += 1
            logger.info("Renamed duplicate session name %s -> %s", s.tmux_session_name, cand)
            s.tmux_session_name = cand
            seen.add(cand)
            changed = True
    return changed


def adopt_orphans_for_worktree(
    worktree: Worktree, live_names: set[str] | None = None
) -> list[Session]:
    """Adopt LIVE sw-created tmux sessions for ONE worktree back into its list.

    A crash — or the old TUI blind-save that clobbered the shared file — can
    leave a worktree with a live ``sw-<name>-<scope>-*`` tmux session that isn't
    in ``worktree.sessions`` (an "orphan"). Without this, WorktreeTabContent
    spawns a fresh EMPTY session beside the real one. Re-adopt each orphan as a
    real, sw-managed session (``foreign=False`` → it persists and recovers).

    Matching is by tmux name prefix: ``_worktree_scope`` is a hash of the
    worktree PATH, so ``sw-<name>-<scope>-`` uniquely identifies THIS worktree
    and can never collide with a different worktree's sessions. The original
    ``claude_session_id`` is unrecoverable → ``None`` (recovery then falls back
    to ``--continue``, the legacy id-less path). Session type is unknown →
    assumed "claude" (the overwhelmingly common case). Returns the adopted
    sessions (also appended to ``worktree.sessions``).
    """
    from super_worker.services.tmux import (
        TMUX_SESSION_PREFIX, _default_session_label, _worktree_scope,
    )

    if live_names is None:
        try:
            live_names = {s.session_name for s in _get_server().sessions}
        except Exception:
            return []

    scope = _worktree_scope(worktree)
    prefix = f"{TMUX_SESSION_PREFIX}-{worktree.name}-{scope}-"
    tracked = {s.tmux_session_name for s in worktree.sessions}
    adopted: list[Session] = []
    for name in sorted(live_names):
        if not name.startswith(prefix) or name in tracked:
            continue
        sess = Session(
            tmux_session_name=name,
            label=_default_session_label(name),
            session_type="claude",
            claude_session_id=None,
        )
        worktree.sessions.append(sess)
        tracked.add(name)
        adopted.append(sess)
        logger.info("Adopted orphaned sw session %s into worktree %s", name, worktree.name)
    return adopted


def adopt_orphan_sw_sessions(state: AppState) -> list[tuple[str, Session]]:
    """Adopt live-but-untracked sw sessions across ALL worktrees. See
    ``adopt_orphans_for_worktree``. Returns ``(worktree_name, session)`` pairs."""
    try:
        live_names = {s.session_name for s in _get_server().sessions}
    except Exception:
        return []
    result: list[tuple[str, Session]] = []
    for wt in state.worktrees:
        for sess in adopt_orphans_for_worktree(wt, live_names=live_names):
            result.append((wt.name, sess))
    return result


def reconcile_state(state: AppState, config: ResolvedConfig | None = None) -> bool:
    """Prune worktrees whose paths no longer exist, discover new ones. Returns True if changed."""
    changed = dedupe_session_names(state)

    valid_worktrees = []
    for wt in state.worktrees:
        if not Path(wt.path).exists():
            changed = True
            continue
        valid_worktrees.append(wt)
    state.worktrees = valid_worktrees
    prune_git_cache({wt.path for wt in valid_worktrees})

    # Ensure remain-on-exit is set on all existing sessions
    _ensure_remain_on_exit(state)

    # Discover worktrees on disk that aren't in state
    if config is not None:
        known_paths = {wt.path for wt in state.worktrees}
        for wt in discover_worktrees(config):
            if wt.path not in known_paths:
                logger.info("Discovered worktree on disk", extra={"name": wt.name, "path": wt.path})
                state.worktrees.append(wt)
                changed = True

    # Re-adopt live sw sessions that fell out of the file (crash / past clobber)
    # so sessionless worktrees are non-empty before their tab spawns an empty one.
    if adopt_orphan_sw_sessions(state):
        changed = True

    return changed


def _load_registry_json() -> list[str]:
    """Load projects.json with error handling. Returns empty list on any failure."""
    registry_path = STATE_DIR / "projects.json"
    if not registry_path.exists():
        return []
    try:
        return json.loads(registry_path.read_text())
    except (json.JSONDecodeError, TypeError):
        return []


def _normalize_registry(projects: list[str]) -> list[str]:
    """Resolve any worktree paths to their main repo root and deduplicate.

    Paths that don't exist RIGHT NOW are kept as-is: dropping them would
    permanently forget projects on unmounted volumes / disconnected drives.
    """
    seen: list[str] = []
    for p in projects:
        path = Path(p)
        if path.exists():
            try:
                p = str(detect_repo_root(path))
            except RuntimeError:
                continue  # exists but is no longer a git repo — drop
        if p not in seen:
            seen.append(p)
    return seen


def _write_registry(registry_path: Path, projects: list[str]) -> None:
    """Atomic write — a crash mid-write must not empty the project list."""
    tmp = registry_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(projects, indent=2))
    tmp.rename(registry_path)


def update_projects_registry(config: ResolvedConfig) -> None:
    """Track this repo in the global projects registry."""
    _ensure_state_dir()
    registry_path = STATE_DIR / "projects.json"
    with _file_lock(registry_path):
        projects = _load_registry_json()
        # Normalize: resolve worktrees → main repo.
        projects = _normalize_registry(projects)
        repo_str = str(config.repo_root)
        if repo_str not in projects:
            projects.append(repo_str)
        _write_registry(registry_path, projects)


def remove_from_projects_registry(path: str) -> None:
    """Remove a repo path from the global projects registry."""
    _ensure_state_dir()
    registry_path = STATE_DIR / "projects.json"
    with _file_lock(registry_path):
        projects = [p for p in _load_registry_json() if p != path]
        _write_registry(registry_path, projects)


def load_projects_registry() -> list[str]:
    """Load list of known repo paths."""
    registry_path = STATE_DIR / "projects.json"
    try:
        with _file_lock(registry_path, exclusive=False):
            return _load_registry_json()
    except OSError:
        return _load_registry_json()


def load_and_reconcile(config: ResolvedConfig, ui_mode: str = "tui") -> AppState:
    """Load state, register project, reconcile worktrees, recover dead sessions.

    Saves state if any changes were made. Used by both TUI and fast mode
    startup — ``ui_mode`` records which one, and matters: fast mode sets
    ``state.ui_mode = "fast"`` and nothing ever set it back, which
    permanently disabled TUI crash-recovery after a single fast-mode run.
    """
    state = load_state(config)
    changed = False
    if state.ui_mode != ui_mode:
        state.ui_mode = ui_mode
        changed = True
    update_projects_registry(config)
    changed = reconcile_state(state, config) or changed
    changed = recover_dead_sessions(state) or changed
    if changed:
        save_state(state, config)
    return state
