/**
 * Embedded terminal component (xterm.js + main-process pty).
 *
 * Lifecycle on mount:
 *   1. Construct the xterm.js Terminal and attach to the container DOM
 *   2. Load addons (fit, webgl)
 *   3. Subscribe to push events from main: terminal:data → write to xterm
 *   4. Wire xterm onData → ranch.terminal.write (keystrokes back to pty)
 *   5. Call ranch.terminal.attach with the current cols/rows
 *   6. Resize observer keeps pty in sync with the container
 *
 * On unmount: unsubscribe + detach the pty (the tmux session keeps
 * running — we're closing the client, not killing the session).
 */

import { useEffect, useRef, useState } from 'react';
import { Terminal as XTerm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import '@xterm/xterm/css/xterm.css';

interface TerminalProps {
  agent: string;
  /** Increment this to force a fresh attach (e.g. after detach). */
  generation: number;
  /**
   * Called when the user clicks the Reconnect overlay button. The parent
   * should bump `generation` in response — that triggers a remount which
   * re-runs attach (which spawns a fresh tmux session via -A semantics
   * if none exists, or attaches to an existing one).
   */
  onReconnect?: () => void;
}

type Status =
  | { kind: 'connecting' }
  | { kind: 'connected'; terminalId: string }
  | { kind: 'exited'; exitCode: number; signal: number | null }
  | { kind: 'error'; reason: string };

export function Terminal({
  agent,
  generation,
  onReconnect,
}: TerminalProps): JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<Status>({ kind: 'connecting' });

  useEffect(() => {
    let disposed = false;
    const container = containerRef.current;
    if (!container) return;

    // Reset status on every (re)mount. Without this the Reconnect path
    // is broken: bumping generation remounts the effect but React keeps
    // the old `status` state, so the 'exited' overlay sticks even after
    // the new pty attaches.
    setStatus({ kind: 'connecting' });

    // 1. xterm instance
    const xterm = new XTerm({
      fontFamily: 'SF Mono, Menlo, Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.2,
      cursorBlink: true,
      allowProposedApi: true,
      // Wheel events are forwarded to the pty (which is tmux) — tmux's
      // smart-wheel config (see pty.ts) auto-enters copy-mode in
      // alt-screen apps so scrolling always works. xterm.js's own
      // scrollback only matters when the inner program is NOT in
      // alt-screen (rare for our use case), but we keep a generous
      // buffer anyway.
      scrollback: 10_000,
      // Sensitivity 1 keeps wheel events one-to-one with what tmux
      // expects; cranking it makes copy-mode jump too aggressively.
      scrollSensitivity: 1,
      fastScrollSensitivity: 5,
      scrollOnUserInput: true,
      theme: {
        background: '#0f1115',
        foreground: '#e6e9ef',
        cursor: '#6aa2ff',
        selectionBackground: '#2a3044',
      },
    });

    const fitAddon = new FitAddon();
    xterm.loadAddon(fitAddon);
    xterm.open(container);

    // WebGL renderer is much faster on busy streams (CC plan mode etc.)
    // It can fail on some GPU configs — fall back silently.
    try {
      const webgl = new WebglAddon();
      webgl.onContextLoss(() => webgl.dispose());
      xterm.loadAddon(webgl);
    } catch {
      // canvas/dom renderer kicks in by default
    }

    fitAddon.fit();
    const initialCols = xterm.cols;
    const initialRows = xterm.rows;

    // 2. Subscribe to pty push events. Only render data for our terminal —
    //    multiple terminals can be active simultaneously in main, but this
    //    component owns one.
    let attachedTerminalId: string | null = null;

    const offData = window.ranch.terminal.onData((evt) => {
      if (evt.terminalId !== attachedTerminalId) return;
      xterm.write(evt.data);
    });

    const offExit = window.ranch.terminal.onExit((evt) => {
      if (evt.terminalId !== attachedTerminalId) return;
      const sigPart = evt.signal !== null ? ` signal=${evt.signal}` : '';
      xterm.writeln(
        `\r\n\x1b[33m[ranch] tmux client detached (exit=${evt.exitCode}${sigPart})\x1b[0m`,
      );
      // Surface a Reconnect overlay so the operator has a clear path back
      // — without this, killing the session leaves the cell stuck.
      setStatus({
        kind: 'exited',
        exitCode: evt.exitCode,
        signal: evt.signal,
      });
    });

    // 3. Forward keystrokes from xterm into the pty.
    const onDataDisposable = xterm.onData((data) => {
      if (attachedTerminalId !== null) {
        void window.ranch.terminal.write(attachedTerminalId, data);
      }
    });

    // 4. Attach. We pass `command: 'claude'` so that if the tmux session
    //    is being newly created (no existing ranch-<agent>), it launches
    //    straight into claude. tmux's -A flag ignores the command when
    //    attaching to an existing session, so this is no-op for re-attach.
    void (async () => {
      const result = await window.ranch.terminal.attach(agent, {
        cols: initialCols,
        rows: initialRows,
        command: 'claude',
      });
      if (disposed) {
        if (result.ok) {
          void window.ranch.terminal.detach(result.terminalId);
        }
        return;
      }
      if (!result.ok) {
        setStatus({ kind: 'error', reason: result.reason });
        return;
      }
      attachedTerminalId = result.terminalId;
      setStatus({ kind: 'connected', terminalId: result.terminalId });
      xterm.focus();
    })();

    // 5. Resize: refit on container resize, then push cols/rows to the pty.
    const ro = new ResizeObserver(() => {
      try {
        fitAddon.fit();
      } catch {
        // container detached mid-resize
        return;
      }
      if (attachedTerminalId !== null) {
        void window.ranch.terminal.resize(
          attachedTerminalId,
          xterm.cols,
          xterm.rows,
        );
      }
    });
    ro.observe(container);

    return () => {
      disposed = true;
      ro.disconnect();
      offData();
      offExit();
      onDataDisposable.dispose();
      if (attachedTerminalId !== null) {
        void window.ranch.terminal.detach(attachedTerminalId);
      }
      xterm.dispose();
    };
    // generation is in deps so a re-mount can be triggered externally if needed
  }, [agent, generation]);

  return (
    <div className="terminal">
      <div ref={containerRef} className="terminal__pane" />
      {status.kind === 'connecting' && (
        <div className="terminal__overlay">connecting…</div>
      )}
      {status.kind === 'error' && (
        <div className="terminal__overlay terminal__overlay--error">
          {status.reason}
        </div>
      )}
      {status.kind === 'exited' && (
        <div className="terminal__overlay terminal__overlay--exited">
          <div className="terminal__exit-card">
            <p className="terminal__exit-msg">
              Session ended
              {status.signal !== null
                ? ` (signal ${status.signal})`
                : ` (exit ${status.exitCode})`}
            </p>
            {onReconnect ? (
              <button
                type="button"
                className="terminal__reconnect"
                onClick={onReconnect}
              >
                Reconnect
              </button>
            ) : (
              <p className="terminal__exit-hint">
                Bump generation to reattach.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
