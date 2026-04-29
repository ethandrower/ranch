import { useEffect, useState } from 'react';
import type {
  SessionState,
  TerminalEnv,
  TodoItem,
  WorktreeBasics,
} from '../shared/types.js';
import { Terminal } from './Terminal.js';

const SESSION_POLL_MS = 4000;
const WORKTREE_POLL_MS = 30_000;

interface AppState {
  status: 'loading' | 'ready' | 'error';
  worktrees: WorktreeBasics[];
  appVersion?: string;
  error?: string;
}

interface ActiveTerminal {
  agent: string;
  /** Bumped on each (re-)open to force a fresh attach. */
  generation: number;
}

export function App(): JSX.Element {
  const [state, setState] = useState<AppState>({
    status: 'loading',
    worktrees: [],
  });
  const [activeTerminal, setActiveTerminal] = useState<ActiveTerminal | null>(
    null,
  );
  const [terminalEnv, setTerminalEnv] = useState<TerminalEnv | null>(null);

  useEffect(() => {
    let cancelled = false;
    void window.ranch.terminal.env().then((env) => {
      if (!cancelled) setTerminalEnv(env);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Initial load + slow refresh of worktree basics (.env.agent rarely changes).
  useEffect(() => {
    let cancelled = false;

    async function refreshWorktrees(initial = false): Promise<void> {
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

    void refreshWorktrees(true);
    const handle = setInterval(() => {
      void refreshWorktrees(false);
    }, WORKTREE_POLL_MS);

    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, []);

  function openTerminal(agent: string): void {
    setActiveTerminal((prev) =>
      prev?.agent === agent
        ? { agent, generation: prev.generation + 1 }
        : { agent, generation: 1 },
    );
  }

  function closeTerminal(): void {
    setActiveTerminal(null);
  }

  return (
    <div className="app">
      <header className="app__header">
        <h1>Ranch</h1>
        <span className="app__version">
          {state.appVersion ? `v${state.appVersion}` : ''}
        </span>
      </header>
      <main className="app__layout">
        <section className="pane pane--grid">
          <h2>Worktree grid</h2>
          <Grid
            state={state}
            onOpenTerminal={openTerminal}
            terminalEnv={terminalEnv}
          />
        </section>
        <section className="pane pane--terminal">
          <div className="pane__heading">
            <h2>
              Terminal
              {activeTerminal && (
                <span className="pane__heading-sub">
                  {' '}
                  · ranch-{activeTerminal.agent}
                </span>
              )}
            </h2>
            {activeTerminal && (
              <button className="link-button" onClick={closeTerminal}>
                detach
              </button>
            )}
          </div>
          {activeTerminal ? (
            <Terminal
              key={activeTerminal.agent}
              agent={activeTerminal.agent}
              generation={activeTerminal.generation}
            />
          ) : (
            <p className="placeholder">
              Click <strong>Open terminal</strong> on a worktree card to attach.
            </p>
          )}
        </section>
        <section className="pane pane--inbox">
          <h2>Inbox</h2>
          <p className="placeholder">Empty.</p>
        </section>
        <section className="pane pane--memory">
          <h2>Memory</h2>
          <p className="placeholder">Lessons panel coming in Phase F.</p>
        </section>
      </main>
    </div>
  );
}

function Grid({
  state,
  onOpenTerminal,
  terminalEnv,
}: {
  state: AppState;
  onOpenTerminal: (agent: string) => void;
  terminalEnv: TerminalEnv | null;
}): JSX.Element {
  if (state.status === 'loading') {
    return <p className="placeholder">Loading worktrees…</p>;
  }
  if (state.status === 'error') {
    return (
      <p className="placeholder placeholder--error">Error: {state.error}</p>
    );
  }
  if (state.worktrees.length === 0) {
    return (
      <p className="placeholder">
        No agents registered. Add some to <code>~/.ranch/config.toml</code>.
      </p>
    );
  }
  return (
    <>
      {terminalEnv && !terminalEnv.tmuxAvailable && (
        <p className="card__warn card__warn--banner">
          ⚠ tmux not found on PATH. Install with <code>brew install tmux</code>{' '}
          to enable embedded terminals.
        </p>
      )}
      <div className="grid">
        {state.worktrees.map((wt) => (
          <WorktreeCard
            key={wt.agent}
            worktree={wt}
            onOpenTerminal={onOpenTerminal}
            terminalEnv={terminalEnv}
          />
        ))}
      </div>
    </>
  );
}

interface WorktreeCardProps {
  worktree: WorktreeBasics;
  onOpenTerminal: (agent: string) => void;
  terminalEnv: TerminalEnv | null;
}

function WorktreeCard({
  worktree,
  onOpenTerminal,
  terminalEnv,
}: WorktreeCardProps): JSX.Element {
  const [session, setSession] = useState<SessionState | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);

  // Per-card transcript polling: cheap (file mtime + parse tail) and the
  // freshness here is what makes the card actually useful.
  useEffect(() => {
    let cancelled = false;

    async function refresh(): Promise<void> {
      try {
        const next = await window.ranch.worktrees.session(worktree.agent);
        if (!cancelled) {
          setSession(next);
          setSessionError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setSessionError(err instanceof Error ? err.message : String(err));
        }
      }
    }

    void refresh();
    const handle = setInterval(refresh, SESSION_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(handle);
    };
  }, [worktree.agent]);

  return (
    <article className="card">
      <header className="card__header">
        <span className="card__name">{worktree.agent}</span>
        <SessionPill session={session} error={sessionError} />
      </header>
      {worktree.description && (
        <p className="card__desc">{worktree.description}</p>
      )}
      <p className="card__path">{worktree.worktreePath}</p>
      <BranchRow session={session} />
      <TopicLine session={session} />
      <TodoSummary todos={session?.todos ?? []} />
      <PortsRow ports={worktree.ports} source={worktree.portsSource} />
      <DriftWarnings worktree={worktree} />
      {!worktree.envAgentExists && (
        <p className="card__warn">
          No <code>.env.agent</code> at this worktree.
        </p>
      )}
      <footer className="card__actions">
        <button
          className="card__action"
          onClick={() => onOpenTerminal(worktree.agent)}
          disabled={terminalEnv !== null && !terminalEnv.tmuxAvailable}
          title={
            terminalEnv && !terminalEnv.tmuxAvailable
              ? 'tmux not installed'
              : 'Attach an embedded terminal to this worktree'
          }
        >
          Open terminal
        </button>
      </footer>
    </article>
  );
}

function SessionPill({
  session,
  error,
}: {
  session: SessionState | null;
  error: string | null;
}): JSX.Element {
  if (error) return <span className="pill pill--error">err</span>;
  if (!session) return <span className="pill">…</span>;
  if (session.status === 'none') {
    return <span className="pill pill--idle">no session</span>;
  }
  const age = relativeAge(session.lastActivityAt);
  return <span className="pill pill--active">active · {age}</span>;
}

function BranchRow({
  session,
}: {
  session: SessionState | null;
}): JSX.Element | null {
  const branch = session?.gitBranch;
  if (!branch) return null;
  const ticket = extractTicketId(branch);
  return (
    <p className="card__branch">
      <span className="card__branch-name">{branch}</span>
      {ticket && <span className="card__ticket">{ticket}</span>}
    </p>
  );
}

function TopicLine({
  session,
}: {
  session: SessionState | null;
}): JSX.Element | null {
  if (!session || session.status === 'none') return null;
  const inProgress = session.todos.find((t) => t.status === 'in_progress');
  const topic =
    inProgress?.activeForm ??
    inProgress?.content ??
    session.lastUserPrompt ??
    null;
  if (!topic) return null;
  return <p className="card__topic">{truncate(topic, 140)}</p>;
}

function TodoSummary({ todos }: { todos: TodoItem[] }): JSX.Element | null {
  if (todos.length === 0) return null;
  const done = todos.filter((t) => t.status === 'completed').length;
  const inProgress = todos.filter((t) => t.status === 'in_progress').length;
  return (
    <div className="card__todos">
      <div className="card__todos-summary">
        <strong>
          {done} / {todos.length}
        </strong>{' '}
        complete
        {inProgress > 0 && <span> · {inProgress} in progress</span>}
      </div>
      <ul className="todo-list">
        {todos.map((t, i) => (
          <li
            key={i}
            className={`todo todo--${t.status.replace('_', '-')}`}
            title={t.content}
          >
            <span className="todo__icon">{todoIcon(t.status)}</span>
            <span className="todo__text">{t.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function PortsRow({
  ports,
  source,
}: {
  ports: WorktreeBasics['ports'];
  source: WorktreeBasics['portsSource'];
}): JSX.Element | null {
  const buttons: { label: string; port: number }[] = [];
  if (ports.django !== undefined)
    buttons.push({ label: 'Django', port: ports.django });
  if (ports.vite !== undefined)
    buttons.push({ label: 'Vite', port: ports.vite });
  if (buttons.length === 0) return null;
  const sourceLabel =
    source === 'ranch-config'
      ? 'from ~/.ranch/config.toml'
      : source === 'env-agent'
        ? 'from .env.agent (may drift)'
        : '';
  return (
    <div className="card__ports">
      {buttons.map((b) => (
        <a
          key={b.label}
          className="port-button"
          href={`http://localhost:${b.port}`}
          target="_blank"
          rel="noreferrer"
          title={`${b.label} :${b.port} ${sourceLabel}`}
        >
          {b.label} <span className="port-button__num">:{b.port}</span>
        </a>
      ))}
    </div>
  );
}

function DriftWarnings({
  worktree,
}: {
  worktree: WorktreeBasics;
}): JSX.Element | null {
  const messages: string[] = [];

  if (!worktree.envAgentMatches && worktree.envAgentName !== undefined) {
    messages.push(
      `.env.agent says AGENT_NAME=${worktree.envAgentName}, but this worktree is registered as ${worktree.agent}. Run \`make sync-env\` to repair.`,
    );
  }

  if (worktree.portsSource === 'ranch-config') {
    const drift: string[] = [];
    if (
      worktree.ports.django !== undefined &&
      worktree.envAgentPorts.django !== undefined &&
      worktree.ports.django !== worktree.envAgentPorts.django
    ) {
      drift.push(
        `DJANGO_PORT (env: ${worktree.envAgentPorts.django}, config: ${worktree.ports.django})`,
      );
    }
    if (
      worktree.ports.vite !== undefined &&
      worktree.envAgentPorts.vite !== undefined &&
      worktree.ports.vite !== worktree.envAgentPorts.vite
    ) {
      drift.push(
        `VITE_PORT (env: ${worktree.envAgentPorts.vite}, config: ${worktree.ports.vite})`,
      );
    }
    if (drift.length > 0) {
      messages.push(`.env.agent ports drift: ${drift.join(', ')}`);
    }
  }

  if (messages.length === 0) return null;
  return (
    <div className="card__drift">
      {messages.map((m, i) => (
        <p key={i} className="card__warn">
          ⚠ {m}
        </p>
      ))}
    </div>
  );
}

// ─── helpers ─────────────────────────────────────────────────────────────

function todoIcon(status: TodoItem['status']): string {
  switch (status) {
    case 'completed':
      return '✓';
    case 'in_progress':
      return '◐';
    case 'pending':
      return '○';
  }
}

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
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}
