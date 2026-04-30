/**
 * MVP-3 — find a worktree's most-recent CC session transcript and
 * extract the live state we want to surface on the card:
 *
 *   - latest TodoWrite list (the killer feature)
 *   - most recent user prompt text (for topic derivation)
 *   - last-activity timestamp
 *   - gitBranch CC last recorded
 *
 * Transcript layout (CC convention):
 *   ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl
 *
 * where `<encoded-cwd>` is the absolute worktree path with `/` replaced
 * by `-`. Each line is one event. We walk the file backward to avoid
 * loading megabytes when the only thing we need is the tail.
 */

import { readdir, readFile, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import type {
  SessionRunState,
  SessionState,
  TodoItem,
  TodoStatus,
} from '../shared/types.js';

const ACTIVE_WINDOW_MS = 5_000;
const IDLE_THRESHOLD_MS = 5 * 60_000;

const PROJECTS_DIR = join(homedir(), '.claude', 'projects');

export function encodeWorktreePath(worktreePath: string): string {
  // Drop the leading slash so we don't end up with double dashes.
  const trimmed = worktreePath.replace(/^\/+/, '');
  return '-' + trimmed.replace(/\//g, '-');
}

/**
 * The directory CC writes session JSONL to. Returns null if no
 * directory exists yet (i.e. CC has never been run for this worktree).
 */
function projectsDirFor(worktreePath: string): string | null {
  const dir = join(PROJECTS_DIR, encodeWorktreePath(worktreePath));
  return existsSync(dir) ? dir : null;
}

interface SessionFileInfo {
  sessionId: string;
  path: string;
  mtimeMs: number;
}

async function findMostRecentSession(
  dir: string,
): Promise<SessionFileInfo | null> {
  const entries = await readdir(dir);
  let newest: SessionFileInfo | null = null;
  for (const name of entries) {
    if (!name.endsWith('.jsonl')) continue;
    const path = join(dir, name);
    const s = await stat(path);
    if (!s.isFile()) continue;
    if (newest === null || s.mtimeMs > newest.mtimeMs) {
      newest = {
        sessionId: name.replace(/\.jsonl$/, ''),
        path,
        mtimeMs: s.mtimeMs,
      };
    }
  }
  return newest;
}

interface ParsedEntry {
  raw: Record<string, unknown>;
}

/**
 * Parse a single JSONL line. Real transcripts can have partial writes
 * mid-stream, so we tolerate failure and let the caller skip.
 */
function parseLine(line: string): ParsedEntry | null {
  const trimmed = line.trim();
  if (!trimmed) return null;
  try {
    const raw: unknown = JSON.parse(trimmed);
    if (typeof raw === 'object' && raw !== null && !Array.isArray(raw)) {
      return { raw: raw as Record<string, unknown> };
    }
  } catch {
    // Partial line during a concurrent write; skip.
  }
  return null;
}

function asString(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined;
}

function asArray(v: unknown): unknown[] | undefined {
  return Array.isArray(v) ? v : undefined;
}

function asObject(v: unknown): Record<string, unknown> | undefined {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : undefined;
}

function getMessageContent(entry: Record<string, unknown>): unknown[] {
  const message = asObject(entry['message']);
  if (message) {
    const c = asArray(message['content']);
    if (c) return c;
  }
  const direct = asArray(entry['content']);
  return direct ?? [];
}

function isTodoStatus(v: unknown): v is TodoStatus {
  return v === 'pending' || v === 'in_progress' || v === 'completed';
}

function extractTodos(entry: Record<string, unknown>): TodoItem[] | null {
  const content = getMessageContent(entry);
  for (const block of content) {
    const obj = asObject(block);
    if (!obj) continue;
    if (obj['type'] !== 'tool_use') continue;
    if (obj['name'] !== 'TodoWrite') continue;
    const input = asObject(obj['input']);
    const todosRaw = asArray(input?.['todos']);
    if (!todosRaw) continue;
    const todos: TodoItem[] = [];
    for (const t of todosRaw) {
      const todoObj = asObject(t);
      if (!todoObj) continue;
      const content = asString(todoObj['content']);
      const status = todoObj['status'];
      if (!content || !isTodoStatus(status)) continue;
      const item: TodoItem = { content, status };
      const activeForm = asString(todoObj['activeForm']);
      if (activeForm !== undefined) item.activeForm = activeForm;
      todos.push(item);
    }
    return todos;
  }
  return null;
}

function extractUserPromptText(entry: Record<string, unknown>): string | null {
  const t = entry['type'];
  if (t !== 'user') return null;
  const message = asObject(entry['message']);
  if (!message) return null;
  const content = message['content'];
  if (typeof content === 'string') {
    const trimmed = content.trim();
    return trimmed.length > 0 ? trimmed : null;
  }
  const arr = asArray(content);
  if (!arr) return null;
  for (const block of arr) {
    const obj = asObject(block);
    if (!obj) continue;
    if (obj['type'] === 'text') {
      const text = asString(obj['text']);
      if (text && text.trim().length > 0) return text.trim();
    }
  }
  return null;
}

interface TranscriptScan {
  todos: TodoItem[];
  lastUserPrompt?: string;
  lastActivityAt?: string;
  gitBranch?: string;
  runState: SessionRunState;
}

/**
 * Pull the tool_use IDs from an assistant entry's content blocks.
 */
function extractAssistantToolUseIds(entry: Record<string, unknown>): string[] {
  if (entry['type'] !== 'assistant') return [];
  const ids: string[] = [];
  for (const block of getMessageContent(entry)) {
    const obj = asObject(block);
    if (!obj) continue;
    if (obj['type'] !== 'tool_use') continue;
    const id = asString(obj['id']);
    if (id) ids.push(id);
  }
  return ids;
}

/**
 * Pull tool_result IDs that this user entry is responding to. CC writes
 * these as `type: user` with a `content` array containing tool_result
 * blocks (each carrying `tool_use_id`).
 */
function extractToolResultIds(entry: Record<string, unknown>): string[] {
  if (entry['type'] !== 'user') return [];
  const ids: string[] = [];
  for (const block of getMessageContent(entry)) {
    const obj = asObject(block);
    if (!obj) continue;
    if (obj['type'] !== 'tool_result') continue;
    const id = asString(obj['tool_use_id']);
    if (id) ids.push(id);
  }
  return ids;
}

/**
 * Decide what state the session is in based on the latest assistant turn,
 * subsequent tool results, and how long ago the last entry was written.
 */
function inferRunState(
  entries: ParsedEntry[],
  lastActivityAt: string | undefined,
  now: number,
): SessionRunState {
  if (entries.length === 0) return 'unknown';

  const lastTs = lastActivityAt ? Date.parse(lastActivityAt) : NaN;
  const ageMs = Number.isFinite(lastTs) ? now - lastTs : Infinity;

  // Find the latest assistant entry.
  let latestAssistantIdx = -1;
  for (let i = entries.length - 1; i >= 0; i--) {
    if (entries[i]!.raw['type'] === 'assistant') {
      latestAssistantIdx = i;
      break;
    }
  }
  if (latestAssistantIdx < 0) {
    // No assistant entries seen — typically means user just opened a session.
    return ageMs < ACTIVE_WINDOW_MS ? 'active' : 'idle';
  }

  // Tool uses requested in the latest assistant turn.
  const toolUseIds = extractAssistantToolUseIds(
    entries[latestAssistantIdx]!.raw,
  );

  if (toolUseIds.length > 0) {
    // Walk forward looking for matching tool_results.
    const responded = new Set<string>();
    for (let i = latestAssistantIdx + 1; i < entries.length; i++) {
      for (const id of extractToolResultIds(entries[i]!.raw)) {
        responded.add(id);
      }
    }
    const allAnswered = toolUseIds.every((id) => responded.has(id));
    if (!allAnswered) {
      // Tool call queued without a result yet — claude is mid-work
      // (or the tool's hung; either way, not "awaiting input").
      return 'tool_in_flight';
    }
  }

  // Latest assistant turn either had no tools or all tools resolved.
  if (ageMs < ACTIVE_WINDOW_MS) return 'active';
  if (ageMs < IDLE_THRESHOLD_MS) return 'awaiting_input';
  return 'idle';
}

/**
 * Walk entries newest → oldest. Stop early once we have everything.
 */
function scanEntries(entries: ParsedEntry[]): TranscriptScan {
  const result: TranscriptScan = { todos: [], runState: 'unknown' };
  let foundTodos = false;
  let foundPrompt = false;

  // First pass: capture lastActivityAt + gitBranch from the newest entry
  // that has them. Walk backward.
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i]!.raw;
    if (result.lastActivityAt === undefined) {
      const ts = asString(entry['timestamp']);
      if (ts) result.lastActivityAt = ts;
    }
    if (result.gitBranch === undefined) {
      const gb = asString(entry['gitBranch']);
      if (gb) result.gitBranch = gb;
    }
    if (!foundTodos) {
      const todos = extractTodos(entry);
      if (todos) {
        result.todos = todos;
        foundTodos = true;
      }
    }
    if (!foundPrompt) {
      const prompt = extractUserPromptText(entry);
      if (prompt) {
        result.lastUserPrompt = prompt;
        foundPrompt = true;
      }
    }
    if (
      foundTodos &&
      foundPrompt &&
      result.lastActivityAt !== undefined &&
      result.gitBranch !== undefined
    ) {
      break;
    }
  }

  result.runState = inferRunState(entries, result.lastActivityAt, Date.now());
  return result;
}

export async function getActiveSession(
  worktreePath: string,
): Promise<SessionState> {
  const dir = projectsDirFor(worktreePath);
  if (!dir) {
    return { status: 'none', todos: [], runState: 'unknown' };
  }
  const newest = await findMostRecentSession(dir);
  if (!newest) {
    return { status: 'none', todos: [], runState: 'unknown' };
  }
  const raw = await readFile(newest.path, 'utf8');
  const entries: ParsedEntry[] = [];
  for (const line of raw.split('\n')) {
    const parsed = parseLine(line);
    if (parsed) entries.push(parsed);
  }
  const scan = scanEntries(entries);
  const state: SessionState = {
    status: 'active',
    sessionId: newest.sessionId,
    transcriptPath: newest.path,
    todos: scan.todos,
    runState: scan.runState,
  };
  if (scan.lastActivityAt !== undefined)
    state.lastActivityAt = scan.lastActivityAt;
  if (scan.lastUserPrompt !== undefined)
    state.lastUserPrompt = scan.lastUserPrompt;
  if (scan.gitBranch !== undefined) state.gitBranch = scan.gitBranch;
  return state;
}
