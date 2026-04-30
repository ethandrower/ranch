import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import type {
  AgentNote,
  CCProcessState,
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

const SESSION_POLL_MS = 4000;
const PROCESS_POLL_MS = 5000;
const GIT_POLL_MS = 8000;
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
  const [notes, setNotes] = useState<Record<string, AgentNote>>({});
  const [focusedAgent, setFocusedAgent] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(false);
  const [mode, setMode] = useState<'interactive' | 'automated'>('interactive');

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
                  note={notes[wt.agent] ?? null}
                  terminalEnv={terminalEnv}
                  focused={focusedAgent === wt.agent}
                  onFocus={() => setFocusedAgent(wt.agent)}
                  onSaveNote={(label) => saveNote(wt.agent, label)}
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

  const totalCount = runs?.length ?? 0;

  return (
    <div className="automated">
      <div className="automated__top">
        <div className="automated__top-row">
          <h2>Automated runs</h2>
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

  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
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

            <DetailSection title="Lifecycle (CLI)">
              <p className="detail__empty">
                Approve / reject / stop via terminal until UI buttons land:
              </p>
              <pre className="run-modal__cli">
                ranch approve {runId}
                {'\n'}ranch reject {runId} &quot;reason&quot;
                {'\n'}ranch note {runId} &quot;note text&quot;
                {'\n'}ranch stop {runId}
              </pre>
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
  note: AgentNote | null;
  terminalEnv: TerminalEnv | null;
  focused: boolean;
  onFocus: () => void;
  onSaveNote: (label: string) => void;
}

function AgentCell({
  worktree,
  processState,
  note,
  terminalEnv,
  focused,
  onFocus,
  onSaveNote,
}: AgentCellProps): JSX.Element {
  const [session, setSession] = useState<SessionState | null>(null);
  const [git, setGit] = useState<WorktreeGitState | null>(null);

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
        git={git}
        note={note}
        onSaveNote={onSaveNote}
      />
      <div className="cell__terminal">
        {terminalEnv && terminalEnv.tmuxAvailable ? (
          <Terminal agent={worktree.agent} generation={1} />
        ) : (
          <p className="placeholder placeholder--center">
            tmux not installed — terminals unavailable
          </p>
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
  git,
  note,
  onSaveNote,
}: {
  worktree: WorktreeBasics;
  session: SessionState | null;
  processState: CCProcessState | null;
  git: WorktreeGitState | null;
  note: AgentNote | null;
  onSaveNote: (label: string) => void;
}): JSX.Element {
  return (
    <header className="cell__header">
      <div className="cell__top">
        <span className="cell__name">{worktree.agent}</span>
        <SessionPill session={session} processState={processState} />
        <GitInline git={git} session={session} />
        <PortsInline ports={worktree.ports} />
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
  note,
}: {
  worktree: WorktreeBasics;
  processState: CCProcessState | null;
  note: AgentNote | null;
}): JSX.Element {
  const [session, setSession] = useState<SessionState | null>(null);
  const [git, setGit] = useState<WorktreeGitState | null>(null);

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
