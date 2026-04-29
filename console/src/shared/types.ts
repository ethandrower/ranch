/**
 * Shared types — the IPC contract between main, preload, and renderer.
 *
 * Anything the renderer can call lives in `RanchApi`. Adding a capability
 * is always a triangle:
 *   1. Define types here
 *   2. Implement in src/main/<module>.ts
 *   3. Register the channel in src/main/ipc.ts
 *   4. Expose via contextBridge in src/preload/index.ts
 *   5. Consume from React via window.ranch.<namespace>.<method>()
 */

// ─── Agent registry (from ~/.ranch/config.toml) ──────────────────────────

export interface AgentConfig {
  name: string;
  worktree: string;
  description?: string;
}

export interface ProjectConfig {
  name: string;
  path: string;
  label?: string;
  config?: string;
  enabled: boolean;
}

export interface RanchConfig {
  agents: AgentConfig[];
  projects: ProjectConfig[];
  configPath: string;
  projectsPath: string;
  ranchDir: string;
}

// ─── Worktree basics (MVP-1: from .env.agent) ────────────────────────────

export interface WorktreePorts {
  django?: number;
  vite?: number;
}

export interface WorktreeBasics {
  agent: string;
  worktreePath: string;
  description?: string;
  envAgentPath: string;
  envAgentExists: boolean;
  ports: WorktreePorts;
}

// ─── CC session state (MVP-3: from ~/.claude/projects/<encoded>/*.jsonl) ─

export type TodoStatus = 'pending' | 'in_progress' | 'completed';

export interface TodoItem {
  content: string;
  activeForm?: string;
  status: TodoStatus;
}

export interface SessionState {
  /** 'active' = transcript exists; 'none' = no transcript directory */
  status: 'active' | 'none';
  /** Newest .jsonl file discovered for this worktree */
  sessionId?: string;
  transcriptPath?: string;
  /** ISO timestamp of the last entry */
  lastActivityAt?: string;
  /** Most recent user prompt text (for "topic" derivation in the card) */
  lastUserPrompt?: string;
  /** Most recent TodoWrite list — empty if the agent never used TodoWrite */
  todos: TodoItem[];
  /** Branch CC was on at the latest entry (assistant entries include `gitBranch`) */
  gitBranch?: string;
}

// ─── IPC surface ─────────────────────────────────────────────────────────

export interface RanchApi {
  config: {
    get: () => Promise<RanchConfig>;
  };
  worktrees: {
    list: () => Promise<WorktreeBasics[]>;
    session: (agent: string) => Promise<SessionState>;
  };
  app: {
    version: () => Promise<string>;
  };
}

declare global {
  interface Window {
    ranch: RanchApi;
  }
}
