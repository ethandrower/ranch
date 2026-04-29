import { useEffect, useState } from 'react';
import type { RanchConfig } from '../shared/types.js';

interface ConfigState {
  status: 'loading' | 'ready' | 'error';
  config?: RanchConfig;
  appVersion?: string;
  error?: string;
}

export function App(): JSX.Element {
  const [state, setState] = useState<ConfigState>({ status: 'loading' });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [config, appVersion] = await Promise.all([
          window.ranch.config.get(),
          window.ranch.app.version(),
        ]);
        if (!cancelled) {
          setState({ status: 'ready', config, appVersion });
        }
      } catch (err) {
        if (!cancelled) {
          setState({
            status: 'error',
            error: err instanceof Error ? err.message : String(err),
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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
          <GridPlaceholder state={state} />
        </section>
        <section className="pane pane--terminal">
          <h2>Terminal</h2>
          <p className="placeholder">No session selected.</p>
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

function GridPlaceholder({ state }: { state: ConfigState }): JSX.Element {
  if (state.status === 'loading') {
    return <p className="placeholder">Loading config…</p>;
  }
  if (state.status === 'error') {
    return (
      <p className="placeholder placeholder--error">Error: {state.error}</p>
    );
  }
  const { agents, projects, configPath } = state.config!;
  return (
    <div>
      <p className="config-summary">
        Loaded <strong>{agents.length}</strong> agent
        {agents.length === 1 ? '' : 's'} and <strong>{projects.length}</strong>{' '}
        project{projects.length === 1 ? '' : 's'} from <code>{configPath}</code>
      </p>
      {agents.length > 0 && (
        <ul className="agent-list">
          {agents.map((agent) => (
            <li key={agent.name} className="agent-list__item">
              <span className="agent-list__name">{agent.name}</span>
              <span className="agent-list__path">{agent.worktree}</span>
              {agent.description && (
                <span className="agent-list__desc">{agent.description}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
