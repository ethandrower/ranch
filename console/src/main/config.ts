import { execFile as execFileCb } from 'node:child_process';
import { readFile, writeFile, rename, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { promisify } from 'node:util';
import toml from '@iarna/toml';

const execFile = promisify(execFileCb);
import type {
  AgentConfig,
  ProjectConfig,
  RanchConfig,
  WorktreePorts,
} from '../shared/types.js';

const RANCH_DIR = process.env.RANCH_HOME ?? join(homedir(), '.ranch');
const CONFIG_PATH = join(RANCH_DIR, 'config.toml');
const PROJECTS_PATH = join(RANCH_DIR, 'projects.toml');

interface AgentSection {
  worktree?: unknown;
  description?: unknown;
  ports?: unknown;
  docker?: unknown;
}

function parsePortsBlock(raw: unknown): WorktreePorts | undefined {
  if (!isObject(raw)) return undefined;
  const out: WorktreePorts = {};
  for (const key of ['django', 'vite'] as const) {
    const v = raw[key];
    if (typeof v === 'number' && Number.isFinite(v) && v > 0 && v < 65536) {
      out[key] = v;
    }
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

function parseDockerBlock(raw: unknown): AgentConfig['docker'] | undefined {
  if (!isObject(raw)) return undefined;
  const out: NonNullable<AgentConfig['docker']> = {};
  // Accept both kebab and snake key names for forgiveness.
  const filesRaw =
    raw['compose_files'] ?? raw['composeFiles'] ?? raw['compose-files'];
  if (Array.isArray(filesRaw)) {
    const files = filesRaw.filter((s): s is string => typeof s === 'string');
    if (files.length > 0) out.composeFiles = files;
  }
  const envFile = raw['env_file'] ?? raw['envFile'] ?? raw['env-file'];
  if (typeof envFile === 'string' && envFile.length > 0) out.envFile = envFile;
  const project =
    raw['project_name'] ?? raw['projectName'] ?? raw['project-name'];
  if (typeof project === 'string' && project.length > 0) {
    out.projectName = project;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

interface ProjectSection {
  path?: unknown;
  label?: unknown;
  config?: unknown;
  enabled?: unknown;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseAgents(raw: unknown): AgentConfig[] {
  if (!isObject(raw)) return [];
  const agentsBlock = raw['agents'];
  if (!isObject(agentsBlock)) return [];
  const agents: AgentConfig[] = [];
  for (const [name, value] of Object.entries(agentsBlock)) {
    if (!isObject(value)) continue;
    const section = value as AgentSection;
    if (typeof section.worktree !== 'string') continue;
    const agent: AgentConfig = {
      name,
      worktree: section.worktree,
    };
    if (typeof section.description === 'string') {
      agent.description = section.description;
    }
    const ports = parsePortsBlock(section.ports);
    if (ports !== undefined) agent.ports = ports;
    const docker = parseDockerBlock(section.docker);
    if (docker !== undefined) agent.docker = docker;
    agents.push(agent);
  }
  return agents;
}

function parseProjects(raw: unknown): ProjectConfig[] {
  if (!isObject(raw)) return [];
  const projectsBlock = raw['projects'];
  if (!isObject(projectsBlock)) return [];
  const projects: ProjectConfig[] = [];
  for (const [name, value] of Object.entries(projectsBlock)) {
    if (!isObject(value)) continue;
    const section = value as ProjectSection;
    if (typeof section.path !== 'string') continue;
    const project: ProjectConfig = {
      name,
      path: section.path,
      enabled: section.enabled !== false,
    };
    if (typeof section.label === 'string') project.label = section.label;
    if (typeof section.config === 'string') project.config = section.config;
    projects.push(project);
  }
  return projects;
}

async function readToml(path: string): Promise<unknown> {
  if (!existsSync(path)) return {};
  const raw = await readFile(path, 'utf8');
  return toml.parse(raw);
}

export async function loadRanchConfig(): Promise<RanchConfig> {
  const [agentsToml, projectsToml] = await Promise.all([
    readToml(CONFIG_PATH),
    readToml(PROJECTS_PATH),
  ]);

  return {
    agents: parseAgents(agentsToml),
    projects: parseProjects(projectsToml),
    configPath: CONFIG_PATH,
    projectsPath: PROJECTS_PATH,
    ranchDir: RANCH_DIR,
  };
}

// ─── Add agent (write side) ────────────────────────────────────

export interface AddAgentInput {
  /** New agent name (e.g. "stevie"). Must be unique. */
  name: string;
  /** Worktree path. If omitted, defaults to $(HOME)/code/citemed/<name>. */
  worktree?: string;
  description?: string;
  /** Optional canonical port hints (these go into the ports block). */
  djangoPort?: number;
  vitePort?: number;
  /**
   * If true, ranch shells out to `make init-agent AGENT=<name>` from
   * citemed_web's Makefile. Creates the git worktree, generates
   * .env.agent, runs migrations. Requires the agent name to already be
   * in the Makefile's AGENTS list — currently a manual edit step.
   */
  runMakeInitAgent?: boolean;
}

export interface AddAgentResult {
  ok: boolean;
  /** Combined stdout/stderr from `make init-agent` if runMakeInitAgent. */
  output: string;
  /** The agent block that was appended to ~/.ranch/config.toml. */
  configEntry?: string;
}

/** Locate any worktree we know about that contains a Makefile (citemed_web). */
async function findCitemedWebMakefile(): Promise<string | null> {
  const file = await readToml(CONFIG_PATH);
  const agents = parseAgents(file);
  for (const a of agents) {
    const mf = join(a.worktree, 'Makefile');
    if (existsSync(mf)) return a.worktree;
  }
  return null;
}

function defaultWorktreePath(name: string): string {
  return join(homedir(), 'code', 'citemed', name);
}

function escapeTomlString(s: string): string {
  // For our purposes the values are paths and short text — escape only
  // backslash and double-quote, fine for basic strings.
  return s.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function buildAgentTomlBlock(input: AddAgentInput, worktree: string): string {
  const lines = [`[agents.${input.name}]`];
  lines.push(`worktree = "${escapeTomlString(worktree)}"`);
  if (input.description) {
    lines.push(`description = "${escapeTomlString(input.description)}"`);
  }
  if (input.djangoPort !== undefined || input.vitePort !== undefined) {
    const parts: string[] = [];
    if (input.djangoPort !== undefined)
      parts.push(`django = ${input.djangoPort}`);
    if (input.vitePort !== undefined) parts.push(`vite = ${input.vitePort}`);
    lines.push(`ports = { ${parts.join(', ')} }`);
  }
  return lines.join('\n');
}

async function appendToConfigFile(block: string): Promise<void> {
  const dir = dirname(CONFIG_PATH);
  if (!existsSync(dir)) await mkdir(dir, { recursive: true });
  const existing = existsSync(CONFIG_PATH)
    ? await readFile(CONFIG_PATH, 'utf8')
    : '';
  // Ensure a separating blank line, regardless of whether existing
  // file ends with newline or not.
  const sep = existing.endsWith('\n\n')
    ? ''
    : existing.endsWith('\n')
      ? '\n'
      : '\n\n';
  const next = existing + sep + block + '\n';
  const tmp = `${CONFIG_PATH}.${process.pid}.tmp`;
  await writeFile(tmp, next, 'utf8');
  await rename(tmp, CONFIG_PATH);
}

/**
 * Read just the ports out of a freshly-created .env.agent. Used after
 * `make init-agent` to capture the canonical ports it allocated.
 */
async function readEnvAgentPorts(
  worktree: string,
): Promise<{ django?: number; vite?: number }> {
  const path = join(worktree, '.env.agent');
  if (!existsSync(path)) return {};
  const raw = await readFile(path, 'utf8');
  const out: { django?: number; vite?: number } = {};
  for (const line of raw.split(/\r?\n/)) {
    const m = /^([A-Z_]+)=(.+)$/.exec(line.trim());
    if (!m) continue;
    const [, key, value] = m;
    const n = Number.parseInt(value!, 10);
    if (!Number.isFinite(n)) continue;
    if (key === 'DJANGO_PORT') out.django = n;
    if (key === 'VITE_PORT') out.vite = n;
  }
  return out;
}

export async function addAgent(input: AddAgentInput): Promise<AddAgentResult> {
  const trimmedName = input.name.trim();
  if (!/^[a-z][a-z0-9_-]*$/.test(trimmedName)) {
    return {
      ok: false,
      output:
        'Agent name must be lowercase letters/digits/hyphens, starting with a letter.',
    };
  }
  // Reject duplicates.
  const config = await loadRanchConfig();
  if (config.agents.some((a) => a.name === trimmedName)) {
    return { ok: false, output: `Agent '${trimmedName}' already exists.` };
  }

  const worktree = (input.worktree ?? defaultWorktreePath(trimmedName)).trim();

  let output = '';
  let djangoPort = input.djangoPort;
  let vitePort = input.vitePort;

  if (input.runMakeInitAgent) {
    const makefileDir = await findCitemedWebMakefile();
    if (!makefileDir) {
      return {
        ok: false,
        output:
          'No Makefile found in any registered worktree. Either run `make init-agent` manually, or untoggle the option and add the config entry only.',
      };
    }
    try {
      const { stdout, stderr } = await execFile('make', [
        '-C',
        makefileDir,
        'init-agent',
        `AGENT=${trimmedName}`,
      ]);
      output = stdout + stderr;
    } catch (err) {
      const stderr =
        err && typeof err === 'object' && 'stderr' in err
          ? String((err as { stderr: unknown }).stderr ?? '')
          : err instanceof Error
            ? err.message
            : String(err);
      return {
        ok: false,
        output: stderr,
      };
    }
    // Pick up ports the Makefile allocated.
    const detected = await readEnvAgentPorts(worktree);
    if (detected.django !== undefined && djangoPort === undefined)
      djangoPort = detected.django;
    if (detected.vite !== undefined && vitePort === undefined)
      vitePort = detected.vite;
  }

  const block = buildAgentTomlBlock(
    {
      ...input,
      name: trimmedName,
      worktree,
      ...(djangoPort !== undefined ? { djangoPort } : {}),
      ...(vitePort !== undefined ? { vitePort } : {}),
    },
    worktree,
  );
  await appendToConfigFile(block);

  return {
    ok: true,
    output,
    configEntry: block,
  };
}
