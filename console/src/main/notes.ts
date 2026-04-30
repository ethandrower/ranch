/**
 * Operator-owned per-agent notes — free-form labels like
 * "max will work on scrapers tickets today".
 *
 * Lives at ~/.ranch/notes.json (operator's home dir, intentionally
 * out of every agent's worktree so a stray rm/edit can't blow them
 * away). Writes are atomic: write to a tmp file, rename onto target.
 */

import { readFile, writeFile, rename, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import type { AgentNote } from '../shared/types.js';

const RANCH_DIR = process.env.RANCH_HOME ?? join(homedir(), '.ranch');
const NOTES_PATH = join(RANCH_DIR, 'notes.json');

interface NotesFile {
  agents: Record<string, AgentNote>;
}

function emptyFile(): NotesFile {
  return { agents: {} };
}

async function readFileSafe(): Promise<NotesFile> {
  if (!existsSync(NOTES_PATH)) return emptyFile();
  try {
    const raw = await readFile(NOTES_PATH, 'utf8');
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      'agents' in parsed &&
      typeof (parsed as { agents: unknown }).agents === 'object' &&
      (parsed as { agents: unknown }).agents !== null
    ) {
      const agents = (parsed as { agents: Record<string, unknown> }).agents;
      const out: Record<string, AgentNote> = {};
      for (const [k, v] of Object.entries(agents)) {
        if (
          typeof v === 'object' &&
          v !== null &&
          typeof (v as AgentNote).label === 'string' &&
          typeof (v as AgentNote).updatedAt === 'string'
        ) {
          out[k] = {
            label: (v as AgentNote).label,
            updatedAt: (v as AgentNote).updatedAt,
          };
        }
      }
      return { agents: out };
    }
  } catch {
    // corrupt file — fall through to empty
  }
  return emptyFile();
}

async function writeFileAtomic(data: NotesFile): Promise<void> {
  const dir = dirname(NOTES_PATH);
  if (!existsSync(dir)) await mkdir(dir, { recursive: true });
  const tmp = `${NOTES_PATH}.${process.pid}.tmp`;
  await writeFile(tmp, JSON.stringify(data, null, 2), 'utf8');
  await rename(tmp, NOTES_PATH);
}

export async function getAllNotes(): Promise<Record<string, AgentNote>> {
  const file = await readFileSafe();
  return file.agents;
}

export async function setNote(
  agent: string,
  label: string,
): Promise<AgentNote | null> {
  const file = await readFileSafe();
  const trimmed = label.trim();
  if (trimmed.length === 0) {
    delete file.agents[agent];
    await writeFileAtomic(file);
    return null;
  }
  const note: AgentNote = {
    label: trimmed,
    updatedAt: new Date().toISOString(),
  };
  file.agents[agent] = note;
  await writeFileAtomic(file);
  return note;
}
