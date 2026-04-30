/**
 * MVP-6 — embedded terminal via tmux + node-pty.
 *
 * The convention this module enforces:
 *   - Every ranch terminal lives in a tmux session named `ranch-<agent>`
 *   - We attach via `tmux new-session -A`, which means "attach if exists,
 *     create if not" — idempotent and survives detach
 *   - Closing the pty detaches the tmux client; the session keeps running
 *
 * This naming contract is also how ranch tells its sessions apart from
 * the operator's manually-created tmux sessions, and how cross-contamination
 * detection (future MVP-4 work) will identify which agent a process belongs to.
 *
 * Why two IPC patterns:
 *   - request/response (ipcMain.handle): for one-shot ops (attach, write, resize)
 *   - main→renderer push (webContents.send): for streaming pty output
 *     There's no other clean way to hand a continuous byte stream from
 *     a Node child process up to a sandboxed renderer.
 */

import { spawn as ptySpawn, type IPty } from 'node-pty';
import { execFile as execFileCb } from 'node:child_process';
import { existsSync } from 'node:fs';
import { promisify } from 'node:util';
import type { WebContents } from 'electron';
import type { TerminalEnv, TerminalAttachResult } from '../shared/types.js';

const execFile = promisify(execFileCb);

interface ActiveTerminal {
  terminalId: string;
  agent: string;
  pty: IPty;
  /** WebContents that this terminal pushes data to. */
  webContents: WebContents;
  /**
   * Number of live attach() calls that haven't been balanced by detach().
   * React 18 StrictMode in dev double-fires effects (mount → cleanup →
   * mount), and even in production a card may attach twice across a
   * re-render before the first detach lands. We only SIGHUP the tmux
   * client when refCount drops to 0 — otherwise we kill the pty out
   * from under a still-mounted Terminal component.
   */
  refCount: number;
}

/** Indexed by terminalId (`ranch-<agent>`). */
const terminals = new Map<string, ActiveTerminal>();

let cachedTmuxPath: string | null | undefined;

/**
 * Locate tmux on PATH. Cached after first lookup.
 * Returns null if not installed.
 */
async function findTmux(): Promise<string | null> {
  if (cachedTmuxPath !== undefined) return cachedTmuxPath;
  // Check common Homebrew + system locations directly first
  // (the spawned PATH inside Electron may not include /opt/homebrew/bin).
  for (const candidate of [
    '/opt/homebrew/bin/tmux',
    '/usr/local/bin/tmux',
    '/usr/bin/tmux',
  ]) {
    if (existsSync(candidate)) {
      cachedTmuxPath = candidate;
      return candidate;
    }
  }
  // Fall back to `which`.
  try {
    const { stdout } = await execFile('which', ['tmux']);
    const path = stdout.trim();
    cachedTmuxPath = path.length > 0 ? path : null;
    return cachedTmuxPath;
  } catch {
    cachedTmuxPath = null;
    return null;
  }
}

export async function getTerminalEnv(): Promise<TerminalEnv> {
  const tmuxPath = await findTmux();
  const env: TerminalEnv = { tmuxAvailable: tmuxPath !== null };
  if (tmuxPath) env.tmuxPath = tmuxPath;
  return env;
}

interface AttachOptions {
  agent: string;
  worktreePath: string;
  cols?: number;
  rows?: number;
  webContents: WebContents;
  /**
   * Command to run inside the tmux session if it doesn't already exist.
   * tmux's `-A` flag (attach-or-create) ignores command args when the
   * session is already running, so this only affects fresh sessions —
   * exactly what we want.
   */
  command?: string;
}

export async function attachTerminal(
  opts: AttachOptions,
): Promise<TerminalAttachResult> {
  const tmuxPath = await findTmux();
  if (!tmuxPath) {
    return {
      ok: false,
      reason:
        'tmux is not installed. Install via `brew install tmux` and reopen the terminal.',
    };
  }

  const terminalId = `ranch-${opts.agent}`;

  // If already attached for this webContents, increment the refcount and
  // re-use. The same data stream is fanned out — re-spawning would kill
  // the live one and break the still-mounted component.
  const existing = terminals.get(terminalId);
  if (existing && existing.webContents === opts.webContents) {
    existing.refCount += 1;
    return { ok: true, terminalId };
  }
  // If a different window had it attached, force-detach the old client
  // first. (This is intentional: only one window owns a terminal at a
  // time. Multi-window support is post-MVP.)
  if (existing) {
    detachInternal(terminalId, true);
  }

  // tmux new-session:
  //   -A  attach if a session by this name exists, else create
  //   -s  session name
  //   -c  start directory (only used when creating a new session)
  //   final positional COMMAND (if given) only runs when creating;
  //   ignored when attaching to existing session
  const args = ['new-session', '-A', '-s', terminalId, '-c', opts.worktreePath];
  if (opts.command) args.push(opts.command);

  const pty = ptySpawn(tmuxPath, args, {
    name: 'xterm-256color',
    cols: opts.cols ?? 100,
    rows: opts.rows ?? 30,
    cwd: opts.worktreePath,
    env: {
      ...process.env,
      // Force a known TERM so xterm.js renders correctly.
      TERM: 'xterm-256color',
      // Tell child processes they're inside ranch — useful for future
      // hooks that want to behave differently when launched here.
      RANCH_AGENT: opts.agent,
      RANCH_TERMINAL: terminalId,
    },
  });

  const active: ActiveTerminal = {
    terminalId,
    agent: opts.agent,
    pty,
    webContents: opts.webContents,
    refCount: 1,
  };
  terminals.set(terminalId, active);

  pty.onData((data) => {
    if (active.webContents.isDestroyed()) return;
    active.webContents.send('terminal:data', { terminalId, data });
  });

  pty.onExit(({ exitCode, signal }) => {
    // Diagnostic — useful when tmux dies for non-obvious reasons.
    console.error(
      `[ranch.pty] ${terminalId} exited (exitCode=${exitCode}, signal=${signal ?? 'none'})`,
    );
    if (!active.webContents.isDestroyed()) {
      active.webContents.send('terminal:exit', {
        terminalId,
        exitCode,
        signal: signal ?? null,
      });
    }
    terminals.delete(terminalId);
  });

  return { ok: true, terminalId };
}

export function writeTerminal(terminalId: string, data: string): void {
  const t = terminals.get(terminalId);
  if (!t) return;
  t.pty.write(data);
}

export function resizeTerminal(
  terminalId: string,
  cols: number,
  rows: number,
): void {
  const t = terminals.get(terminalId);
  if (!t) return;
  if (cols < 1 || rows < 1) return;
  try {
    t.pty.resize(Math.floor(cols), Math.floor(rows));
  } catch {
    // pty closed mid-resize; ignore
  }
}

export function detachTerminal(terminalId: string): void {
  detachInternal(terminalId, false);
}

/**
 * @param force when true, ignore refcount and SIGHUP immediately. Used
 *   when a window closes (we want all its ptys gone, regardless of how
 *   many components were mid-cleanup) and when transferring a terminal
 *   between windows.
 */
function detachInternal(terminalId: string, force: boolean): void {
  const t = terminals.get(terminalId);
  if (!t) return;
  if (!force) {
    t.refCount -= 1;
    if (t.refCount > 0) return;
  }
  try {
    // SIGHUP → tmux client detaches cleanly without killing the session.
    t.pty.kill('SIGHUP');
  } catch {
    // already gone
  }
  terminals.delete(terminalId);
}

/**
 * Detach all terminals associated with a webContents instance — called
 * when a window closes so we don't leak ptys.
 */
export function detachAllForWebContents(wc: WebContents): void {
  for (const [id, t] of terminals.entries()) {
    if (t.webContents === wc) {
      detachInternal(id, true);
    }
  }
}
