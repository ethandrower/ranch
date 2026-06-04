/**
 * H19 Phase 1 — streaming log subscriptions.
 *
 * Per-agent local docker compose log streaming. Wraps
 *   `docker compose -p <project> -f <files> logs -f --tail=N [service]`
 * as a long-running subprocess and pushes lines to the renderer via
 * webContents.send('logs:line', { streamId, line }).
 *
 * Lifecycle:
 *   - subscribeLogs() returns a streamId. Renderer holds it and calls
 *     unsubscribeLogs(streamId) on unmount.
 *   - Each WebContents (window) may own multiple subscriptions; killing
 *     the window auto-unsubscribes all of its streams.
 *   - The same (agent, source, service) triple is fanned out — duplicate
 *     subscribe re-uses the running subprocess and refCounts. Closing
 *     the last consumer kills the subprocess.
 *
 * Why ring-buffer here (not just renderer-side):
 *   pty.ts's earlier OOM crash (#67) was caused by unbounded buffering
 *   on the main side. We cap main-side history to the last N lines per
 *   stream so a slow renderer can't OOM us either.
 */

import { type WebContents } from 'electron';
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { execFile as execFileCb } from 'node:child_process';

import { findDocker, resolveComposeFiles } from './docker.js';
import type { AgentDockerConfig, LogSource } from '../shared/types.js';

const execFile = promisify(execFileCb);

// Main-side ring buffer cap per stream. The renderer maintains its own
// independent cap; this one exists to protect main from a slow consumer.
const MAIN_RING_BUFFER_LINES = 5000;

interface ActiveLogStream {
  streamId: string;
  agent: string;
  source: LogSource;
  service: string | undefined;  // explicit undefined for exactOptionalPropertyTypes
  proc: ChildProcess;
  /** Last N lines emitted, for catch-up on duplicate subscribe. */
  history: string[];
  /** Refcount: WebContents instances currently listening. */
  consumers: Set<WebContents>;
  /** Set true when WE kill the subprocess so the exit event is expected. */
  expectedExit: boolean;
  /** Stable key for de-dup: `${source}:${agent}:${service ?? ''}`. */
  key: string;
}

const streams = new Map<string, ActiveLogStream>(); // by streamId
const byKey = new Map<string, ActiveLogStream>(); // for dedup lookup

let streamCounter = 0;
function nextStreamId(): string {
  streamCounter += 1;
  return `logs-${Date.now()}-${streamCounter}`;
}

function streamKey(args: SubscribeArgs): string {
  return `${args.source}:${args.agent}:${args.service ?? ''}`;
}

// ─── Describe (which services / apps does this agent expose?) ─────


export interface AgentLogDescription {
  local: { services: string[] };
  // remote shipped in Phase 2; surface it as empty for now so renderer
  // can render a disabled tab without a schema break later.
  remote: { apps: string[] };
}

/**
 * For an agent's local docker stack, list the compose services that
 * could be tailed. Uses `docker compose config --services` against the
 * resolved compose files for the agent.
 */
export async function describeAgentLogs(
  agent: string,
  worktreePath: string,
  dockerConfig?: AgentDockerConfig,
): Promise<AgentLogDescription> {
  const empty: AgentLogDescription = { local: { services: [] }, remote: { apps: [] } };
  const dockerPath = await findDocker();
  if (!dockerPath) return empty;

  const files = resolveComposeFiles(worktreePath, dockerConfig);
  if (!files) return empty;

  const args: string[] = ['compose'];
  if (files.envFile) args.push('--env-file', files.envFile);
  for (const f of files.composeFiles) args.push('-f', f);
  args.push('config', '--services');

  try {
    const { stdout } = await execFile(dockerPath, args, {
      cwd: worktreePath,
      timeout: 15_000,
    });
    const services = stdout
      .split('\n')
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    return { local: { services }, remote: { apps: [] } };
  } catch {
    return empty;
  }
}

// ─── Subscribe / unsubscribe ──────────────────────────────────────


export interface SubscribeArgs {
  agent: string;
  worktreePath: string;
  source: LogSource;
  /** Optional compose service name (local) or app name (remote). */
  service: string | undefined;
  /** Initial lines to surface (default 200). */
  tail: number | undefined;
  dockerConfig: AgentDockerConfig | undefined;
}

export interface SubscribeResult {
  ok: boolean;
  streamId?: string;
  reason?: string;
  /** Lines already-buffered on main side; renderer renders these first. */
  history?: string[];
}

export async function subscribeLogs(
  args: SubscribeArgs,
  webContents: WebContents,
): Promise<SubscribeResult> {
  if (args.source !== 'local') {
    // Phase 2 — remote SSH streaming. Surfacing the not-implemented
    // path explicitly so renderer can disable the tab cleanly instead
    // of getting a silent timeout.
    return {
      ok: false,
      reason: 'remote log streaming is not yet implemented (H19 Phase 2)',
    };
  }

  const dockerPath = await findDocker();
  if (!dockerPath) {
    return { ok: false, reason: 'docker not installed' };
  }
  const files = resolveComposeFiles(args.worktreePath, args.dockerConfig);
  if (!files) {
    return {
      ok: false,
      reason: `no compose files at ${args.worktreePath}`,
    };
  }

  const key = streamKey(args);
  const existing = byKey.get(key);
  if (existing) {
    existing.consumers.add(webContents);
    return { ok: true, streamId: existing.streamId, history: [...existing.history] };
  }

  const project =
    args.dockerConfig?.projectName ?? `citemed_${args.agent}`;
  const composeArgs: string[] = ['compose'];
  if (files.envFile) composeArgs.push('--env-file', files.envFile);
  for (const f of files.composeFiles) composeArgs.push('-f', f);
  composeArgs.push('-p', project, 'logs', '-f', '--no-color',
                    `--tail=${args.tail ?? 200}`);
  if (args.service) composeArgs.push(args.service);

  const proc = spawn(dockerPath, composeArgs, {
    cwd: args.worktreePath,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  const streamId = nextStreamId();
  const active: ActiveLogStream = {
    streamId,
    agent: args.agent,
    source: args.source,
    service: args.service,
    proc,
    history: [],
    consumers: new Set([webContents]),
    expectedExit: false,
    key,
  };
  streams.set(streamId, active);
  byKey.set(key, active);

  // Line-buffer both streams. Docker compose stamps stderr with service
  // health notices ("attaching to / detaching from") — keep them inline
  // so the operator sees lifecycle events alongside app output.
  let stdoutBuf = '';
  let stderrBuf = '';

  function emit(line: string) {
    // Trim main-side ring buffer
    active.history.push(line);
    if (active.history.length > MAIN_RING_BUFFER_LINES) {
      active.history.splice(0, active.history.length - MAIN_RING_BUFFER_LINES);
    }
    for (const wc of active.consumers) {
      if (!wc.isDestroyed()) wc.send('logs:line', { streamId, line });
    }
  }

  function feed(buf: string, chunk: Buffer | string, which: 'stdout' | 'stderr'): string {
    buf += typeof chunk === 'string' ? chunk : chunk.toString('utf8');
    let nl: number;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      emit(line);
    }
    void which;
    return buf;
  }

  proc.stdout?.on('data', (chunk) => {
    stdoutBuf = feed(stdoutBuf, chunk, 'stdout');
  });
  proc.stderr?.on('data', (chunk) => {
    stderrBuf = feed(stderrBuf, chunk, 'stderr');
  });

  proc.on('exit', (code, signal) => {
    // Flush any trailing partial lines
    if (stdoutBuf.length > 0) emit(stdoutBuf);
    if (stderrBuf.length > 0) emit(stderrBuf);
    for (const wc of active.consumers) {
      if (!wc.isDestroyed()) {
        wc.send('logs:exit', {
          streamId,
          exitCode: code,
          signal,
          expected: active.expectedExit,
        });
      }
    }
    streams.delete(streamId);
    byKey.delete(key);
  });

  proc.on('error', (err) => {
    for (const wc of active.consumers) {
      if (!wc.isDestroyed()) {
        wc.send('logs:line', {
          streamId,
          line: `[ranch.logs] subprocess error: ${err.message}`,
        });
      }
    }
  });

  return { ok: true, streamId, history: [] };
}

export function unsubscribeLogs(streamId: string, webContents: WebContents): void {
  const s = streams.get(streamId);
  if (!s) return;
  s.consumers.delete(webContents);
  if (s.consumers.size === 0) {
    s.expectedExit = true;
    try {
      s.proc.kill('SIGTERM');
    } catch {
      // already dead
    }
  }
}

/** Called by main when a window closes — kill all of its subscriptions. */
export function unsubscribeAllForWebContents(wc: WebContents): void {
  for (const [id, s] of streams.entries()) {
    if (s.consumers.has(wc)) {
      unsubscribeLogs(id, wc);
    }
  }
}
