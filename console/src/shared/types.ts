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
  /**
   * Operator-canonical ports for this agent (lives in ~/.ranch/config.toml,
   * out of reach of the agents themselves). When set, the card displays
   * these and warns if .env.agent disagrees.
   */
  ports?: WorktreePorts;
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
  /** AGENT_NAME from .env.agent — useful for catching stale env files */
  envAgentName?: string;
  /** True if envAgentName matches the registered agent name */
  envAgentMatches: boolean;
  /**
   * Ports surfaced on the card. Sourced from `agents.<name>.ports` in
   * ~/.ranch/config.toml when present (operator-canonical), otherwise
   * fall back to whatever `.env.agent` had (potentially stale).
   */
  ports: WorktreePorts;
  /** Where `ports` came from. */
  portsSource: 'ranch-config' | 'env-agent' | 'unknown';
  /** Ports as read directly from .env.agent — used to detect drift. */
  envAgentPorts: WorktreePorts;
}

// ─── Git state (MVP-2) ──────────────────────────────────────────────────

export interface GitLastCommit {
  sha: string;
  message: string;
  age: string;
}

export type WorktreeGitState =
  | { status: 'no-git' }
  | {
      status: 'ok';
      branch: string;
      dirty: boolean;
      ahead?: number;
      behind?: number;
      lastCommit?: GitLastCommit;
    };

// ─── Process state (MVP-4) ──────────────────────────────────────────────

export interface TmuxSessionState {
  sessionName: string;
  exists: boolean;
  attachedClients: number;
  createdAt?: string;
}

export interface ClaudeProcess {
  pid: number;
  ppid: number;
  command: string;
  cwd?: string;
}

export interface CCProcessState {
  tmux: TmuxSessionState | null;
  claudeRunning: boolean;
  claudeProcesses: ClaudeProcess[];
}

export interface ProcessSnapshot {
  /** Per-registered-agent process state. */
  perAgent: Record<string, CCProcessState>;
  /**
   * Claude processes whose cwd doesn't match any registered worktree —
   * either truly outside the agent fleet (e.g. a manual `claude` in
   * Documents/) or in an unregistered subproject.
   */
  orphanClaudes: ClaudeProcess[];
  /** Total number of claude processes seen across all categories. */
  totalClaudes: number;
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

// ─── Terminal (MVP-6) ────────────────────────────────────────────────────

export interface TerminalEnv {
  tmuxAvailable: boolean;
  tmuxPath?: string;
}

export type TerminalAttachResult =
  | { ok: true; terminalId: string }
  | { ok: false; reason: string };

/** Renderer-side options for ranch.terminal.attach. */
export interface TerminalAttachOptions {
  cols?: number;
  rows?: number;
  /**
   * Command to run inside the tmux session if it's being newly created.
   * Ignored if the session already exists (tmux -A semantics). Use this
   * to launch `claude` immediately on first open, while still attaching
   * cleanly on subsequent opens.
   */
  command?: string;
}

export interface TerminalDataEvent {
  terminalId: string;
  data: string;
}

export interface TerminalExitEvent {
  terminalId: string;
  exitCode: number;
  signal: number | null;
}

/**
 * Renderer-side subscription handle. Calling the function unsubscribes —
 * standard React-effect-cleanup pattern.
 */
export type Unsubscribe = () => void;

// ─── IPC surface ─────────────────────────────────────────────────────────

export interface RanchApi {
  config: {
    get: () => Promise<RanchConfig>;
  };
  worktrees: {
    list: () => Promise<WorktreeBasics[]>;
    session: (agent: string) => Promise<SessionState>;
    git: (agent: string) => Promise<WorktreeGitState>;
    /** Fleet snapshot — one ps + one tmux-list per call, all agents. */
    processSnapshot: () => Promise<ProcessSnapshot>;
  };
  terminal: {
    env: () => Promise<TerminalEnv>;
    attach: (
      agent: string,
      opts?: TerminalAttachOptions,
    ) => Promise<TerminalAttachResult>;
    write: (terminalId: string, data: string) => Promise<void>;
    resize: (terminalId: string, cols: number, rows: number) => Promise<void>;
    detach: (terminalId: string) => Promise<void>;
    onData: (handler: (event: TerminalDataEvent) => void) => Unsubscribe;
    onExit: (handler: (event: TerminalExitEvent) => void) => Unsubscribe;
  };
  app: {
    version: () => Promise<string>;
    /** Open a path in the OS file manager. */
    revealInFinder: (path: string) => Promise<void>;
  };
}

declare global {
  interface Window {
    ranch: RanchApi;
  }
}
