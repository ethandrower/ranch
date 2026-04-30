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

/**
 * Inferred state of the latest CC session, derived from the transcript tail:
 *
 *   - 'active'         very recent activity (assistant just spoke, < ~5s)
 *   - 'tool_in_flight' assistant kicked off a tool call and we haven't seen
 *                      its result yet — claude is doing work
 *   - 'awaiting_input' assistant has spoken (text-only or all tools resolved)
 *                      and the transcript has been quiet — claude is waiting
 *                      on a human reply
 *   - 'idle'           nothing recent at all (transcript stale > ~5min)
 *   - 'unknown'        couldn't determine (parse failures, no entries)
 */
export type SessionRunState =
  | 'active'
  | 'tool_in_flight'
  | 'awaiting_input'
  | 'idle'
  | 'unknown';

/** A single tool_use the assistant is running but hasn't received a result for. */
export interface InFlightTool {
  /** Tool name, e.g. "Bash", "Edit", "Read" */
  name: string;
  /** Short, human-readable summary of the tool's input */
  summary: string;
}

export interface SessionState {
  /** 'active' = transcript exists; 'none' = no transcript directory */
  status: 'active' | 'none';
  /** Newest .jsonl file discovered for this worktree */
  sessionId?: string;
  transcriptPath?: string;
  /** ISO timestamp of the last entry */
  lastActivityAt?: string;
  /**
   * Claude's most recent assistant text content — usually a "here's
   * what I just did" wrap-up. Far more useful than the user's last
   * prompt when surfacing on a card.
   */
  lastAssistantText?: string;
  /**
   * Tool currently in flight when runState === 'tool_in_flight'.
   * The first unanswered tool_use from the latest assistant turn.
   */
  currentTool?: InFlightTool;
  /** Most recent TodoWrite list — empty if the agent never used TodoWrite */
  todos: TodoItem[];
  /** Branch CC was on at the latest entry (assistant entries include `gitBranch`) */
  gitBranch?: string;
  /** Inferred run state — see SessionRunState */
  runState: SessionRunState;
}

// ─── Notes (operator-owned per-agent labels) ─────────────────────────────

export interface AgentNote {
  /** Free-form text the operator wrote for this agent */
  label: string;
  /** ISO timestamp of last edit */
  updatedAt: string;
}

// ─── Automated runs (read from ~/.ranch/ranch.db) ────────────────────────

/**
 * Card-level status that the UI distinguishes between. Maps from the
 * orchestrator's wider state vocabulary in src/main/runs.ts. The UI
 * groups multiple raw states under one status when they look the same
 * to the operator.
 */
export type RunStatus =
  | 'planning'
  | 'queued'
  | 'working'
  | 'awaiting_approval'
  | 'done'
  | 'stopped'
  | 'blocked'
  | 'abandoned'
  | 'unknown';

export interface RunRecord {
  id: number;
  agent: string;
  ticket?: string;
  status: RunStatus;
  /** The orchestrator's raw state string — useful for tooltips. */
  rawState: string;
  /** Truncated initial_prompt for the card. */
  brief: string;
  startedAt?: string;
  endedAt?: string;
  dispatchMode: 'foreground' | 'background';
  prUrl?: string;
  pid?: number;
  /**
   * Was the recorded PID still a live process at snapshot time?
   *   true  — process exists and answers signal 0
   *   false — pid was set but the process is gone (zombie)
   *   undefined — no pid recorded for this run
   */
  pidAlive?: boolean;
  logPath?: string;
  branchName?: string;
}

export interface RunCheckpoint {
  id: number;
  runId: number;
  kind: string;
  summary?: string;
  createdAt?: string;
  decision: 'pending' | 'approved' | 'rejected' | string;
  decisionNote?: string;
  decidedAt?: string;
}

export interface RunInterjection {
  id: number;
  runId: number;
  kind: string;
  content?: string;
  createdAt?: string;
  processedAt?: string;
}

export interface RunDetail extends RunRecord {
  /** The full, untruncated initial_prompt. */
  initialPrompt?: string;
  checkpoints: RunCheckpoint[];
  interjections: RunInterjection[];
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
  notes: {
    /** Returns notes keyed by agent name. Missing key = no note. */
    getAll: () => Promise<Record<string, AgentNote>>;
    /** Pass empty string to clear. */
    set: (agent: string, label: string) => Promise<AgentNote | null>;
  };
  runs: {
    list: (limit?: number) => Promise<RunRecord[]>;
    get: (id: number) => Promise<RunDetail | null>;
    /**
     * Mark stale/abandoned runs (active state, dead PID) as stopped.
     * Returns the number of rows updated.
     */
    cleanupAbandoned: () => Promise<number>;
    /** Lifecycle — each shells out to the Python `ranch` CLI. */
    approve: (id: number, note?: string) => Promise<void>;
    reject: (id: number, reason?: string) => Promise<void>;
    note: (id: number, text: string) => Promise<void>;
    stop: (id: number) => Promise<void>;
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
    /** Kill the tmux session for an agent (recovery from hangs). */
    killSession: (agent: string) => Promise<void>;
    /**
     * Send keys to an agent's tmux session — accepts both plain strings
     * and tmux key names like `C-c`, `Enter`, `Escape`.
     */
    sendKeys: (agent: string, keys: string[]) => Promise<void>;
    onData: (handler: (event: TerminalDataEvent) => void) => Unsubscribe;
    onExit: (handler: (event: TerminalExitEvent) => void) => Unsubscribe;
  };
  app: {
    version: () => Promise<string>;
    /** Open a path in the OS file manager. */
    revealInFinder: (path: string) => Promise<void>;
    /** Open a URL in the user's default browser (or whatever shell handles it). */
    openExternal: (url: string) => Promise<void>;
  };
}

declare global {
  interface Window {
    ranch: RanchApi;
  }
}
