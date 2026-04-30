/**
 * MVP-2 — per-worktree git state.
 *
 * We shell out to `git` rather than pull in a library. A handful of
 * commands and we don't have to worry about libgit2 native rebuild
 * across Electron versions.
 *
 * The `git -C <path>` form runs as if cwd were <path>, without changing
 * our process's actual cwd — important when polling four worktrees in
 * parallel.
 */

import { execFile as execFileCb } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { promisify } from 'node:util';
import type { GitLastCommit, WorktreeGitState } from '../shared/types.js';

const execFile = promisify(execFileCb);

const COMPARE_BRANCH = 'origin/develop';

async function git(
  cwd: string,
  args: string[],
): Promise<{ stdout: string; stderr: string; ok: boolean }> {
  try {
    const { stdout, stderr } = await execFile('git', ['-C', cwd, ...args], {
      // 1 MB is plenty for everything we run here
      maxBuffer: 1024 * 1024,
    });
    return { stdout, stderr, ok: true };
  } catch (err) {
    const stderr =
      err && typeof err === 'object' && 'stderr' in err
        ? String((err as { stderr: unknown }).stderr ?? '')
        : '';
    return { stdout: '', stderr, ok: false };
  }
}

export async function getWorktreeGitState(
  worktreePath: string,
): Promise<WorktreeGitState> {
  // Cheap precheck. `.git` may be a directory (regular repo) or a file
  // (linked worktree, points at .git/worktrees/<name>).
  if (!existsSync(join(worktreePath, '.git'))) {
    return { status: 'no-git' };
  }

  const branchRes = await git(worktreePath, [
    'rev-parse',
    '--abbrev-ref',
    'HEAD',
  ]);
  if (!branchRes.ok) {
    return { status: 'no-git' };
  }
  const branch = branchRes.stdout.trim();

  // Run the rest in parallel — they're all independent reads.
  const [statusRes, aheadBehindRes, lastCommitRes] = await Promise.all([
    git(worktreePath, ['status', '--porcelain']),
    git(worktreePath, [
      'rev-list',
      '--left-right',
      '--count',
      `${COMPARE_BRANCH}...HEAD`,
    ]),
    git(worktreePath, ['log', '-1', '--format=%h%x09%s%x09%cr']),
  ]);

  const dirty =
    statusRes.ok && statusRes.stdout.split(/\r?\n/).some((l) => l.length > 0);

  let behind: number | undefined;
  let ahead: number | undefined;
  if (aheadBehindRes.ok) {
    // Output: "<behind>\t<ahead>"
    const parts = aheadBehindRes.stdout.trim().split(/\s+/);
    if (parts.length === 2) {
      const b = Number.parseInt(parts[0]!, 10);
      const a = Number.parseInt(parts[1]!, 10);
      if (Number.isFinite(b)) behind = b;
      if (Number.isFinite(a)) ahead = a;
    }
  }

  let lastCommit: GitLastCommit | undefined;
  if (lastCommitRes.ok) {
    const [sha, message, age] = lastCommitRes.stdout.split('\t');
    if (sha && message && age) {
      lastCommit = {
        sha: sha.trim(),
        message: message.trim(),
        age: age.trim(),
      };
    }
  }

  const result: WorktreeGitState = { status: 'ok', branch, dirty };
  if (ahead !== undefined) result.ahead = ahead;
  if (behind !== undefined) result.behind = behind;
  if (lastCommit) result.lastCommit = lastCommit;
  return result;
}
