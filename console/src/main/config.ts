import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import toml from '@iarna/toml';
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
