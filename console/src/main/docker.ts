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

interface ComposeFiles {
  composeFile: string;
  agentComposeFile: string;
  envFile: string;
}

function resolveComposeFiles(worktreePath: string): ComposeFiles | null {
  const composeFile = join(worktreePath, 'docker-compose.yml');
  const agentComposeFile = join(worktreePath, 'docker-compose.agent.yml');
  const envFile = join(worktreePath, '.env.agent');
  // We need at least the base compose file. agent override + env are
  // expected for citemed_web but soft-fail on simpler projects.
  if (!existsSync(composeFile)) return null;
  return { composeFile, agentComposeFile, envFile };
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
): Promise<ComposeRunResult> {
  const dockerPath = await findDocker();
  if (!dockerPath) {
    return { ok: false, stdout: '', stderr: 'docker not installed' };
  }
  const project = projectOverride ?? `citemed_${agent}`;
  const composeArgs: string[] = ['compose'];

  if (needsFiles) {
    const files = resolveComposeFiles(worktreePath);
    if (!files) {
      return {
        ok: false,
        stdout: '',
        stderr: `no docker-compose.yml found at ${worktreePath}`,
      };
    }
    if (existsSync(files.envFile)) {
      composeArgs.push('--env-file', files.envFile);
    }
    composeArgs.push('-f', files.composeFile);
    if (existsSync(files.agentComposeFile)) {
      composeArgs.push('-f', files.agentComposeFile);
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
  );
}

export async function dockerStackDown(
  agent: string,
  worktreePath: string,
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
  return composeRun(agent, worktreePath, ['down'], false, existing);
}

export async function dockerStackRestart(
  agent: string,
  worktreePath: string,
): Promise<ComposeRunResult> {
  const existing = await findActiveProjectForAgent(agent);
  if (!existing) {
    return {
      ok: false,
      stdout: '',
      stderr: 'no active stack to restart — bring it Up first',
    };
  }
  return composeRun(agent, worktreePath, ['restart'], false, existing);
}

export async function dockerStackLogs(
  agent: string,
  worktreePath: string,
  tail = 200,
): Promise<ComposeRunResult> {
  const existing = await findActiveProjectForAgent(agent);
  return composeRun(
    agent,
    worktreePath,
    ['logs', `--tail=${tail}`, '--no-color'],
    false,
    existing ?? undefined,
  );
}
