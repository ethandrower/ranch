/**
 * Docker introspection + per-agent compose lifecycle.
 *
 * Read state via `docker ps --format json`. Manage stacks via
 * `docker compose -p citemed_<agent>` shelling out. Convention matches
 * citemed_web's Makefile:
 *
 *   docker compose --env-file <worktree>/.env.agent
 *     -f <worktree>/docker-compose.yml
 *     -f <worktree>/docker-compose.agent.yml
 *     -p citemed_<agent>
 *
 * For drift tolerance we also match a project named just `<agent>`
 * (some old stacks were started without the citemed_ prefix).
 */

import { execFile as execFileCb } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { promisify } from 'node:util';
import type {
  AgentDockerConfig,
  DockerContainer,
  DockerEnv,
  DockerStackSnapshot,
} from '../shared/types.js';

const execFile = promisify(execFileCb);

let cachedDockerPath: string | null | undefined;

async function findDocker(): Promise<string | null> {
  if (cachedDockerPath !== undefined) return cachedDockerPath;
  for (const candidate of [
    '/opt/homebrew/bin/docker',
    '/usr/local/bin/docker',
    '/usr/bin/docker',
  ]) {
    if (existsSync(candidate)) {
      cachedDockerPath = candidate;
      return candidate;
    }
  }
  try {
    const { stdout } = await execFile('which', ['docker']);
    const path = stdout.trim();
    cachedDockerPath = path.length > 0 ? path : null;
    return cachedDockerPath;
  } catch {
    cachedDockerPath = null;
    return null;
  }
}

/**
 * Introspect what compose files + project name we'd actually use for
 * an agent. Surfaced in the sidebar so the operator sees exactly what
 * docker compose calls are being made — and notices if a path is missing.
 */
export interface ResolvedAgentDocker {
  projectName: string;
  composeFiles: string[];
  envFile: string | null;
  /** True if at least one configured compose file is missing on disk. */
  missingFiles: string[];
}

export function resolveAgentDocker(
  agent: string,
  worktreePath: string,
  dockerConfig?: AgentDockerConfig,
): ResolvedAgentDocker {
  const projectName = dockerConfig?.projectName ?? `citemed_${agent}`;
  const wanted = dockerConfig?.composeFiles ?? DEFAULT_COMPOSE_FILES;
  const composeFiles: string[] = [];
  const missingFiles: string[] = [];
  for (const name of wanted) {
    const p = join(worktreePath, name);
    if (existsSync(p)) composeFiles.push(p);
    else missingFiles.push(p);
  }
  const envFileName = dockerConfig?.envFile ?? DEFAULT_ENV_FILE;
  const envPath = join(worktreePath, envFileName);
  const envFile = existsSync(envPath) ? envPath : null;
  return { projectName, composeFiles, envFile, missingFiles };
}

export async function getDockerEnv(): Promise<DockerEnv> {
  const path = await findDocker();
  if (!path) return { dockerAvailable: false };
  // Probe daemon connectivity. `docker info` fails fast when the
  // engine isn't running (Docker Desktop quit, etc.).
  try {
    await execFile(path, ['info', '--format', '{{.ServerVersion}}'], {
      timeout: 4000,
    });
    return { dockerAvailable: true, dockerPath: path };
  } catch {
    return { dockerAvailable: false, dockerPath: path };
  }
}

interface SnapshotInput {
  /** agent name → worktree path (for compose file resolution on lifecycle ops). */
  agents: Record<string, string>;
}

interface RawPsRow {
  ID?: unknown;
  Names?: unknown;
  Image?: unknown;
  State?: unknown;
  Status?: unknown;
  Ports?: unknown;
  RunningFor?: unknown;
  Labels?: unknown;
}

function parseLabels(raw: unknown): Record<string, string> {
  if (typeof raw !== 'string') return {};
  const out: Record<string, string> = {};
  // docker formats labels as a comma-separated list of key=value pairs.
  // Values themselves don't contain `=`, so split on the first `=` only.
  for (const part of raw.split(',')) {
    const eq = part.indexOf('=');
    if (eq <= 0) continue;
    const k = part.slice(0, eq).trim();
    const v = part.slice(eq + 1).trim();
    out[k] = v;
  }
  return out;
}

function projectMatchesAgent(project: string, agent: string): boolean {
  // Accept canonical name + drift variant (some stacks started without prefix).
  return project === `citemed_${agent}` || project === agent;
}

/**
 * Find the actual docker compose project name in use for an agent's stack.
 * Returns the canonical `citemed_<agent>` if both variants are present,
 * the bare `<agent>` if only that exists, or null if no stack runs at all.
 *
 * Used by the lifecycle ops (down / restart / logs) — the user has at
 * least one stack started without the `citemed_` prefix, and a hardcoded
 * `-p citemed_<agent>` would no-op against it.
 */
async function findActiveProjectForAgent(
  agent: string,
): Promise<string | null> {
  const dockerPath = await findDocker();
  if (!dockerPath) return null;
  const seen = new Set<string>();
  try {
    const { stdout } = await execFile(
      dockerPath,
      ['ps', '-a', '--format', 'json'],
      { timeout: 5000 },
    );
    for (const line of stdout.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const obj = JSON.parse(trimmed) as RawPsRow;
        const labels = parseLabels(obj.Labels);
        const project = labels['com.docker.compose.project'];
        if (project && projectMatchesAgent(project, agent)) {
          seen.add(project);
        }
      } catch {
        /* skip */
      }
    }
  } catch {
    return null;
  }
  // Prefer canonical when both exist.
  if (seen.has(`citemed_${agent}`)) return `citemed_${agent}`;
  if (seen.has(agent)) return agent;
  return null;
}

function rowToContainer(row: RawPsRow): DockerContainer | null {
  const labels = parseLabels(row.Labels);
  const project = labels['com.docker.compose.project'];
  const service = labels['com.docker.compose.service'];
  if (!project) return null;
  const id = typeof row.ID === 'string' ? row.ID : '';
  const name = typeof row.Names === 'string' ? row.Names : '';
  const image = typeof row.Image === 'string' ? row.Image : '';
  const state = typeof row.State === 'string' ? row.State : 'unknown';
  const status = typeof row.Status === 'string' ? row.Status : '';
  const ports = typeof row.Ports === 'string' ? row.Ports : '';
  const c: DockerContainer = {
    id,
    name,
    image,
    state,
    status,
    project,
  };
  if (service) c.service = service;
  if (ports) c.ports = ports;
  return c;
}

export async function snapshotDockerState(
  input: SnapshotInput,
): Promise<DockerStackSnapshot> {
  const dockerPath = await findDocker();
  const empty: DockerStackSnapshot = {
    available: false,
    perAgent: {},
    shared: [],
  };
  if (!dockerPath) return empty;

  let parsed: RawPsRow[] = [];
  try {
    const { stdout } = await execFile(
      dockerPath,
      ['ps', '-a', '--format', 'json'],
      { timeout: 5000 },
    );
    // docker emits one JSON object per line, NOT a JSON array.
    for (const line of stdout.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const obj = JSON.parse(trimmed) as RawPsRow;
        parsed.push(obj);
      } catch {
        /* malformed line — skip */
      }
    }
  } catch {
    return { ...empty, available: true };
  }

  const containers: DockerContainer[] = [];
  for (const row of parsed) {
    const c = rowToContainer(row);
    if (c) containers.push(c);
  }

  const perAgent: Record<string, DockerContainer[]> = {};
  for (const agent of Object.keys(input.agents)) {
    perAgent[agent] = containers.filter((c) =>
      projectMatchesAgent(c.project, agent),
    );
  }
  const shared = containers.filter((c) => c.project === 'citemed_shared');

  return {
    available: true,
    perAgent,
    shared,
  };
}

// ─── Lifecycle ───────────────────────────────────────────────

/** What we'll actually pass to `docker compose -f <…>` after resolution. */
export interface ResolvedComposeConfig {
  /** Absolute paths, in order, of compose files that exist. */
  composeFiles: string[];
  /** Absolute path to .env-style file, if it exists. */
  envFile: string | null;
}

// Per-agent stacks are self-contained in docker-compose.agent.yml — its
// own header literally documents `docker compose -f docker-compose.agent.yml
// -p citemed_$AGENT_NAME up -d`. Including docker-compose.yml as well
// (the Makefile's habit) merges the full app stack on top, which
// duplicates services and competes with citemed_shared. Default is just
// the agent file. Anyone who needs the override layer can opt in via
// the per-agent docker.compose_files block in ~/.ranch/config.toml.
const DEFAULT_COMPOSE_FILES = ['docker-compose.agent.yml'];
const DEFAULT_ENV_FILE = '.env.agent';

/**
 * Resolve which compose files + env file to use for an agent. Per-agent
 * `docker` block in ~/.ranch/config.toml wins; otherwise we fall back
 * to citemed_web's convention. Files that don't exist on disk are
 * silently dropped from the list — operator sees what we actually used
 * via the sidebar's "Compose files" caption.
 */
export function resolveComposeFiles(
  worktreePath: string,
  config?: AgentDockerConfig,
): ResolvedComposeConfig | null {
  const fileNames = config?.composeFiles ?? DEFAULT_COMPOSE_FILES;
  const composeFiles = fileNames
    .map((name) => join(worktreePath, name))
    .filter((p) => existsSync(p));
  if (composeFiles.length === 0) return null;

  const envFileName = config?.envFile ?? DEFAULT_ENV_FILE;
  const envPath = join(worktreePath, envFileName);
  const envFile = existsSync(envPath) ? envPath : null;

  return { composeFiles, envFile };
}

interface ComposeRunResult {
  ok: boolean;
  stdout: string;
  stderr: string;
}

async function composeRun(
  agent: string,
  worktreePath: string,
  args: string[],
  needsFiles: boolean,
  projectOverride?: string,
  dockerConfig?: AgentDockerConfig,
): Promise<ComposeRunResult> {
  const dockerPath = await findDocker();
  if (!dockerPath) {
    return { ok: false, stdout: '', stderr: 'docker not installed' };
  }
  const project =
    projectOverride ?? dockerConfig?.projectName ?? `citemed_${agent}`;
  const composeArgs: string[] = ['compose'];

  if (needsFiles) {
    const files = resolveComposeFiles(worktreePath, dockerConfig);
    if (!files) {
      return {
        ok: false,
        stdout: '',
        stderr: `no compose files found at ${worktreePath}. Configure via ~/.ranch/config.toml: [agents.<name>.docker].compose_files`,
      };
    }
    if (files.envFile) {
      composeArgs.push('--env-file', files.envFile);
    }
    for (const f of files.composeFiles) {
      composeArgs.push('-f', f);
    }
  }

  composeArgs.push('-p', project, ...args);

  try {
    const { stdout, stderr } = await execFile(dockerPath, composeArgs, {
      cwd: worktreePath,
      maxBuffer: 4 * 1024 * 1024,
      timeout: 120_000,
    });
    return { ok: true, stdout, stderr };
  } catch (err) {
    const stderr =
      err && typeof err === 'object' && 'stderr' in err
        ? String((err as { stderr: unknown }).stderr ?? '')
        : err instanceof Error
          ? err.message
          : String(err);
    return { ok: false, stdout: '', stderr };
  }
}

export async function dockerStackUp(
  agent: string,
  worktreePath: string,
  dockerConfig?: AgentDockerConfig,
): Promise<ComposeRunResult> {
  // For up: prefer existing project name if there's already a stack
  // running for this agent. Avoids creating a duplicate (e.g. canonical
  // `citemed_max` alongside an existing bare `max` stack).
  const existing = await findActiveProjectForAgent(agent);
  return composeRun(
    agent,
    worktreePath,
    ['up', '-d'],
    true,
    existing ?? undefined,
    dockerConfig,
  );
}

export async function dockerStackDown(
  agent: string,
  worktreePath: string,
  dockerConfig?: AgentDockerConfig,
): Promise<ComposeRunResult> {
  // Detect the actual project so we don't issue a no-op `down` against
  // the wrong project name.
  const existing = await findActiveProjectForAgent(agent);
  if (!existing) {
    return {
      ok: true,
      stdout: 'no active stack to bring down',
      stderr: '',
    };
  }
  return composeRun(
    agent,
    worktreePath,
    ['down'],
    false,
    existing,
    dockerConfig,
  );
}

export async function dockerStackRestart(
  agent: string,
  worktreePath: string,
  dockerConfig?: AgentDockerConfig,
): Promise<ComposeRunResult> {
  const existing = await findActiveProjectForAgent(agent);
  if (!existing) {
    return {
      ok: false,
      stdout: '',
      stderr: 'no active stack to restart — bring it Up first',
    };
  }
  return composeRun(
    agent,
    worktreePath,
    ['restart'],
    false,
    existing,
    dockerConfig,
  );
}

/**
 * Wipe and recreate an agent's stack. Equivalent to:
 *
 *   docker compose -p <project> down -v   (-v removes named volumes,
 *                                           which is the data wipe)
 *   docker compose --env-file ... -f ... -p <project> up -d
 *
 * Destructive: kills containers AND removes their volumes (Postgres
 * data, Redis state, anything else volume-backed). The next up
 * recreates containers with empty volumes.
 *
 * Returns the combined output of both phases. If down fails, up is
 * skipped and the caller sees the down error.
 */
export async function dockerStackReset(
  agent: string,
  worktreePath: string,
  dockerConfig?: AgentDockerConfig,
): Promise<ComposeRunResult> {
  const existing = await findActiveProjectForAgent(agent);
  if (existing) {
    const downResult = await composeRun(
      agent,
      worktreePath,
      ['down', '-v'],
      false,
      existing,
      dockerConfig,
    );
    if (!downResult.ok) {
      return {
        ok: false,
        stdout: downResult.stdout,
        stderr: `[reset:down] ${downResult.stderr}`,
      };
    }
  }
  // Up uses the configured project name (or canonical) — after a reset
  // the operator wants the cleanly-named stack going forward.
  const project = dockerConfig?.projectName ?? `citemed_${agent}`;
  const upResult = await composeRun(
    agent,
    worktreePath,
    ['up', '-d'],
    true,
    project,
    dockerConfig,
  );
  return {
    ok: upResult.ok,
    stdout: upResult.stdout,
    stderr: upResult.ok ? '' : `[reset:up] ${upResult.stderr}`,
  };
}

export async function dockerStackLogs(
  agent: string,
  worktreePath: string,
  tail = 200,
  dockerConfig?: AgentDockerConfig,
): Promise<ComposeRunResult> {
  const existing = await findActiveProjectForAgent(agent);
  return composeRun(
    agent,
    worktreePath,
    ['logs', `--tail=${tail}`, '--no-color'],
    false,
    existing ?? undefined,
    dockerConfig,
  );
}

// ─── Shared infra (citemed_shared: postgres, redis) ─────────────────

const SHARED_PROJECT = 'citemed_shared';
const SHARED_COMPOSE_FILE = 'docker-compose.shared.yml';

/**
 * Find a worktree that has the shared compose file, so lifecycle ops
 * have a directory to run from. All registered worktrees come from the
 * same repo so any of them works; we just need to pick one that's
 * actually checked out.
 */
function pickSharedWorktree(worktreePaths: string[]): string | null {
  for (const p of worktreePaths) {
    if (existsSync(join(p, SHARED_COMPOSE_FILE))) return p;
  }
  return null;
}

async function sharedComposeRun(
  worktreePaths: string[],
  args: string[],
  needsFiles: boolean,
): Promise<ComposeRunResult> {
  const dockerPath = await findDocker();
  if (!dockerPath) {
    return { ok: false, stdout: '', stderr: 'docker not installed' };
  }
  const worktree = pickSharedWorktree(worktreePaths);
  const composeArgs: string[] = ['compose'];
  if (needsFiles) {
    if (!worktree) {
      return {
        ok: false,
        stdout: '',
        stderr: `${SHARED_COMPOSE_FILE} not found in any registered worktree`,
      };
    }
    composeArgs.push('-f', join(worktree, SHARED_COMPOSE_FILE));
  }
  composeArgs.push('-p', SHARED_PROJECT, ...args);
  try {
    const { stdout, stderr } = await execFile(dockerPath, composeArgs, {
      cwd: worktree ?? process.cwd(),
      maxBuffer: 4 * 1024 * 1024,
      timeout: 120_000,
    });
    return { ok: true, stdout, stderr };
  } catch (err) {
    const stderr =
      err && typeof err === 'object' && 'stderr' in err
        ? String((err as { stderr: unknown }).stderr ?? '')
        : err instanceof Error
          ? err.message
          : String(err);
    return { ok: false, stdout: '', stderr };
  }
}

export async function dockerSharedUp(
  worktreePaths: string[],
): Promise<ComposeRunResult> {
  return sharedComposeRun(worktreePaths, ['up', '-d'], true);
}

export async function dockerSharedDown(
  worktreePaths: string[],
): Promise<ComposeRunResult> {
  return sharedComposeRun(worktreePaths, ['down'], false);
}

export async function dockerSharedRestart(
  worktreePaths: string[],
): Promise<ComposeRunResult> {
  return sharedComposeRun(worktreePaths, ['restart'], false);
}

export interface ResolvedSharedDocker {
  projectName: string;
  composeFile: string | null;
}

export function resolveSharedDocker(
  worktreePaths: string[],
): ResolvedSharedDocker {
  const worktree = pickSharedWorktree(worktreePaths);
  return {
    projectName: SHARED_PROJECT,
    composeFile: worktree ? join(worktree, SHARED_COMPOSE_FILE) : null,
  };
}
