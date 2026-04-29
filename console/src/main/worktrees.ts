/**
 * MVP-1 — read per-worktree basics from .env.agent.
 *
 * This module never writes. Source of truth is whatever
 * citemed_web's `make init-agent` already produced; ranch
 * just observes.
 */

import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { loadRanchConfig } from './config.js';
import type {
  WorktreeBasics,
  WorktreePorts,
  AgentConfig,
} from '../shared/types.js';

const ENV_AGENT_FILENAME = '.env.agent';

/**
 * Parse a minimal subset of dotenv syntax: `KEY=VALUE` per line, ignoring
 * comments and blank lines. We deliberately don't run shell expansion or
 * variable substitution — `.env.agent` for the four hardcoded worktrees
 * has flat literal values and we want a parser that can't surprise us.
 */
function parseDotEnv(contents: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    // Strip matching surrounding quotes if present.
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

function parsePort(raw: string | undefined): number | undefined {
  if (!raw) return undefined;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 && n < 65536 ? n : undefined;
}

async function readEnvAgent(envAgentPath: string): Promise<WorktreePorts> {
  if (!existsSync(envAgentPath)) return {};
  const contents = await readFile(envAgentPath, 'utf8');
  const parsed = parseDotEnv(contents);
  const ports: WorktreePorts = {};
  const django = parsePort(parsed['DJANGO_PORT']);
  const vite = parsePort(parsed['VITE_PORT']);
  if (django !== undefined) ports.django = django;
  if (vite !== undefined) ports.vite = vite;
  return ports;
}

async function inspectAgent(agent: AgentConfig): Promise<WorktreeBasics> {
  const envAgentPath = join(agent.worktree, ENV_AGENT_FILENAME);
  const envAgentExists = existsSync(envAgentPath);
  const ports = envAgentExists ? await readEnvAgent(envAgentPath) : {};
  const basics: WorktreeBasics = {
    agent: agent.name,
    worktreePath: agent.worktree,
    envAgentPath,
    envAgentExists,
    ports,
  };
  if (agent.description !== undefined) {
    basics.description = agent.description;
  }
  return basics;
}

export async function listWorktrees(): Promise<WorktreeBasics[]> {
  const config = await loadRanchConfig();
  return Promise.all(config.agents.map(inspectAgent));
}
