/**
 * LogStreamPanel — H19 Phase 1 standalone component.
 *
 * Subscribes to the agent's local docker compose logs and renders them
 * as a scrolling tail. Manages its own ring buffer (5000 lines) so
 * memory stays bounded — main side does the same independently, the
 * two are a belt + suspenders against the OOM class of failure we hit
 * in pty.ts (#67).
 *
 * Drop-in usage:
 *   <LogStreamPanel agent="max" />
 *
 * The component handles its own:
 *   - source selector (local | remote — remote disabled in Phase 1)
 *   - service picker (auto-discovered via window.ranch.logs.describe)
 *   - pause/resume autoscroll
 *   - clear buffer
 *   - subscription lifecycle (subscribe on mount/picker-change,
 *     unsubscribe on unmount/picker-change/source-change)
 *
 * It does NOT touch any of the existing renderer files; the parent
 * just renders <LogStreamPanel agent={agent} /> wherever the agent's
 * "Logs" tab content should live.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { AgentLogDescription, LogSource } from '../shared/types.js';

const RENDERER_RING_BUFFER_LINES = 5000;

interface LogStreamPanelProps {
  agent: string;
  /** Optional starting source. Defaults to 'local'. */
  initialSource?: LogSource;
}

interface PanelState {
  source: LogSource;
  service: string | undefined; // undefined = "all services" (local) / first app (remote)
  paused: boolean;
  lines: string[];
  desc: AgentLogDescription | null;
  status:
    | { kind: 'idle' }
    | { kind: 'connecting' }
    | { kind: 'connected' }
    | { kind: 'error'; reason: string }
    | { kind: 'exited'; expected: boolean; code: number | null };
}

const INITIAL_STATE: PanelState = {
  source: 'local',
  service: undefined,
  paused: false,
  lines: [],
  desc: null,
  status: { kind: 'idle' },
};

export function LogStreamPanel({ agent, initialSource }: LogStreamPanelProps): JSX.Element {
  const [state, setState] = useState<PanelState>({
    ...INITIAL_STATE,
    source: initialSource ?? 'local',
  });
  const scrollRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);
  const unsubscribeRef = useRef<(() => void) | null>(null);

  // ─── Auto-discover services / apps for this agent ────────────
  useEffect(() => {
    let cancelled = false;
    void window.ranch.logs.describe(agent).then((desc) => {
      if (cancelled) return;
      setState((prev) => ({
        ...prev,
        desc,
        // Pick the first local service by default; remote stays empty in Phase 1
        service:
          prev.service ??
          (prev.source === 'local'
            ? desc.local.services[0]
            : desc.remote.apps[0]),
      }));
    });
    return () => {
      cancelled = true;
    };
  }, [agent]);

  // ─── Subscribe on source / service change ────────────────────
  const subscribe = useCallback(async () => {
    // Tear down any prior subscription
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;

    setState((prev) => ({ ...prev, status: { kind: 'connecting' }, lines: [] }));

    // exactOptionalPropertyTypes is strict — omit service/tail when undefined
    // rather than passing literal `undefined`.
    const subscribeArgs: import('../shared/types.js').LogSubscribeArgs = {
      agent,
      source: state.source,
      tail: 200,
    };
    if (state.service) subscribeArgs.service = state.service;
    const result = await window.ranch.logs.subscribe(
      subscribeArgs,
      (line) => {
        // We use the functional setter so the lambda isn't stale-closed
        // over an old `lines` array (a common cause of dropped log lines).
        setState((prev) => {
          if (prev.paused) return prev;
          const next = prev.lines.length >= RENDERER_RING_BUFFER_LINES
            ? prev.lines.slice(prev.lines.length - RENDERER_RING_BUFFER_LINES + 1)
            : prev.lines.slice();
          next.push(line);
          return { ...prev, lines: next };
        });
      },
      (info) => {
        setState((prev) => ({
          ...prev,
          status: {
            kind: 'exited',
            expected: info.expected,
            code: info.exitCode,
          },
        }));
      },
    );

    if (!result.ok) {
      setState((prev) => ({
        ...prev,
        status: { kind: 'error', reason: result.reason ?? 'unknown error' },
      }));
      return;
    }

    const history = result.history ?? [];
    setState((prev) => ({
      ...prev,
      status: { kind: 'connected' },
      lines: history.slice(-RENDERER_RING_BUFFER_LINES),
    }));
    unsubscribeRef.current = result.unsubscribe ?? null;
  }, [agent, state.source, state.service]);

  useEffect(() => {
    // Only subscribe once a service has been resolved (avoids a 0-arg
    // sub that would tail all services, which is rarely what you want
    // when a picker is visible). For remote, the subscribe still fires
    // and the not-implemented response is rendered as an error.
    if (!state.service && state.source === 'local') return;
    void subscribe();
    return () => {
      unsubscribeRef.current?.();
      unsubscribeRef.current = null;
    };
  }, [subscribe, state.source, state.service]);

  // ─── Auto-scroll, with user-scroll-up to pause ──────────────
  useEffect(() => {
    if (!autoScrollRef.current) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [state.lines]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    autoScrollRef.current = atBottom;
  }, []);

  // ─── Action handlers ────────────────────────────────────────
  const setSource = (source: LogSource) => {
    const desc = state.desc;
    const nextService =
      source === 'local' ? desc?.local.services[0] : desc?.remote.apps[0];
    setState((prev) => ({ ...prev, source, service: nextService, lines: [] }));
  };
  const setService = (service: string) => {
    setState((prev) => ({ ...prev, service, lines: [] }));
  };
  const togglePause = () => setState((prev) => ({ ...prev, paused: !prev.paused }));
  const clearBuffer = () => setState((prev) => ({ ...prev, lines: [] }));

  // ─── Render ─────────────────────────────────────────────────
  const services = state.desc
    ? state.source === 'local'
      ? state.desc.local.services
      : state.desc.remote.apps
    : [];

  const statusLabel = useMemo(() => {
    switch (state.status.kind) {
      case 'idle': return '○ idle';
      case 'connecting': return '◌ connecting…';
      case 'connected': return '● connected';
      case 'error': return `✗ error: ${state.status.reason}`;
      case 'exited':
        return state.status.expected
          ? '○ closed'
          : `! exited (code ${state.status.code ?? '?'})`;
    }
  }, [state.status]);

  return (
    <div className="logstream">
      <div className="logstream__controls">
        <div className="logstream__sources">
          <button
            type="button"
            className={`logstream__pill ${state.source === 'local' ? 'is-active' : ''}`}
            onClick={() => setSource('local')}
          >
            Local
          </button>
          <button
            type="button"
            className={`logstream__pill ${state.source === 'remote' ? 'is-active' : ''}`}
            onClick={() => setSource('remote')}
            title="Remote streaming lands in H19 Phase 2"
          >
            Remote
          </button>
        </div>
        {services.length > 0 && (
          <select
            className="logstream__service"
            value={state.service ?? ''}
            onChange={(e) => setService(e.target.value)}
          >
            {services.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        )}
        <div className="logstream__spacer" />
        <span className="logstream__status">{statusLabel}</span>
        <button type="button" className="logstream__btn" onClick={togglePause}>
          {state.paused ? 'Resume' : 'Pause'}
        </button>
        <button type="button" className="logstream__btn" onClick={clearBuffer}>
          Clear
        </button>
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="logstream__output"
        // Inline minimal styling so the component renders sensibly
        // even before any host CSS is added; host can override via .logstream class.
        style={{
          fontFamily: 'SF Mono, Menlo, Consolas, monospace',
          fontSize: 12,
          lineHeight: 1.4,
          background: '#0f1115',
          color: '#e6e9ef',
          padding: 8,
          overflowY: 'auto',
          height: '100%',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
        }}
      >
        {state.lines.length === 0 && state.status.kind === 'connected' && (
          <div style={{ color: '#5a6478' }}>(no log lines yet — tail is empty)</div>
        )}
        {state.lines.map((line, i) => (
          <div key={`${state.lines.length}:${i}`}>{line}</div>
        ))}
      </div>
    </div>
  );
}
