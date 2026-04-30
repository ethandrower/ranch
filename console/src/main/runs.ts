/**
 * Read-only access to the Python orchestrator's run history.
 *
 * The Python `ranch dispatch / run / runs / approve / reject` CLI writes
 * to ~/.ranch/ranch.db (SQLite). We surface it here for the Automated
 * mode in the console. Lifecycle controls (approve/reject/stop) keep
 * happening through the Python CLI for now — this module is read-only.
 *
 * Implementation note: we shell out to /usr/bin/sqlite3 with -json flag
 * rather than pulling in a native sqlite library. Avoids another
 * electron-rebuild step. Performance is fine for the volumes ranch
 * deals with (dozens of runs).
 */

import { execFile as execFileCb } from 'node:child_process';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import type {
  RunCheckpoint,
  RunDetail,
  RunInterjection,
  RunRecord,
  RunStatus,
} from '../shared/types.js';

const execFile = promisify(execFileCb);

const RANCH_DIR = process.env.RANCH_HOME ?? join(homedir(), '.ranch');
const DB_PATH = join(RANCH_DIR, 'ranch.db');

async function query(sql: string): Promise<unknown[]> {
  if (!existsSync(DB_PATH)) return [];
  const { stdout } = await execFile('/usr/bin/sqlite3', [
    DB_PATH,
    '-json',
    sql,
  ]);
  if (!stdout.trim()) return [];
  try {
    const parsed: unknown = JSON.parse(stdout);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function asString(v: unknown): string | undefined {
  return typeof v === 'string' && v.length > 0 ? v : undefined;
}

function asNumber(v: unknown): number | undefined {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string') {
    const n = Number.parseInt(v, 10);
    if (Number.isFinite(n)) return n;
  }
  return undefined;
}

/**
 * Map the orchestrator's state vocabulary onto the smaller set the UI
 * uses. Tests-green/pre-push checkpoints flow through `needs_approval`
 * generically — we don't try to disambiguate those at the card level.
 */
function mapState(raw: unknown): RunStatus {
  switch (raw) {
    case 'planning':
      return 'planning';
    case 'needs_approval':
      return 'awaiting_approval';
    case 'in_development':
    case 'tests_green':
      return 'working';
    case 'queued':
      return 'queued';
    case 'completed':
      return 'done';
    case 'stopped':
      return 'stopped';
    case 'error':
      return 'blocked';
    default:
      return 'unknown';
  }
}

/** Trim a long brief down for display. */
function summarizeBrief(text: string | undefined, max = 200): string {
  if (!text) return '';
  const oneLine = text.replace(/\s+/g, ' ').trim();
  return oneLine.length <= max ? oneLine : oneLine.slice(0, max - 1) + '…';
}

function rowToRunRecord(row: Record<string, unknown>): RunRecord {
  const id = asNumber(row['id'])!;
  const status = mapState(row['state']);
  const brief = summarizeBrief(asString(row['initial_prompt']));
  const rec: RunRecord = {
    id,
    agent: asString(row['agent']) ?? 'unknown',
    status,
    brief,
    rawState: asString(row['state']) ?? 'unknown',
    dispatchMode:
      asString(row['dispatch_mode']) === 'background'
        ? 'background'
        : 'foreground',
  };
  const ticket = asString(row['ticket']);
  if (ticket) rec.ticket = ticket;
  const startedAt = asString(row['started_at']);
  if (startedAt) rec.startedAt = startedAt;
  const endedAt = asString(row['ended_at']);
  if (endedAt) rec.endedAt = endedAt;
  const prUrl = asString(row['pr_url']);
  if (prUrl) rec.prUrl = prUrl;
  const pid = asNumber(row['pid']);
  if (pid !== undefined) rec.pid = pid;
  const logPath = asString(row['log_path']);
  if (logPath) rec.logPath = logPath;
  const branchName = asString(row['branch_name']);
  if (branchName) rec.branchName = branchName;
  return rec;
}

function rowToCheckpoint(row: Record<string, unknown>): RunCheckpoint {
  const id = asNumber(row['id'])!;
  const cp: RunCheckpoint = {
    id,
    runId: asNumber(row['run_id'])!,
    kind: asString(row['kind']) ?? 'unknown',
    decision: (asString(row['decision']) ??
      'pending') as RunCheckpoint['decision'],
  };
  const summary = asString(row['summary']);
  if (summary) cp.summary = summary;
  const createdAt = asString(row['created_at']);
  if (createdAt) cp.createdAt = createdAt;
  const decidedAt = asString(row['decided_at']);
  if (decidedAt) cp.decidedAt = decidedAt;
  const note = asString(row['decision_note']);
  if (note) cp.decisionNote = note;
  return cp;
}

function rowToInterjection(row: Record<string, unknown>): RunInterjection {
  const inj: RunInterjection = {
    id: asNumber(row['id'])!,
    runId: asNumber(row['run_id'])!,
    kind: asString(row['kind']) ?? 'unknown',
  };
  const content = asString(row['content']);
  if (content) inj.content = content;
  const createdAt = asString(row['created_at']);
  if (createdAt) inj.createdAt = createdAt;
  const processedAt = asString(row['processed_at']);
  if (processedAt) inj.processedAt = processedAt;
  return inj;
}

export async function listRuns(limit = 50): Promise<RunRecord[]> {
  const rows = (await query(
    `SELECT id, agent, ticket, state, dispatch_mode, started_at, ended_at,
            initial_prompt, pid, log_path, branch_name, pr_url
     FROM runs
     ORDER BY id DESC
     LIMIT ${Number(limit)}`,
  )) as Record<string, unknown>[];
  return rows.map(rowToRunRecord);
}

export async function getRun(id: number): Promise<RunDetail | null> {
  const runRows = (await query(
    `SELECT id, agent, ticket, state, dispatch_mode, started_at, ended_at,
            initial_prompt, pid, log_path, branch_name, pr_url
     FROM runs WHERE id = ${Number(id)} LIMIT 1`,
  )) as Record<string, unknown>[];
  const row = runRows[0];
  if (!row) return null;
  const run = rowToRunRecord(row);

  const checkpointRows = (await query(
    `SELECT id, run_id, kind, summary, created_at, decision, decision_note,
            decided_at
     FROM checkpoints WHERE run_id = ${Number(id)} ORDER BY id`,
  )) as Record<string, unknown>[];
  const checkpoints = checkpointRows.map(rowToCheckpoint);

  const interjectionRows = (await query(
    `SELECT id, run_id, kind, content, created_at, processed_at
     FROM interjections WHERE run_id = ${Number(id)} ORDER BY id`,
  )) as Record<string, unknown>[];
  const interjections = interjectionRows.map(rowToInterjection);

  // Read the initial_prompt fully (rather than the truncated `brief` on
  // RunRecord) — detail view wants the whole thing.
  const initialPromptFull = asString(row['initial_prompt']);

  const detail: RunDetail = {
    ...run,
    checkpoints,
    interjections,
  };
  if (initialPromptFull) detail.initialPrompt = initialPromptFull;
  return detail;
}
