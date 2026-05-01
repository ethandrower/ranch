import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react';
import type {
  AgentNote,
  CCProcessState,
  DockerContainer,
  DockerStackSnapshot,
  ProcessSnapshot,
  RunDetail,
  RunRecord,
  RunStatus,
  SessionState,
  TerminalEnv,
  WorktreeBasics,
  WorktreeGitState,
} from '../shared/types.js';
import { Terminal } from './Terminal.js';

const EMPTY_SNAPSHOT: ProcessSnapshot = {
  perAgent: {},
  orphanClaudes: [],
  totalClaudes: 0,
};

const EMPTY_DOCKER: DockerStackSnapshot = {
  available: false,
  perAgent: {},
  shared: [],
};

const SESSION_POLL_MS = 4000;
const PROCESS_POLL_MS = 5000;
const GIT_POLL_MS = 8000;
const DOCKER_POLL_MS = 7000;
const WORKTREE_POLL_MS = 30_000;
const NOTES_POLL_MS = 15_000;

interface AppState {
  status: 'loading' | 'ready' | 'error';
  worktrees: WorktreeBasics[];
  appVersion?: string;
  error?: string;
}

export function App(): JSX.Element {
  const [state, setState] = useState<AppState>({
    status: 'loading',
    worktrees: [],
  });
  const [terminalEnv, setTerminalEnv] = useState<TerminalEnv | null>(null);
  const [processSnapshot, setProcessSnapshot] =
    useState<ProcessSnapshot>(EMPTY_SNAPSHOT);
  const [dockerSnapshot, setDockerSnapshot] =
    useState<DockerStackSnapshot>(EMPTY_DOCKER);
  const [notes, setNotes] = useState<Record<string, AgentNote>>({});
  const [focusedAgent, setFocusedAgent] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(false);
  const [mode, setMode] = useState<'interactive' | 'automated'>('interactive');
  // Per-agent generation counter — bumping it forces the Terminal
  // component to remount, which triggers a fresh tmux attach. Used by
  // the "Restart Claude" action.
  const [terminalGenerations, setTerminalGenerations] = useState<
    Record<string, number>
  >({});

  const bumpTerminalGeneration = useCallback((agent: string) => {
    setTerminalGenerations((prev) => ({
      ...prev,
      [agent]: (prev[agent] ?? 0) + 1,
    }));
  }, []);

  // ─── one-shot loads ────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    void window.ranch.terminal.env().then((env) => {
      if (!cancelled) setTerminalEnv(env);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // ─── worktree list (slow refresh) ──────────────────────────
  useEffect(() => {
    let cancelled = false;
    async function refresh(initial = false): Promise<void> {
      try {
        const worktrees = await window.ranch.worktrees.list();
        if (cancelled) return;
        if (initial) {
          const appVersion = await window.ranch.app.version();
          if (cancelled) return;
          setState({ status: 'ready', worktrees, appVersion });
        } else {
          setState((prev) => ({ ...prev, worktrees }));
        }
      } catch (err) {
        if (!cancelled) {
          setState((prev) => ({
            ...prev,
            status: 'error',
            error: err instanceof Error ? err.message : String(err),
          }));
        }
      }
    }
    void refresh(true);
    const handle = setInterval(() => void refresh(false), WORKTREE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, []);

  // ─── process snapshot (fleet-wide) ─────────────────────────
  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const snap = await window.ranch.worktrees.processSnapshot();
        if (!cancelled) setProcessSnapshot(snap);
      } catch {
        // tmux/ps errors are surfaced indirectly (empty snapshot)
      }
    }
    void refresh();
    const handle = setInterval(refresh, PROCESS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, []);

  // ─── docker snapshot (fleet-wide) ─────────────────────────
  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const snap = await window.ranch.docker.snapshot();
        if (!cancelled) setDockerSnapshot(snap);
      } catch {
        // docker not running / not installed — empty state surfaces it
      }
    }
    void refresh();
    const handle = setInterval(refresh, DOCKER_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, []);

  // ─── notes (slow refresh — operator edits are intentional) ──
  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const all = await window.ranch.notes.getAll();
        if (!cancelled) setNotes(all);
      } catch {
        // ignore
      }
    }
    void refresh();
    const handle = setInterval(refresh, NOTES_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, []);

  // Save a note locally + persist. Optimistic — if the persist fails
  // we'll see it on the next refresh tick.
  const saveNote = useCallback(async (agent: string, label: string) => {
    const trimmed = label.trim();
    setNotes((prev) => {
      const next = { ...prev };
      if (trimmed.length === 0) {
        delete next[agent];
      } else {
        next[agent] = {
          label: trimmed,
          updatedAt: new Date().toISOString(),
        };
      }
      return next;
    });
    try {
      await window.ranch.notes.set(agent, trimmed);
    } catch {
      // server-side fail; next poll will reconcile
    }
  }, []);

  const focusedWorktree =
    focusedAgent !== null
      ? (state.worktrees.find((w) => w.agent === focusedAgent) ?? null)
      : null;

  return (
    <div className="app">
      <header className="app__header">
        <h1>Ranch</h1>
        <ModeTabs mode={mode} onChange={setMode} />
        <span className="app__version">
          {state.appVersion ? `v${state.appVersion}` : ''}
        </span>
        <FleetWarnings snapshot={processSnapshot} />
        {mode === 'interactive' &&
          terminalEnv &&
          !terminalEnv.tmuxAvailable && (
            <span className="app__warn">
              ⚠ tmux not found — install with <code>brew install tmux</code>
            </span>
          )}
        {mode === 'interactive' && (
          <button
            type="button"
            className={`sidebar-toggle${sidebarOpen ? ' sidebar-toggle--open' : ''}`}
            onClick={() => setSidebarOpen((v) => !v)}
            title={sidebarOpen ? 'Hide detail sidebar' : 'Show detail sidebar'}
          >
            {sidebarOpen ? 'Hide details ›' : '‹ Details'}
          </button>
        )}
      </header>
      {mode === 'interactive' ? (
        <div className={`app__body${sidebarOpen ? ' app__body--sidebar' : ''}`}>
          <main className="app__grid">
            {state.status === 'loading' && (
              <p className="placeholder">Loading worktrees…</p>
            )}
            {state.status === 'error' && (
              <p className="placeholder placeholder--error">
                Error: {state.error}
              </p>
            )}
            {state.status === 'ready' &&
              state.worktrees.map((wt) => (
                <AgentCell
                  key={wt.agent}
                  worktree={wt}
                  processState={processSnapshot.perAgent[wt.agent] ?? null}
                  dockerContainers={dockerSnapshot.perAgent[wt.agent] ?? []}
                  note={notes[wt.agent] ?? null}
                  terminalEnv={terminalEnv}
                  focused={focusedAgent === wt.agent}
                  generation={terminalGenerations[wt.agent] ?? 1}
                  onFocus={() => setFocusedAgent(wt.agent)}
                  onSaveNote={(label) => saveNote(wt.agent, label)}
                  onBumpGeneration={() => bumpTerminalGeneration(wt.agent)}
                />
              ))}
          </main>
          {sidebarOpen && (
            <aside className="sidebar">
              {focusedWorktree ? (
                <AgentDetail
                  worktree={focusedWorktree}
                  processState={
                    processSnapshot.perAgent[focusedWorktree.agent] ?? null
                  }
                  dockerContainers={
                    dockerSnapshot.perAgent[focusedWorktree.agent] ?? []
                  }
                  dockerAvailable={dockerSnapshot.available}
                  note={notes[focusedWorktree.agent] ?? null}
                />
              ) : (
                <p className="placeholder">
                  Click a cell to see its detail here.
                </p>
              )}
            </aside>
          )}
        </div>
      ) : (
        <AutomatedView />
      )}
    </div>
  );
}

// ─── Mode tabs ────────────────────────────────────────────────

function ModeTabs({
  mode,
  onChange,
}: {
  mode: 'interactive' | 'automated';
  onChange: (mode: 'interactive' | 'automated') => void;
}): JSX.Element {
  return (
    <div className="mode-tabs" role="tablist">
      <button
        role="tab"
        aria-selected={mode === 'interactive'}
        type="button"
        className={`mode-tab${mode === 'interactive' ? ' mode-tab--active' : ''}`}
        onClick={() => onChange('interactive')}
      >
        Interactive
      </button>
      <button
        role="tab"
        aria-selected={mode === 'automated'}
        type="button"
        className={`mode-tab${mode === 'automated' ? ' mode-tab--active' : ''}`}
        onClick={() => onChange('automated')}
      >
        Automated
      </button>
    </div>
  );
}

// ─── AutomatedView (live data from ~/.ranch/ranch.db) ─────────

const RUNS_POLL_MS = 5000;

const STATUS_ORDER: Record<RunStatus, number> = {
  awaiting_approval: 0,
  blocked: 1,
  working: 2,
  planning: 3,
  queued: 4,
  done: 5,
  stopped: 6,
  abandoned: 7,
  unknown: 8,
};

/**
 * "Active" = still demanding operator attention or actually doing work.
 * Everything else (done / stopped / abandoned) is history.
 */
const ACTIVE_STATUSES: RunStatus[] = [
  'awaiting_approval',
  'blocked',
  'working',
  'planning',
  'queued',
];

type RunFilter = 'active' | 'all';

function AutomatedView(): JSX.Element {
  const [runs, setRuns] = useState<RunRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [filter, setFilter] = useState<RunFilter>('active');
  const [cleaningUp, setCleaningUp] = useState(false);
  const [cleanupResult, setCleanupResult] = useState<string | null>(null);
  const [dispatchOpen, setDispatchOpen] = useState(false);

  async function refreshList(): Promise<void> {
    try {
      const list = await window.ranch.runs.list(100);
      setRuns(list);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const list = await window.ranch.runs.list(100);
        if (!cancelled) {
          setRuns(list);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    }
    void refresh();
    const handle = setInterval(refresh, RUNS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, []);

  const sortedRuns = useMemo(() => {
    if (!runs) return null;
    const filtered =
      filter === 'active'
        ? runs.filter((r) => ACTIVE_STATUSES.includes(r.status))
        : runs;
    return [...filtered].sort((a, b) => {
      const sa = STATUS_ORDER[a.status] ?? 99;
      const sb = STATUS_ORDER[b.status] ?? 99;
      if (sa !== sb) return sa - sb;
      return b.id - a.id;
    });
  }, [runs, filter]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    if (runs) {
      for (const r of runs) c[r.status] = (c[r.status] ?? 0) + 1;
    }
    return c;
  }, [runs]);

  const activeCount = useMemo(() => {
    if (!runs) return 0;
    return runs.filter((r) => ACTIVE_STATUSES.includes(r.status)).length;
  }, [runs]);

  const abandonedCount = useMemo(() => {
    if (!runs) return 0;
    return runs.filter((r) => r.status === 'abandoned').length;
  }, [runs]);

  const totalCount = runs?.length ?? 0;

  async function handleCleanup(): Promise<void> {
    if (cleaningUp) return;
    const ok = window.confirm(
      `Mark ${abandonedCount} abandoned run${abandonedCount === 1 ? '' : 's'} as stopped?\n\n` +
        'This sets state=stopped and exit_reason="cleanup: orchestrator process no longer alive" — ' +
        'rows stay in the DB but are properly classified as historical.',
    );
    if (!ok) return;
    setCleaningUp(true);
    try {
      const n = await window.ranch.runs.cleanupAbandoned();
      setCleanupResult(`Cleaned up ${n} abandoned run${n === 1 ? '' : 's'}.`);
      // Force-refresh the list so the operator sees the change instantly
      const list = await window.ranch.runs.list(100);
      setRuns(list);
      setTimeout(() => setCleanupResult(null), 4000);
    } catch (err) {
      setCleanupResult(
        `Cleanup failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setCleaningUp(false);
    }
  }

  return (
    <div className="automated">
      <div className="automated__top">
        <div className="automated__top-row">
          <h2>Automated runs</h2>
          <div className="automated__top-actions">
            <button
              type="button"
              className="automated__dispatch-btn"
              onClick={() => setDispatchOpen(true)}
              title="Dispatch a new automated run via ranch dispatch"
            >
              + New automated run
            </button>
            {abandonedCount > 0 && (
              <button
                type="button"
                className="automated__cleanup-btn"
                onClick={handleCleanup}
                disabled={cleaningUp}
                title={`Mark ${abandonedCount} abandoned runs as stopped — they're orchestrator processes that died without cleanup.`}
              >
                {cleaningUp
                  ? 'Cleaning up…'
                  : `Clean up ${abandonedCount} abandoned`}
              </button>
            )}
            <div className="automated__filter">
              <button
                type="button"
                className={`automated__filter-btn${filter === 'active' ? ' automated__filter-btn--active' : ''}`}
                onClick={() => setFilter('active')}
              >
                Active · {activeCount}
              </button>
              <button
                type="button"
                className={`automated__filter-btn${filter === 'all' ? ' automated__filter-btn--active' : ''}`}
                onClick={() => setFilter('all')}
              >
                All · {totalCount}
              </button>
            </div>
          </div>
        </div>
        {cleanupResult && (
          <p className="automated__cleanup-result">{cleanupResult}</p>
        )}
        <p className="automated__sub">
          Fire-and-forget Claude SDK sessions managed by the Python
          orchestrator. Each row in this list is one historical{' '}
          <code>ranch dispatch</code> invocation. Lifecycle controls (
          <code>approve</code>, <code>reject</code>, <code>stop</code>) still
          happen via <code>ranch &lt;cmd&gt; &lt;run_id&gt;</code> in your
          terminal — UI buttons coming next.
        </p>
        {filter === 'active' && (
          <p className="automated__hint">
            Showing only <em>active</em> runs (waiting on you, working,
            planning, queued). Toggle <strong>All</strong> to see history,
            including <em>abandoned</em> runs whose orchestrator process is no
            longer alive.
          </p>
        )}
        <div className="automated__counts">
          {Object.entries(counts)
            .sort(
              ([a], [b]) =>
                (STATUS_ORDER[a as RunStatus] ?? 99) -
                (STATUS_ORDER[b as RunStatus] ?? 99),
            )
            .map(([status, n]) => (
              <span
                key={status}
                className={`pill run-pill run-pill--${status}`}
                title={`${n} ${status.replace('_', ' ')}`}
              >
                {statusLabel(status as RunStatus)} · {n}
              </span>
            ))}
        </div>
      </div>

      {error && (
        <p className="placeholder placeholder--error">
          Couldn&apos;t read ~/.ranch/ranch.db: {error}
        </p>
      )}

      {sortedRuns === null && !error && (
        <p className="placeholder">Loading runs…</p>
      )}

      {sortedRuns && sortedRuns.length === 0 && (
        <div className="placeholder">
          {filter === 'active' && totalCount > 0 ? (
            <>
              No active runs right now. {totalCount} historical{' '}
              {totalCount === 1 ? 'run' : 'runs'} in the DB — toggle{' '}
              <strong>All</strong> above to see them.
            </>
          ) : (
            <>
              No automated runs yet. Dispatch one from a terminal:
              <pre className="automated__empty-hint">
                ranch dispatch &lt;agent&gt; --ticket &lt;ID&gt; --brief
                &quot;...&quot;
              </pre>
            </>
          )}
        </div>
      )}

      {sortedRuns && sortedRuns.length > 0 && (
        <div className="automated__cards">
          {sortedRuns.map((run) => (
            <RunCard
              key={run.id}
              run={run}
              selected={selectedId === run.id}
              onSelect={() => setSelectedId(run.id)}
            />
          ))}
        </div>
      )}

      {selectedId !== null && (
        <RunDetailModal
          runId={selectedId}
          onClose={() => setSelectedId(null)}
        />
      )}

      {dispatchOpen && (
        <DispatchModal
          onClose={() => setDispatchOpen(false)}
          onDispatched={() => {
            setDispatchOpen(false);
            void refreshList();
          }}
        />
      )}
    </div>
  );
}

// ─── Dispatch modal ───────────────────────────────────────────

function DispatchModal({
  onClose,
  onDispatched,
}: {
  onClose: () => void;
  onDispatched: (runId?: number) => void;
}): JSX.Element {
  const [agents, setAgents] = useState<string[] | null>(null);
  const [agent, setAgent] = useState<string>('');
  const [ticket, setTicket] = useState('');
  const [brief, setBrief] = useState('');
  const [free, setFree] = useState(false);
  const [autoApprove, setAutoApprove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Pull registered agents so the operator picks from a list, not types
  // a name and risks a typo.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const cfg = await window.ranch.config.get();
        if (cancelled) return;
        const names = cfg.agents.map((a) => a.name);
        setAgents(names);
        if (names[0]) setAgent(names[0]);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function submit(e: FormEvent): Promise<void> {
    e.preventDefault();
    if (busy) return;
    if (!agent || !brief.trim()) {
      setError('agent and brief are required (ticket is optional)');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const trimmedTicket = ticket.trim();
      const result = await window.ranch.runs.dispatch({
        agent,
        ...(trimmedTicket ? { ticket: trimmedTicket } : {}),
        brief: brief.trim(),
        free,
        autoApprove,
      });
      if (!result.ok) {
        const tail = result.output.trim().slice(-400);
        setError(tail || 'dispatch failed (no output)');
        return;
      }
      onDispatched(result.runId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="run-modal__backdrop" onClick={onClose}>
      <div className="run-modal" onClick={(e) => e.stopPropagation()}>
        <header className="run-modal__head">
          <h3>New automated run</h3>
          <button
            type="button"
            className="run-modal__close"
            onClick={onClose}
            disabled={busy}
          >
            ✕
          </button>
        </header>
        <form className="run-modal__body dispatch-form" onSubmit={submit}>
          <label className="dispatch-form__field">
            <span className="dispatch-form__label">Agent</span>
            {agents === null ? (
              <span className="placeholder">loading…</span>
            ) : agents.length === 0 ? (
              <span className="placeholder placeholder--error">
                No agents registered in ~/.ranch/config.toml
              </span>
            ) : (
              <select
                value={agent}
                onChange={(e) => setAgent(e.target.value)}
                disabled={busy}
                className="dispatch-form__input"
              >
                {agents.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            )}
          </label>

          <label className="dispatch-form__field">
            <span className="dispatch-form__label">Ticket (optional)</span>
            <input
              type="text"
              value={ticket}
              onChange={(e) => setTicket(e.target.value)}
              placeholder="ECD-1234 — leave blank for ad-hoc"
              disabled={busy}
              className="dispatch-form__input"
              autoFocus
            />
          </label>

          <label className="dispatch-form__field">
            <span className="dispatch-form__label">Brief</span>
            <textarea
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
              placeholder="What should the agent do? Plain text or markdown."
              disabled={busy}
              rows={6}
              className="dispatch-form__input dispatch-form__input--textarea"
            />
          </label>

          <div className="dispatch-form__toggles">
            <label className="dispatch-form__toggle">
              <input
                type="checkbox"
                checked={free}
                onChange={(e) => setFree(e.target.checked)}
                disabled={busy}
              />
              <span>
                <strong>Free mode</strong> — skip the plan→push checkpoint
                workflow. The brief becomes the whole instruction.
              </span>
            </label>
            <label className="dispatch-form__toggle">
              <input
                type="checkbox"
                checked={autoApprove}
                onChange={(e) => setAutoApprove(e.target.checked)}
                disabled={busy}
              />
              <span>
                <strong>Auto-approve</strong> — auto-approve every checkpoint.
                Use with care; combine with free mode for fully unattended.
              </span>
            </label>
          </div>

          {error && <pre className="dispatch-form__error">{error}</pre>}

          <div className="dispatch-form__buttons">
            <button
              type="button"
              className="run-action"
              onClick={onClose}
              disabled={busy}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="run-action run-action--approve"
              disabled={busy || agents === null || agents.length === 0}
            >
              {busy ? 'Dispatching…' : 'Dispatch'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function RunCard({
  run,
  selected,
  onSelect,
}: {
  run: RunRecord;
  selected: boolean;
  onSelect: () => void;
}): JSX.Element {
  const ageRef = run.endedAt ?? run.startedAt;
  const ageStr = relativeAge(ageRef);
  const actionHint =
    run.status === 'awaiting_approval'
      ? 'click to review →'
      : run.status === 'blocked'
        ? 'needs unblock →'
        : null;
  return (
    <article
      className={`run-card run-card--${run.status}${selected ? ' run-card--selected' : ''}`}
      onClick={onSelect}
    >
      <header className="run-card__head">
        <span className="run-card__agent">{run.agent}</span>
        {run.ticket && <span className="run-card__ticket">{run.ticket}</span>}
        <RunStatusPill status={run.status} rawState={run.rawState} />
        <span className="run-card__id" title={`Run ID ${run.id}`}>
          #{run.id}
        </span>
      </header>
      <p className="run-card__brief">
        {run.brief || <em className="run-card__brief-empty">no brief</em>}
      </p>
      {run.status === 'abandoned' && (
        <p className="run-card__zombie">
          orchestrator state is <code>{run.rawState}</code> but the process{' '}
          {run.pid !== undefined ? (
            <>(PID {run.pid}) is no longer alive</>
          ) : (
            'has no recorded PID'
          )}
          .
        </p>
      )}
      <footer className="run-card__foot">
        <span className="run-card__age">
          {run.endedAt ? `ended ${ageStr} ago` : `started ${ageStr} ago`}
        </span>
        {run.dispatchMode === 'background' && (
          <span className="run-card__mode">bg</span>
        )}
        {run.prUrl && (
          <a
            className="run-card__pr"
            href={run.prUrl}
            onClick={(e) => {
              e.stopPropagation();
              e.preventDefault();
              void window.ranch.app.openExternal(run.prUrl!);
            }}
            title={run.prUrl}
          >
            PR ↗
          </a>
        )}
        {actionHint && (
          <span className="run-card__action-hint">{actionHint}</span>
        )}
      </footer>
    </article>
  );
}

function RunStatusPill({
  status,
  rawState,
}: {
  status: RunStatus;
  rawState?: string;
}): JSX.Element {
  return (
    <span
      className={`pill run-pill run-pill--${status}`}
      title={rawState ? `orchestrator state: ${rawState}` : undefined}
    >
      {statusLabel(status)}
    </span>
  );
}

function statusLabel(status: RunStatus): string {
  switch (status) {
    case 'awaiting_approval':
      return 'needs approval';
    case 'abandoned':
      return 'abandoned';
    case 'unknown':
      return '?';
    default:
      return status;
  }
}

// ─── Run detail modal ─────────────────────────────────────────

function RunDetailModal({
  runId,
  onClose,
}: {
  runId: number;
  onClose: () => void;
}): JSX.Element {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  async function refreshNow(): Promise<void> {
    try {
      const d = await window.ranch.runs.get(runId);
      setDetail(d);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      if (cancelled) return;
      try {
        const d = await window.ranch.runs.get(runId);
        if (!cancelled) {
          setDetail(d);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    }
    void refresh();
    const handle = setInterval(refresh, 4000);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, [runId]);

  function flashAction(msg: string): void {
    setActionMsg(msg);
    setTimeout(() => setActionMsg(null), 3500);
  }

  async function withBusy(
    label: string,
    fn: () => Promise<void>,
  ): Promise<void> {
    setBusy(label);
    try {
      await fn();
      flashAction(`${label} succeeded`);
      await refreshNow();
    } catch (err) {
      flashAction(
        `${label} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setBusy(null);
    }
  }

  async function handleApprove(): Promise<void> {
    const note = window.prompt(
      'Optional note for approval (leave blank to skip):',
      '',
    );
    if (note === null) return; // user cancelled
    await withBusy('Approve', () =>
      window.ranch.runs.approve(runId, note || undefined),
    );
  }

  async function handleReject(): Promise<void> {
    const reason = window.prompt(
      'Reason for rejection (the agent will see this):',
      '',
    );
    if (reason === null) return;
    if (!reason.trim()) {
      flashAction('Reject needs a reason — cancelled.');
      return;
    }
    await withBusy('Reject', () => window.ranch.runs.reject(runId, reason));
  }

  async function handleNote(): Promise<void> {
    const text = window.prompt(
      'Note to send mid-run (the agent receives this on its next tick):',
      '',
    );
    if (text === null) return;
    if (!text.trim()) {
      flashAction('Note text required — cancelled.');
      return;
    }
    await withBusy('Note', () => window.ranch.runs.note(runId, text));
  }

  async function handleStop(): Promise<void> {
    const ok = window.confirm(
      `Stop run #${runId}?\n\n` +
        'This sends a clean stop signal to the orchestrator. ' +
        'Any in-flight tool call may still complete before exit.',
    );
    if (!ok) return;
    await withBusy('Stop', () => window.ranch.runs.stop(runId));
  }

  return (
    <div className="run-modal__backdrop" onClick={onClose}>
      <div className="run-modal" onClick={(e) => e.stopPropagation()}>
        <header className="run-modal__head">
          <h3>Run #{runId}</h3>
          <button type="button" className="run-modal__close" onClick={onClose}>
            ✕
          </button>
        </header>
        {error && (
          <p className="placeholder placeholder--error">Error: {error}</p>
        )}
        {!error && !detail && (
          <p className="placeholder">Loading run detail…</p>
        )}
        {detail && (
          <div className="run-modal__body">
            <DetailSection title="Run">
              <DetailRow label="agent" value={detail.agent} mono />
              {detail.ticket && (
                <DetailRow label="ticket" value={detail.ticket} mono />
              )}
              <DetailRow
                label="status"
                value={`${statusLabel(detail.status)} (${detail.rawState})`}
              />
              <DetailRow label="dispatch" value={detail.dispatchMode} />
              {detail.startedAt && (
                <DetailRow
                  label="started"
                  value={`${relativeAge(detail.startedAt)} ago`}
                />
              )}
              {detail.endedAt && (
                <DetailRow
                  label="ended"
                  value={`${relativeAge(detail.endedAt)} ago`}
                />
              )}
              {detail.branchName && (
                <DetailRow label="branch" value={detail.branchName} mono />
              )}
              {detail.pid !== undefined && (
                <DetailRow label="pid" value={String(detail.pid)} mono />
              )}
              {detail.prUrl && (
                <div className="detail__row">
                  <span className="detail__row-label">PR</span>
                  <a
                    className="detail__pr-link"
                    href={detail.prUrl}
                    onClick={(e) => {
                      e.preventDefault();
                      void window.ranch.app.openExternal(detail.prUrl!);
                    }}
                  >
                    {detail.prUrl} ↗
                  </a>
                </div>
              )}
            </DetailSection>

            {detail.initialPrompt && (
              <DetailSection title="Brief">
                <p className="detail__text">{detail.initialPrompt}</p>
              </DetailSection>
            )}

            <DetailSection
              title="Checkpoints"
              count={detail.checkpoints.length}
            >
              {detail.checkpoints.length === 0 ? (
                <p className="detail__empty">no checkpoints recorded</p>
              ) : (
                <ul className="run-cps">
                  {detail.checkpoints.map((cp) => (
                    <li key={cp.id} className={`run-cp run-cp--${cp.decision}`}>
                      <span className="run-cp__kind">{cp.kind}</span>
                      <span
                        className={`run-cp__decision run-cp__decision--${cp.decision}`}
                      >
                        {cp.decision}
                      </span>
                      {cp.summary && (
                        <span className="run-cp__summary">{cp.summary}</span>
                      )}
                      {cp.decisionNote && (
                        <span className="run-cp__note">
                          — {cp.decisionNote}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </DetailSection>

            {detail.interjections.length > 0 && (
              <DetailSection
                title="Interjections"
                count={detail.interjections.length}
              >
                <ul className="run-cps">
                  {detail.interjections.map((inj) => (
                    <li key={inj.id} className="run-cp">
                      <span className="run-cp__kind">{inj.kind}</span>
                      {inj.processedAt && (
                        <span className="run-cp__decision run-cp__decision--approved">
                          processed
                        </span>
                      )}
                      {inj.content && (
                        <span className="run-cp__summary">{inj.content}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </DetailSection>
            )}

            <DetailSection title="Actions">
              <RunActions
                detail={detail}
                busy={busy}
                onApprove={handleApprove}
                onReject={handleReject}
                onNote={handleNote}
                onStop={handleStop}
              />
              {actionMsg && (
                <p className="run-modal__action-msg">{actionMsg}</p>
              )}
            </DetailSection>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Fleet warnings (orphans) ─────────────────────────────────

function FleetWarnings({
  snapshot,
}: {
  snapshot: ProcessSnapshot;
}): JSX.Element | null {
  if (snapshot.orphanClaudes.length === 0) return null;
  return (
    <span className="app__warn" title={orphanTooltip(snapshot)}>
      ⚠ {snapshot.orphanClaudes.length} orphan claude
      {snapshot.orphanClaudes.length === 1 ? '' : 's'}
    </span>
  );
}

function orphanTooltip(snap: ProcessSnapshot): string {
  return snap.orphanClaudes
    .map((p) => `PID ${p.pid}${p.cwd ? ` · ${p.cwd}` : ''}`)
    .join('\n');
}

// ─── AgentCell — one per worktree, embeds the live terminal ───

interface AgentCellProps {
  worktree: WorktreeBasics;
  processState: CCProcessState | null;
  dockerContainers: DockerContainer[];
  note: AgentNote | null;
  terminalEnv: TerminalEnv | null;
  focused: boolean;
  generation: number;
  onFocus: () => void;
  onSaveNote: (label: string) => void;
  onBumpGeneration: () => void;
}

function AgentCell({
  worktree,
  processState,
  dockerContainers,
  note,
  terminalEnv,
  focused,
  generation,
  onFocus,
  onSaveNote,
  onBumpGeneration,
}: AgentCellProps): JSX.Element {
  const [session, setSession] = useState<SessionState | null>(null);
  const [git, setGit] = useState<WorktreeGitState | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  async function sendCtrlC(): Promise<void> {
    try {
      await window.ranch.terminal.sendKeys(worktree.agent, ['C-c']);
      flashMessage('Ctrl-C sent');
    } catch (err) {
      flashMessage(
        `Failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  async function restartClaude(): Promise<void> {
    const ok = window.confirm(
      `Restart Claude in ranch-${worktree.agent}?\n\n` +
        'This kills the tmux session and creates a fresh one running ' +
        '`claude`. Any unsaved scrollback will be lost.',
    );
    if (!ok) return;
    try {
      await window.ranch.terminal.killSession(worktree.agent);
      onBumpGeneration();
      flashMessage('Restarting…');
    } catch (err) {
      flashMessage(
        `Failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  async function killSession(): Promise<void> {
    const ok = window.confirm(
      `Kill the ranch-${worktree.agent} tmux session?\n\n` +
        'The terminal will be empty until you click "Open Claude" again. ' +
        'Useful when claude AND its host shell are both hung.',
    );
    if (!ok) return;
    try {
      await window.ranch.terminal.killSession(worktree.agent);
      flashMessage('Session killed');
    } catch (err) {
      flashMessage(
        `Failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  function flashMessage(msg: string): void {
    setActionMessage(msg);
    setTimeout(() => setActionMessage(null), 2500);
  }

  // Per-cell transcript polling.
  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const next = await window.ranch.worktrees.session(worktree.agent);
        if (!cancelled) setSession(next);
      } catch {
        // ignore
      }
    }
    void refresh();
    const handle = setInterval(refresh, SESSION_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, [worktree.agent]);

  // Per-cell git polling.
  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const next = await window.ranch.worktrees.git(worktree.agent);
        if (!cancelled) setGit(next);
      } catch {
        // ignore
      }
    }
    void refresh();
    const handle = setInterval(refresh, GIT_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, [worktree.agent]);

  const needsInput =
    (processState?.claudeRunning ?? false) &&
    session?.runState === 'awaiting_input';

  const cellClass = useMemo(() => {
    const c = ['cell'];
    if (focused) c.push('cell--focused');
    if (needsInput) c.push('cell--needs-input');
    return c.join(' ');
  }, [focused, needsInput]);

  return (
    <article
      className={cellClass}
      onMouseDown={onFocus}
      onFocusCapture={onFocus}
    >
      <CellHeader
        worktree={worktree}
        session={session}
        processState={processState}
        dockerContainers={dockerContainers}
        git={git}
        note={note}
        onSaveNote={onSaveNote}
        onSendCtrlC={sendCtrlC}
        onRestartClaude={restartClaude}
        onKillSession={killSession}
      />
      <div className="cell__terminal">
        {terminalEnv && terminalEnv.tmuxAvailable ? (
          <Terminal
            agent={worktree.agent}
            generation={generation}
            onReconnect={onBumpGeneration}
          />
        ) : (
          <p className="placeholder placeholder--center">
            tmux not installed — terminals unavailable
          </p>
        )}
        {actionMessage && (
          <div className="cell__action-flash">{actionMessage}</div>
        )}
      </div>
    </article>
  );
}

// ─── Cell header ──────────────────────────────────────────────

function CellHeader({
  worktree,
  session,
  processState,
  dockerContainers,
  git,
  note,
  onSaveNote,
  onSendCtrlC,
  onRestartClaude,
  onKillSession,
}: {
  worktree: WorktreeBasics;
  session: SessionState | null;
  processState: CCProcessState | null;
  dockerContainers: DockerContainer[];
  git: WorktreeGitState | null;
  note: AgentNote | null;
  onSaveNote: (label: string) => void;
  onSendCtrlC: () => void;
  onRestartClaude: () => void;
  onKillSession: () => void;
}): JSX.Element {
  return (
    <header className="cell__header">
      <div className="cell__top">
        <span className="cell__name">{worktree.agent}</span>
        <SessionPill session={session} processState={processState} />
        <GitInline git={git} session={session} />
        <DockerBadge containers={dockerContainers} />
        <PortsInline ports={worktree.ports} />
        <SessionMenu
          onSendCtrlC={onSendCtrlC}
          onRestartClaude={onRestartClaude}
          onKillSession={onKillSession}
        />
      </div>
      <div className="cell__notes">
        <EditableNote
          agent={worktree.agent}
          note={note}
          onSaveNote={onSaveNote}
        />
        <ActivityLine session={session} />
      </div>
    </header>
  );
}

// ─── Session controls menu (kebab) ────────────────────────────

function SessionMenu({
  onSendCtrlC,
  onRestartClaude,
  onKillSession,
}: {
  onSendCtrlC: () => void;
  onRestartClaude: () => void;
  onKillSession: () => void;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent): void {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open]);

  function run(action: () => void): void {
    setOpen(false);
    action();
  }

  return (
    <div className="session-menu" ref={containerRef}>
      <button
        type="button"
        className="session-menu__trigger"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        title="Session controls"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        ⋯
      </button>
      {open && (
        <div className="session-menu__popover" role="menu">
          <button
            type="button"
            className="session-menu__item"
            onClick={(e) => {
              e.stopPropagation();
              run(onSendCtrlC);
            }}
            title="Send Ctrl-C to interrupt the running command"
          >
            Send Ctrl-C
          </button>
          <button
            type="button"
            className="session-menu__item"
            onClick={(e) => {
              e.stopPropagation();
              run(onRestartClaude);
            }}
            title="Kill tmux session and start a fresh one running claude"
          >
            Restart Claude
          </button>
          <button
            type="button"
            className="session-menu__item session-menu__item--danger"
            onClick={(e) => {
              e.stopPropagation();
              run(onKillSession);
            }}
            title="Kill tmux session entirely (next attach starts fresh)"
          >
            Kill session
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Editable note ────────────────────────────────────────────

function EditableNote({
  agent,
  note,
  onSaveNote,
}: {
  agent: string;
  note: AgentNote | null;
  onSaveNote: (label: string) => void;
}): JSX.Element {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  function startEdit(): void {
    setDraft(note?.label ?? '');
    setEditing(true);
  }

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  function commit(): void {
    onSaveNote(draft);
    setEditing(false);
  }

  function cancel(): void {
    setEditing(false);
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        className="note__input"
        value={draft}
        placeholder={`What is ${agent} working on?`}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit();
          if (e.key === 'Escape') cancel();
        }}
        // Don't let the cell's onMouseDown steal focus.
        onMouseDown={(e) => e.stopPropagation()}
      />
    );
  }

  if (note?.label) {
    return (
      <button
        className="note__display"
        onClick={(e) => {
          e.stopPropagation();
          startEdit();
        }}
        title="Click to edit"
        type="button"
      >
        {note.label}
      </button>
    );
  }

  return (
    <button
      className="note__display note__display--empty"
      onClick={(e) => {
        e.stopPropagation();
        startEdit();
      }}
      type="button"
    >
      + add a note (e.g. {`"working on scrapers tickets"`})
    </button>
  );
}

// ─── Activity line — current tool OR last assistant text ──────

function ActivityLine({
  session,
}: {
  session: SessionState | null;
}): JSX.Element | null {
  if (!session || session.status === 'none') return null;

  if (session.runState === 'tool_in_flight' && session.currentTool) {
    const t = session.currentTool;
    return (
      <p className="cell__activity cell__activity--tool">
        <span className="cell__activity-label">running</span>{' '}
        <code>{t.name}</code>
        {t.summary && <span className="cell__activity-arg">: {t.summary}</span>}
      </p>
    );
  }

  if (session.lastAssistantText) {
    return (
      <p className="cell__activity" title={session.lastAssistantText}>
        {truncate(session.lastAssistantText, 240)}
      </p>
    );
  }

  // Fallback: in-progress todo
  const inProgress = session.todos.find((t) => t.status === 'in_progress');
  if (inProgress) {
    return (
      <p className="cell__activity">
        {truncate(inProgress.activeForm ?? inProgress.content, 240)}
      </p>
    );
  }

  return null;
}

// ─── Compact pieces for the header strip ─────────────────────

function SessionPill({
  session,
  processState,
}: {
  session: SessionState | null;
  processState: CCProcessState | null;
}): JSX.Element {
  const claudeAlive = processState?.claudeRunning ?? false;
  const runState = session?.runState ?? 'unknown';

  if (claudeAlive && runState === 'awaiting_input') {
    return (
      <span
        className="pill pill--awaiting"
        title="Claude is waiting on a human reply"
      >
        needs input
      </span>
    );
  }
  if (claudeAlive && runState === 'tool_in_flight') {
    return (
      <span className="pill pill--running" title="Claude is mid-tool-call">
        working
      </span>
    );
  }
  if (claudeAlive) {
    return <span className="pill pill--running">claude</span>;
  }
  if (!session) return <span className="pill">…</span>;
  if (session.status === 'none') {
    return <span className="pill pill--idle">no session</span>;
  }
  const age = relativeAge(session.lastActivityAt);
  return <span className="pill pill--active">last · {age}</span>;
}

function GitInline({
  git,
  session,
}: {
  git: WorktreeGitState | null;
  session: SessionState | null;
}): JSX.Element | null {
  let branch: string | undefined;
  if (git?.status === 'ok') branch = git.branch;
  else if (session?.gitBranch) branch = session.gitBranch;
  if (!branch) return null;

  const ticket = extractTicketId(branch);
  const dirty = git?.status === 'ok' && git.dirty;
  const ahead = git?.status === 'ok' ? (git.ahead ?? 0) : 0;
  const behind = git?.status === 'ok' ? (git.behind ?? 0) : 0;

  return (
    <span className="git-inline">
      <span className="git-inline__branch">{branch}</span>
      {ticket && <span className="ticket-pill">{ticket}</span>}
      {dirty && (
        <span className="git-inline__dirty" title="uncommitted changes">
          ●
        </span>
      )}
      {(ahead > 0 || behind > 0) && (
        <span
          className="git-inline__ahead-behind"
          title={`${ahead} ahead, ${behind} behind origin/develop`}
        >
          {ahead > 0 && `↑${ahead}`}
          {behind > 0 && `↓${behind}`}
        </span>
      )}
    </span>
  );
}

function DockerBadge({
  containers,
}: {
  containers: DockerContainer[];
}): JSX.Element | null {
  if (containers.length === 0) return null;
  const running = containers.filter((c) => c.state === 'running').length;
  const total = containers.length;
  const allUp = running === total;
  const anyUp = running > 0;
  const cls = allUp
    ? 'docker-badge docker-badge--up'
    : anyUp
      ? 'docker-badge docker-badge--partial'
      : 'docker-badge docker-badge--down';
  return (
    <span className={cls} title={`${running}/${total} containers running`}>
      🐳 {running}/{total}
    </span>
  );
}

function PortsInline({
  ports,
}: {
  ports: WorktreeBasics['ports'];
}): JSX.Element | null {
  const buttons: { label: string; port: number }[] = [];
  if (ports.django !== undefined)
    buttons.push({ label: 'D', port: ports.django });
  if (ports.vite !== undefined) buttons.push({ label: 'V', port: ports.vite });
  if (buttons.length === 0) return null;
  return (
    <span className="ports-inline">
      {buttons.map((b) => (
        <button
          key={b.label}
          className="port-mini"
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            void window.ranch.app.openExternal(`http://localhost:${b.port}`);
          }}
          title={`Open http://localhost:${b.port} in your browser`}
        >
          {b.label}:{b.port}
          <span className="port-mini__arrow">↗</span>
        </button>
      ))}
    </span>
  );
}

// ─── Sidebar AgentDetail ─────────────────────────────────────

function AgentDetail({
  worktree,
  processState,
  dockerContainers,
  dockerAvailable,
  note,
}: {
  worktree: WorktreeBasics;
  processState: CCProcessState | null;
  dockerContainers: DockerContainer[];
  dockerAvailable: boolean;
  note: AgentNote | null;
}): JSX.Element {
  const [session, setSession] = useState<SessionState | null>(null);
  const [git, setGit] = useState<WorktreeGitState | null>(null);
  const [dockerBusy, setDockerBusy] = useState<string | null>(null);
  const [dockerMsg, setDockerMsg] = useState<string | null>(null);

  // Sidebar polls its own copies. We refetch on agent change so switching
  // between cells gets fresh data immediately rather than after the next tick.
  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const [s, g] = await Promise.all([
          window.ranch.worktrees.session(worktree.agent),
          window.ranch.worktrees.git(worktree.agent),
        ]);
        if (!cancelled) {
          setSession(s);
          setGit(g);
        }
      } catch {
        // ignore
      }
    }
    void refresh();
    const handle = setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, [worktree.agent]);

  return (
    <div className="detail">
      <div className="detail__header">
        <span className="detail__name">{worktree.agent}</span>
        {note?.label && <p className="detail__label">{note.label}</p>}
        <button
          type="button"
          className="detail__reveal"
          onClick={() =>
            void window.ranch.app.revealInFinder(worktree.worktreePath)
          }
          title="Reveal worktree in Finder"
        >
          Reveal in Finder
        </button>
      </div>

      <DockerSection
        agent={worktree.agent}
        containers={dockerContainers}
        available={dockerAvailable}
        busy={dockerBusy}
        msg={dockerMsg}
        setBusy={setDockerBusy}
        setMsg={setDockerMsg}
      />

      <DetailSection title="Status">
        <DetailRow label="run state" value={session?.runState ?? '…'} />
        <DetailRow
          label="last activity"
          value={relativeAge(session?.lastActivityAt)}
        />
        <DetailRow
          label="claude PID(s)"
          value={
            processState && processState.claudeProcesses.length > 0
              ? processState.claudeProcesses.map((p) => p.pid).join(', ')
              : '—'
          }
        />
        <DetailRow
          label="tmux"
          value={
            processState?.tmux
              ? `${processState.tmux.sessionName} (${processState.tmux.attachedClients} client${processState.tmux.attachedClients === 1 ? '' : 's'})`
              : '—'
          }
        />
      </DetailSection>

      <DetailSection title="Branch">
        {git?.status === 'ok' ? (
          <>
            <DetailRow label="branch" value={git.branch} mono />
            <DetailRow label="dirty" value={git.dirty ? 'yes' : 'no'} />
            <DetailRow
              label="vs origin/develop"
              value={`↑${git.ahead ?? 0}  ↓${git.behind ?? 0}`}
              mono
            />
            {git.lastCommit && (
              <DetailRow
                label="last commit"
                value={`${git.lastCommit.sha} · ${git.lastCommit.message} · ${git.lastCommit.age}`}
              />
            )}
          </>
        ) : (
          <p className="detail__empty">no git repository</p>
        )}
      </DetailSection>

      <DetailSection title="Ports">
        <DetailRow
          label="source"
          value={
            worktree.portsSource === 'ranch-config'
              ? 'ranch config (canonical)'
              : worktree.portsSource === 'env-agent'
                ? '.env.agent (may drift)'
                : 'unknown'
          }
        />
        {worktree.ports.django !== undefined && (
          <DetailRow
            label="django"
            value={String(worktree.ports.django)}
            mono
          />
        )}
        {worktree.ports.vite !== undefined && (
          <DetailRow label="vite" value={String(worktree.ports.vite)} mono />
        )}
        {worktree.envAgentPath && (
          <DetailRow label=".env.agent" value={worktree.envAgentPath} mono />
        )}
        {!worktree.envAgentMatches && worktree.envAgentName && (
          <p className="detail__warn">
            ⚠ .env.agent says <code>AGENT_NAME={worktree.envAgentName}</code>{' '}
            but this worktree is registered as <code>{worktree.agent}</code>.
            Run <code>make sync-env</code> from citemed_web on develop.
          </p>
        )}
      </DetailSection>

      {processState?.claudeRunning &&
        processState.claudeProcesses.length > 1 && (
          <DetailSection title="⚠ Multiple claude processes">
            <p className="detail__warn">
              {processState.claudeProcesses.length} claude processes detected
              here — duplicate session is likely.
            </p>
            <ul className="detail__pids">
              {processState.claudeProcesses.map((p) => (
                <li key={p.pid}>
                  <code>PID {p.pid}</code>
                  {p.cwd && <span className="detail__path"> · {p.cwd}</span>}
                </li>
              ))}
            </ul>
          </DetailSection>
        )}

      {processState?.claudeRunning && !processState.tmux && (
        <DetailSection title="⚠ Outside ranch">
          <p className="detail__warn">
            Claude is running in this worktree but no{' '}
            <code>ranch-{worktree.agent}</code> tmux session exists. It was
            likely started outside the console.
          </p>
        </DetailSection>
      )}

      <DetailSection title="Todos" count={session?.todos.length ?? 0}>
        {session && session.todos.length > 0 ? (
          <ul className="detail__todos">
            {session.todos.map((t, i) => (
              <li
                key={i}
                className={`detail__todo detail__todo--${t.status.replace('_', '-')}`}
              >
                <span className="detail__todo-icon">
                  {t.status === 'completed'
                    ? '✓'
                    : t.status === 'in_progress'
                      ? '◐'
                      : '○'}
                </span>
                <span className="detail__todo-text">{t.content}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="detail__empty">no todos in this session</p>
        )}
      </DetailSection>

      {session?.lastAssistantText && (
        <DetailSection title="Latest assistant text">
          <p className="detail__text">{session.lastAssistantText}</p>
        </DetailSection>
      )}
    </div>
  );
}

function DockerSection({
  agent,
  containers,
  available,
  busy,
  msg,
  setBusy,
  setMsg,
}: {
  agent: string;
  containers: DockerContainer[];
  available: boolean;
  busy: string | null;
  msg: string | null;
  setBusy: (v: string | null) => void;
  setMsg: (v: string | null) => void;
}): JSX.Element {
  function flash(text: string): void {
    setMsg(text);
    setTimeout(() => setMsg(null), 4000);
  }

  async function withBusy(
    label: string,
    fn: () => Promise<{ ok: boolean; stderr: string; stdout: string }>,
  ): Promise<void> {
    if (busy) return;
    setBusy(label);
    try {
      const result = await fn();
      if (result.ok) {
        flash(`${label} succeeded`);
      } else {
        const tail = (result.stderr || result.stdout || '').trim().slice(-200);
        flash(`${label} failed: ${tail || '(no output)'}`);
      }
    } catch (err) {
      flash(
        `${label} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setBusy(null);
    }
  }

  async function handleUp(): Promise<void> {
    await withBusy('Up', () => window.ranch.docker.up(agent));
  }
  async function handleDown(): Promise<void> {
    if (
      !window.confirm(
        `Bring down docker stack for ${agent}?\n\nRunning containers will be stopped and removed.`,
      )
    )
      return;
    await withBusy('Down', () => window.ranch.docker.down(agent));
  }
  async function handleRestart(): Promise<void> {
    await withBusy('Restart', () => window.ranch.docker.restart(agent));
  }

  const running = containers.filter((c) => c.state === 'running').length;
  const total = containers.length;

  return (
    <DetailSection title={`Docker · ${running}/${total}`}>
      {!available && (
        <p className="detail__warn">
          Docker engine not reachable. Is Docker Desktop running?
        </p>
      )}
      {available && total === 0 && (
        <p className="detail__empty">
          No <code>citemed_{agent}</code> containers. Click <strong>Up</strong>{' '}
          to bring the stack up.
        </p>
      )}
      {total > 0 && (
        <ul className="docker-services">
          {containers.map((c) => (
            <li
              key={c.id}
              className={`docker-service docker-service--${c.state}`}
            >
              <span className="docker-service__name">
                {c.service ?? c.name}
              </span>
              <span
                className={`docker-service__state docker-service__state--${c.state}`}
                title={c.status}
              >
                {c.state}
              </span>
              {c.ports && (
                <span className="docker-service__ports" title={c.ports}>
                  {summarizePortString(c.ports)}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="docker-actions">
        <button
          type="button"
          className="run-action run-action--approve"
          onClick={handleUp}
          disabled={!available || busy !== null}
          title="docker compose up -d (uses .env.agent + docker-compose.agent.yml)"
        >
          {busy === 'Up' ? '…' : 'Up'}
        </button>
        <button
          type="button"
          className="run-action"
          onClick={handleRestart}
          disabled={!available || busy !== null || total === 0}
          title="docker compose restart"
        >
          {busy === 'Restart' ? '…' : 'Restart'}
        </button>
        <button
          type="button"
          className="run-action run-action--stop"
          onClick={handleDown}
          disabled={!available || busy !== null || total === 0}
          title="docker compose down (stops and removes containers)"
        >
          {busy === 'Down' ? '…' : 'Down'}
        </button>
      </div>
      {msg && <p className="run-modal__action-msg">{msg}</p>}
    </DetailSection>
  );
}

/**
 * Boil down docker's `Ports` column to just the host-side bindings —
 * "0.0.0.0:8003->8000/tcp" → "8003".
 */
function summarizePortString(raw: string): string {
  const matches = raw.matchAll(/0\.0\.0\.0:(\d+)->/g);
  const ports = Array.from(matches).map((m) => m[1]!);
  return ports.length > 0 ? ports.join(', ') : raw.slice(0, 30);
}

function DetailSection({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: ReactNode;
}): JSX.Element {
  return (
    <section className="detail__section">
      <h3 className="detail__section-title">
        {title}
        {count !== undefined && count > 0 && (
          <span className="detail__count">{count}</span>
        )}
      </h3>
      {children}
    </section>
  );
}

function DetailRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}): JSX.Element {
  return (
    <div className="detail__row">
      <span className="detail__row-label">{label}</span>
      <span
        className={`detail__row-value${mono ? ' detail__row-value--mono' : ''}`}
      >
        {value}
      </span>
    </div>
  );
}

// ─── Run lifecycle action buttons ─────────────────────────────

function RunActions({
  detail,
  busy,
  onApprove,
  onReject,
  onNote,
  onStop,
}: {
  detail: RunDetail;
  busy: string | null;
  onApprove: () => void;
  onReject: () => void;
  onNote: () => void;
  onStop: () => void;
}): JSX.Element {
  // Whether each action is contextually relevant. Buttons stay visible
  // but are disabled if the run state doesn't make them sensible — that
  // way the operator always sees what's possible, not just what's wired.
  const awaitingApproval = detail.rawState === 'needs_approval';
  const isActive = [
    'planning',
    'in_development',
    'tests_green',
    'needs_approval',
    'queued',
  ].includes(detail.rawState);

  return (
    <div className="run-actions">
      <button
        type="button"
        className="run-action run-action--approve"
        onClick={onApprove}
        disabled={!awaitingApproval || busy !== null}
        title={
          awaitingApproval
            ? 'Approve the current checkpoint (ranch approve)'
            : 'Only available when state = needs_approval'
        }
      >
        {busy === 'Approve' ? '…' : 'Approve'}
      </button>
      <button
        type="button"
        className="run-action run-action--reject"
        onClick={onReject}
        disabled={!awaitingApproval || busy !== null}
        title={
          awaitingApproval
            ? 'Reject the current checkpoint with a reason (ranch reject)'
            : 'Only available when state = needs_approval'
        }
      >
        {busy === 'Reject' ? '…' : 'Reject'}
      </button>
      <button
        type="button"
        className="run-action"
        onClick={onNote}
        disabled={!isActive || busy !== null}
        title="Send a note to the agent mid-run (ranch note)"
      >
        {busy === 'Note' ? '…' : 'Note'}
      </button>
      <button
        type="button"
        className="run-action run-action--stop"
        onClick={onStop}
        disabled={!isActive || busy !== null}
        title="Stop the run cleanly (ranch stop)"
      >
        {busy === 'Stop' ? '…' : 'Stop'}
      </button>
    </div>
  );
}

// ─── helpers ─────────────────────────────────────────────────

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1) + '…';
}

function extractTicketId(branch: string): string | null {
  const m = /([A-Z]{2,}-\d+)/.exec(branch);
  return m ? m[1]! : null;
}

function relativeAge(iso: string | undefined): string {
  if (!iso) return '?';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '?';
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h`;
  const day = Math.round(hr / 24);
  return `${day}d`;
}
