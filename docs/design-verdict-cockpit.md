# Verdict cockpit — super-worker P3.1 design

**Status: draft for Omer's review — design only.** No implementation; this is the deliverable for review (PLAN.md P3.1). Judgment calls are marked **[PROPOSED]** and collected in §6.

Doc home is `docs/` per the brief; sw's own convention is `codegen/` (only `codegen/fast-mode.md` today) — **[PROPOSED]** move here on approval if Omer prefers one place.

## 0 What changes

Today super-worker signals **process state** — is a session running, idle, or blocked on me. This upgrade adds a second, orthogonal signal: **verdict state** — did the task pass its gates. The two coexist; the session dot never changes meaning. sw becomes the render surface for the trust pipeline it already helps run.

Hard rule (PLAN A2): sw is **not a second source of truth**. It *reads* the ledger + results files and *shells* the rollup; it never recomputes trust, never re-derives graduation. Missing a field it needs → propose it back to P0.1, don't invent it locally.

## 1 Data sources (read-only contracts)

| Source | Shape | Fields the cockpit reads |
|---|---|---|
| **Ledger JSONL** (test-strategy §7) | append-only, per-repo | `event ∈ verdict\|hand_back\|agreed\|override\|false_alarm\|escape\|catch\|granted`, `gate ∈ deterministic\|behavioral\|craftsmanship`, `result ∈ green\|red`, `task`, `files`, `attributed_task` (on escape), `ts` |
| **results-v1** (§5) | one JSON/case-run | `suite`, `case_id`, `holdout`, `score` (0–1), `pass`, `reasoning`, `run_id`, `ts`, `model` |
| **Rollup** (`scripts/ledger.sh` → `ledger_report.py`) | derived, rebuildable | per-project `state` (`calibrating`/`TRUSTED (granted)`), `window` (clean-task list), `revoked`, `eligible`, `counts.{escape,override,false_alarm,catch}`, `fa_rate`, `mix` |

**Ledger path per worktree [PROPOSED]:** owned repo → `<worktree.path>/.kinetic/ledger.jsonl`; foreign → `~/.kinetic/ledgers/<slug>.jsonl`. The "owned" test and slug derivation are open in P0.1 §10 — the cockpit keys its watcher on "the worktree's resolved ledger path" and inherits P0.1's answer. **Task key = `models.py:Worktree.branch`** (matches `ledger_report.py`, which defaults `task` to the branch) **[PROPOSED]**.

## 2 The substrate sw already has

Every slice extends one existing mechanism — nothing here is built from scratch:

- **State files + kqueue.** CC hooks (`services/hooks.py:_build_hooks`) write `running|waiting_input|waiting_approval` to `constants.py:SESSION_STATES_DIR` (`~/.config/sw/session-states/<tmux_session_name>`). `services/pane_watcher.py:PaneWatcher.start_watching_state` kqueue-watches each file (`KQ_FILTER_VNODE`+`KQ_NOTE_WRITE`) and fires a callback on write. `services/tmux.py:read_state_file`/`read_all_state_files` parse them into `SessionState`; `has_waiting_approval` is the attention predicate.
- **Render.** `widgets/sidebar.py:SessionSidebar._state_dot` paints the per-session dot; `widgets/project_view.py:ProjectView._tab_label` appends ` 🔔` when `has_waiting_approval`; `on_terminal_pane_state_changed` reacts to a `widgets/terminal_pane.py:TerminalPane.StateChanged` message (posted by its `PaneWatcher` via `_on_state_changed`).
- **Cross-project attention.** `ProjectView.AttentionChanged` bubbles to `app.py:SuperWorkerApp.on_project_view_attention_changed`, which maintains `_attention_paths` and repaints `widgets/project_drawer.py` (`ProjectDrawer`/`ProjectTabBar`). `_periodic_refresh` (every `SIDEBAR_REFRESH_S`) sweeps every open project's `check_attention`.
- **Fleet enumeration.** `services/state.py:load_projects_registry` (`~/.config/sw/projects.json`) is the list of all known projects; `config.py:ResolvedConfig` gives each its `repo_root`.

The verdict layer is a **parallel copy of this exact path**: a ledger/results reader beside `tmux.py`, a `PaneWatcher.start_watching_path` beside `start_watching_state`, a `VerdictChanged` beside `StateChanged`, verdict badges beside the state dot.

## 3 Slices

### (a) Per-session gate badges

- **Shows:** three badges — deterministic · behavioral · craftsmanship — each pass·fail·running, per worktree tab and in its sidebar. Visually distinct from the session dot (a separate glyph row / tab suffix), so "blocked on me" and "gate red" never collide.
- **Data:** latest ledger `verdict` per `gate` for this worktree's `task` (`result` → pass/fail); `running` = a hook-opened gate with no terminal verdict yet **[PROPOSED]**. results-v1 (`suite`, `holdout`, `pass`, `score`) supplies the behavioral tooltip. Per A3, badges are the primary read for `calibrating` projects; a `trusted` project shows the collapsed verdict line only.
- **Hooks:** new `services/verdict.py` mirroring the `tmux.py` readers (`read_verdicts(ledger_path, task) -> GateVerdicts`) + a `GateState` enum beside `SessionState`. Generalize `pane_watcher.py:PaneWatcher.start_watching_state` → `start_watching_path(path, cb)` (identical fd + `KQ_NOTE_WRITE`; JSONL append fires the vnode event exactly as a state write does), wired in `terminal_pane.py` beside `start_watching_states`, posting a new `TerminalPane.VerdictChanged` handled in `project_view.py` beside `on_terminal_pane_state_changed`. Render in `sidebar.py:SessionSidebar.show_worktree` and `project_view.py:_tab_label`.
- **Verify:** append a red `craftsmanship` verdict to a worktree's ledger → its tab badge flips to craftsmanship-fail within one kqueue tick, and the session dot is unchanged.

### (b) Exceptions queue

- **Does:** one ranked cross-session/project "what needs me" list; each row = project · worktree · reason · age, selecting it activates that worktree.
- **Data:** open ledger `verdict result=red` (reds), derived escalations (slice e), and `SessionState.WAITING_APPROVAL` sessions; project `state`/`revoked` from the rollup for tie-breaks.
- **Ranking rule [PROPOSED]:** tier 1 = red gates + escalations (blocking or needs judgment); tier 2 = approvals. Within tier 1, escalations rank above reds (a red may still be inside its hand-back budget and self-resolve; an escalation cannot). Ties: `calibrating` before `trusted`, then oldest `ts` first.
- **Hooks:** new panel widget parallel to `project_drawer.py:ProjectDrawer`, fed by extending `app.py:_periodic_refresh` (already sweeps open projects) with a fleet sweep over `state.py:load_projects_registry`; reuses the `_attention_paths` message plumbing rather than adding a new event loop.
- **Verify:** two projects, each with one open red + one approval → the queue lists all four, both reds/escalations above both approvals.

### (c) Verdict-first preview

- **Does:** activating a flagged worktree shows the **eval delta + judge findings first**; the diff is one keystroke away, not the landing view.
- **Data:** results-v1 `reasoning` (findings), `pass`/`score` per `suite`/`case_id`, delta vs the previous `run_id`/`ts` for the same case; the ledger verdict line as the header.
- **Hooks:** `terminal_pane.py:TerminalPane` — when `active_session`'s worktree is flagged, `watch_active_session` renders a verdict overlay before the live pane; a free key toggles to the diff (**not** Ctrl+R — reserved for CC's transcript per `constants.py` RESERVED_KEYS). Triggered from `project_view.py:_set_active_worktree`/`on_session_selected`; diff via a new `worktree.py` git-diff helper beside `get_worktree_dirty`.
- **Verify:** activate a red worktree → findings show first; the diff key reveals `git diff`; a green worktree drops straight to the live pane.

### (d) Ledger capture keystrokes — **SUPERSEDED (2026-07-27 review decision)**

> Grading moved out of the cockpit entirely: it is **state-driven in the gate flow** — kinetic's `pre-pr` reads the project's trust state and asks the operator for the grade while calibrating (and only on sampled audits after graduation). Rationale: capture's heavy use is transitional (calibration), and sw should carry no feature whose main life is a transition phase; sw stays **display-only** (badges, queue, preview, escalation, rollup). The implementation branch (`program/w3-ledger-capture`, sw PR #2) is parked, not merged. The original slice design is kept below for the record.

- **Does:** one keypress logs exactly one of `agreed | override | false_alarm | escape` by running `scripts/ledger.sh log <event> --task <branch>` with `cwd = worktree.path`, so grading the gate during calibration costs nothing (B5).
- **Data:** **writes** ledger judgment events (§7 enum). No read path, no watcher — this is why it ships alone and first.
- **Hooks:** TUI — a leader key opens a 4-choice capture modal (new `screens.py` ModalScreen beside `CommitMessageScreen`), then a `subprocess.run` in the pattern of `worktree.py:git_create_pr`, `cwd` = `models.py:Worktree.path`. Fast mode — a "Ledger" group in `services/fast_ui.py`'s `display-menu` beside "Git: Open PR" (`fast-git ledger <event>`). Bare single-letter global binds are unsafe (forwarded to the pane), hence the leader+modal **[PROPOSED]**.
- **Verify:** grade a worktree `override` → a new `override` line lands in its ledger with `task` = its branch, and the rollup's clean window resets.

### (e) Escalation attention type

- **Shows:** "agent needs a judgment call" as its **own** alert class, distinct from permission approvals (the existing 🔔).
- **≤2-hand-back source:** derived when a gate accumulates **2 `hand_back` events** for a task with no following green `verdict` (the A6 / test-strategy §1 bound) → escalation. Ledger-driven, *not* a CC hook — so it is fed by the slice-(a) ledger watcher, never the session-state files.
- **Data:** ledger `hand_back` count per `gate`+`task`, minus any clearing `verdict green`.
- **Hooks:** attention today is one bit (`tmux.py:has_waiting_approval` → `_tab_label` 🔔 → `app._attention_paths`). Add an `AttentionKind` (`approval` vs `escalation`); `ProjectView.AttentionChanged` carries the kind; `_attention_paths` becomes kind-keyed; distinct glyph in `_tab_label` and `project_drawer.py`. Forward hook: each escalation row carries its judgment question, later answerable by P4.3 Expert-Me — **not answered here**.
- **Verify:** log a 2nd `hand_back` on one gate/task with no green after → that tab shows the escalation glyph (not 🔔); an independent permission prompt still shows 🔔.

### (f) Batch kickoff + fleet rollup

- **Batch:** new `sw batch` click command beside `cli.py:new` taking N `(name, prompt)` pairs → loops `worktree.py:create_worktree` + `tmux.py:create_session`, persisting under `state.py:mutate_state` (already built for parallel `sw new`). TUI equivalent delegates to `project_view.py:_create_worktree`. Serves B2: kick off several, then live in the alert bar.
- **Fleet view:** per-project trust health, fed by the **rollup** — shell `scripts/ledger.sh report` over every project in `load_projects_registry`, resolving each ledger path, rendering `state` (calibrating/TRUSTED), clean window (`len(window)`/10), open red gates, `escape` count, `fa_rate`. Never recomputed (A2). New view, or a trust-column extension of `project_drawer.py:ProjectDrawer`.
- **Dogfood:** sw is itself an onboarded project — `init-project` adds `.kinetic/` to sw's `.gitignore` (none today). sw's PRs ship through **Gate 1** (its `tests/`) + **Gate 3**, with **Gate 2 = N/A** (A8: a TUI has no clean behavioral surface → 15-task window) **[PROPOSED]**; the cockpit shows sw's own row in the fleet view — it ships through the very gates it displays.
- **Verify:** `sw batch` creates 3 worktrees+sessions in one call; the fleet view shows every project's trust state from the rollup, sw's own row included.

## 4 Implementation order

**(d) → (a) → (e) → (b) → (c) → (f).** Each reuses the prior's plumbing, and (d) delivers value with zero new infra.

- **(d) first** — recommended and matches P3.1's pull-forward note: write-only, no watcher, no reader; makes calibration grading free, so it unblocks P1.4 the moment calibration friction bites.
- **(a)** builds the ledger reader + `start_watching_path` watcher that **(e)** and **(b)** then reuse.
- **(e)** rides (a)'s watcher; **(b)** aggregates (a)+(e) across the fleet.
- **(c)** adds results-v1 reading + a diff view; **(f)** is heaviest (new CLI + rollup shell-out) and is the dogfood proof, so it lands last.

## 5 YAGNI — deliberately NOT built

- **No auto-merge** — the manual click stays everywhere (A7); the cockpit changes what Omer *reads*, never who merges.
- **No authoring gates/cases from the cockpit** — kinetic owns cases, rubric, thresholds; sw only displays verdicts and captures judgments.
- **No reimplemented rollup** — always shell `scripts/ledger.sh`/`ledger_report.py`; never a second source of truth (A2).
- **No Expert-Me answering** — (e) surfaces the escalation class only; answering is P4.3.
- **No new schema** — consume results-v1 + the ledger as-is; a missing field routes back to P0.1.
- **No polling verdict daemon** — reuse kqueue; no new background process.
- **No remote/cross-machine fleet** — the local `projects.json` registry only.
- **No trend charts/history** — current state only; the weekly review (B6) reads the rollup CLI.

## 6 Open questions for Omer

1. **Ledger path resolution** — confirm the per-worktree owned/foreign paths and slug (inherits P0.1 §10); is the watcher keyed on `<worktree>/.kinetic/ledger.jsonl`?
2. **Task key** — is `Worktree.branch` the ledger `task`, or should sw carry an explicit codegen id on `Session`?
3. **Badge policy** — full three-badge row while `calibrating` and collapsed verdict line while `trusted` (per A3) — right split? And the `running` derivation.
4. **Capture keys (d)** — accept a leader-key + modal (single letters are forwarded to the pane), and which leader?
5. **Queue ranking (b)** — accept escalations-above-reds within tier 1, and the calibrating-then-oldest tie-break?
6. **Preview diff key (c)** — which key drops to the diff (Ctrl+R is taken)? Delta vs previous run or vs a frozen baseline?
7. **Fleet refresh (f)** — shelling the rollup across all projects on the 5s timer is costly; prefer event-driven (on ledger write) or on-demand (when the view opens)?
8. **sw's own gates** — confirm Gate 2 = N/A for sw (A8), running only Gates 1+3 on itself.
