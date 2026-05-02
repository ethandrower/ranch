/**
 * MVP-4 — detect tmux sessions and `claude` processes for each worktree.
 *
 * Two parallel signals per worktree:
 *
 *   1. tmux session named `ranch-<agent>`
 *      - exists / attached-clients / created-at
 *      - Lifted from `tmux list-sessions -F '...'`. We don't error if
 *        tmux is missing — that's surfaced separately by pty.ts.
 *
 *   2. `claude` processes whose cwd is inside the worktree path
 *      - We list all claude processes via `ps`, then resolve each one's
 *        cwd via `lsof`. We do this once per fleet refresh (not once per
 *        worktree) so four cards polling at 5s = 4 ps + ~N lsof, not
 *        16 ps invocations.
 *      - The cwd-based attribution is what makes cross-contamination
 *        detectable later: a claude with cwd in jeffy/ but parent shell
 *        rooted in max/'s tmux session is a smell.
 *
 * macOS-only for now. Linux would swap `lsof -a -p X -d cwd -F n` for
 * reading `/proc/X/cwd` symlink.
 */

import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';
import type {
  CCProcessState,
  ClaudeProcess,
  ProcessSnapshot,
  TmuxSessionState,
} from '../shared/types.js';

const execFile = promisify(execFileCb);

interface SnapshotInput {
  /** Agents to surface state for; keyed by agent name → worktree path. */
  agents: Record<string, string>;
}

/** ─── tmux ──────────────────────────────────────────────────── */

async function listRanchTmuxSessions(): Promise<Map<string, TmuxSessionState>> {
  const out = new Map<string, TmuxSessionState>();
  try {
    // Ranch tmux sessions live on the dedicated `ranch` socket — see
    // pty.ts. Without -L we'd be reading the user's default tmux
    // server which won't have our sessions.
    const { stdout } = await execFile('tmux', [
      '-L',
      'ranch',
      'list-sessions',
      '-F',
      '#{session_name}|#{session_attached}|#{session_created}',
    ]);
    for (const line of stdout.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const [name, attached, created] = trimmed.split('|');
      if (!name || !name.startsWith('ranch-')) continue;
      const agent = name.slice('ranch-'.length);
      const attachedClients = Number.parseInt(attached ?? '0', 10);
      const createdAtSec = Number.parseInt(created ?? '0', 10);
      const session: TmuxSessionState = {
        sessionName: name,
        exists: true,
        attachedClients: Number.isFinite(attachedClients) ? attachedClients : 0,
      };
      if (Number.isFinite(createdAtSec)) {
        session.createdAt = new Date(createdAtSec * 1000).toISOString();
      }
      out.set(agent, session);
    }
  } catch {
    // tmux missing, no server running, or it errored — return empty map.
    // pty.ts already surfaces "tmux not installed" as a separate signal.
  }
  return out;
}

/** ─── claude processes ─────────────────────────────────────── */

interface RawProcess {
  pid: number;
  ppid: number;
  command: string;
}

async function listClaudeProcesses(): Promise<RawProcess[]> {
  // ps -axo pid=,ppid=,command=  (= suppresses headers)
  const { stdout } = await execFile('ps', ['-axo', 'pid=,ppid=,command=']);
  const processes: RawProcess[] = [];
  for (const line of stdout.split('\n')) {
    const m = /^\s*(\d+)\s+(\d+)\s+(.+)$/.exec(line);
    if (!m) continue;
    const command = m[3]!.trim();
    if (!isClaudeCommand(command)) continue;
    processes.push({
      pid: Number.parseInt(m[1]!, 10),
      ppid: Number.parseInt(m[2]!, 10),
      command,
    });
  }
  return processes;
}

/**
 * Match the `claude` CLI but reject lookalikes like
 *   `/bin/zsh -c "... claude foo ..."` (a shell calling claude),
 *   `claude --chrome` (Claude Desktop's helper).
 *
 * The CLI command line is either bare `claude` (or `claude <subcmd>`)
 * or its absolute path ending in `/claude`. We tolerate flags but
 * reject anything where `claude` isn't the first token.
 */
function isClaudeCommand(command: string): boolean {
  const first = command.split(/\s+/, 1)[0] ?? '';
  if (first.endsWith('/claude') || first === 'claude') {
    // Reject Claude Desktop helper.
    if (command.includes('--chrome')) return false;
    return true;
  }
  return false;
}

/** Returns the cwd of a process, or null if it can't be resolved. */
async function getProcessCwd(pid: number): Promise<string | null> {
  try {
    // -a ANDs the filters; without it lsof ORs them and dumps everything.
    const { stdout } = await execFile('lsof', [
      '-a',
      '-p',
      String(pid),
      '-d',
      'cwd',
      '-F',
      'n',
    ]);
    // Format:
    //   p<pid>
    //   fcwd
    //   n<path>
    for (const line of stdout.split('\n')) {
      if (line.startsWith('n')) return line.slice(1);
    }
  } catch {
    // process gone, or no permission
  }
  return null;
}

async function resolveClaudeCwds(
  procs: RawProcess[],
): Promise<ClaudeProcess[]> {
  return Promise.all(
    procs.map(async (p) => {
      const cwd = await getProcessCwd(p.pid);
      const result: ClaudeProcess = {
        pid: p.pid,
        ppid: p.ppid,
        command: p.command,
      };
      if (cwd !== null) result.cwd = cwd;
      return result;
    }),
  );
}

/** ─── snapshot (fleet-wide) ─────────────────────────────────── */

/**
 * One ps + one tmux-list-sessions per snapshot, regardless of how many
 * worktrees we're attributing. Cards consume the resulting per-agent
 * slice. Caller decides cadence.
 */
export async function snapshotProcessState(
  input: SnapshotInput,
): Promise<ProcessSnapshot> {
  const [tmuxByAgent, rawClaudes] = await Promise.all([
    listRanchTmuxSessions(),
    listClaudeProcesses(),
  ]);
  const claudes = await resolveClaudeCwds(rawClaudes);

  // Bucket claude processes by worktree (string-prefix match: a claude
  // started inside a subdirectory of the worktree still attributes
  // back to the worktree).
  const perAgent: Record<string, CCProcessState> = {};
  const claimed = new Set<number>();
  for (const [agent, worktreePath] of Object.entries(input.agents)) {
    const tmux = tmuxByAgent.get(agent) ?? null;
    const inWorktree = claudes.filter(
      (c) => c.cwd !== undefined && isWithin(c.cwd, worktreePath),
    );
    for (const p of inWorktree) claimed.add(p.pid);
    perAgent[agent] = {
      tmux,
      claudeRunning: inWorktree.length > 0,
      claudeProcesses: inWorktree,
    };
  }

  // Orphans: claudes that didn't attribute to any registered worktree.
  // Could be an operator running claude in a non-tracked dir (Documents/,
  // a one-off scratch repo) or, more concerningly, a claude inside one
  // of our worktrees that we somehow missed. The cwd will tell.
  const orphanClaudes = claudes.filter((c) => !claimed.has(c.pid));

  return {
    perAgent,
    orphanClaudes,
    totalClaudes: claudes.length,
  };
}

function isWithin(path: string, base: string): boolean {
  if (path === base) return true;
  const baseSlash = base.endsWith('/') ? base : base + '/';
  return path.startsWith(baseSlash);
}
