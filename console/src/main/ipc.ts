/**
 * IPC handler registration.
 *
 * Two channel patterns in this file:
 *   1. Request/response — `ipcMain.handle(channel, fn)`. Renderer-side
 *      `ipcRenderer.invoke(channel, ...args)` returns a Promise.
 *   2. Push (main → renderer) — `webContents.send(channel, payload)`.
 *      Used for streaming pty output, where there's no single "response."
 *      Subscribers register on the renderer side via
 *      `ipcRenderer.on(channel, handler)`.
 *
 * Channel strings appear here AND in src/preload/index.ts. They must
 * stay in sync — easy to grep, easy to keep small.
 */

import { app, ipcMain } from 'electron';
import { loadRanchConfig } from './config.js';
import { listWorktrees } from './worktrees.js';
import { getActiveSession } from './transcript.js';
import { getWorktreeGitState } from './git.js';
import { snapshotProcessState } from './process.js';
import {
  attachTerminal,
  detachTerminal,
  getTerminalEnv,
  resizeTerminal,
  writeTerminal,
} from './pty.js';

export const IPC_CHANNELS = {
  configGet: 'ranch:config:get',
  worktreesList: 'ranch:worktrees:list',
  worktreesSession: 'ranch:worktrees:session',
  worktreesGit: 'ranch:worktrees:git',
  worktreesProcessSnapshot: 'ranch:worktrees:processSnapshot',
  terminalEnv: 'ranch:terminal:env',
  terminalAttach: 'ranch:terminal:attach',
  terminalWrite: 'ranch:terminal:write',
  terminalResize: 'ranch:terminal:resize',
  terminalDetach: 'ranch:terminal:detach',
  // Push channels (main → renderer). No registration needed in main —
  // we just call webContents.send(...). Listed here for grep-ability.
  terminalData: 'terminal:data',
  terminalExit: 'terminal:exit',
  appVersion: 'ranch:app:version',
} as const;

export function registerIpcHandlers(): void {
  ipcMain.handle(IPC_CHANNELS.configGet, async () => loadRanchConfig());
  ipcMain.handle(IPC_CHANNELS.worktreesList, async () => listWorktrees());

  ipcMain.handle(
    IPC_CHANNELS.worktreesSession,
    async (_event, agent: unknown) => {
      if (typeof agent !== 'string' || !agent) {
        throw new Error('worktrees.session requires an agent name');
      }
      const config = await loadRanchConfig();
      const match = config.agents.find((a) => a.name === agent);
      if (!match) {
        throw new Error(`Unknown agent: ${agent}`);
      }
      return getActiveSession(match.worktree);
    },
  );

  ipcMain.handle(IPC_CHANNELS.worktreesGit, async (_event, agent: unknown) => {
    if (typeof agent !== 'string' || !agent) {
      throw new Error('worktrees.git requires an agent name');
    }
    const config = await loadRanchConfig();
    const match = config.agents.find((a) => a.name === agent);
    if (!match) throw new Error(`Unknown agent: ${agent}`);
    return getWorktreeGitState(match.worktree);
  });

  ipcMain.handle(IPC_CHANNELS.worktreesProcessSnapshot, async () => {
    const config = await loadRanchConfig();
    const agents: Record<string, string> = {};
    for (const a of config.agents) agents[a.name] = a.worktree;
    return snapshotProcessState({ agents });
  });

  ipcMain.handle(IPC_CHANNELS.terminalEnv, async () => getTerminalEnv());

  ipcMain.handle(
    IPC_CHANNELS.terminalAttach,
    async (event, agent: unknown, cols: unknown, rows: unknown) => {
      if (typeof agent !== 'string' || !agent) {
        throw new Error('terminal.attach requires an agent name');
      }
      const config = await loadRanchConfig();
      const match = config.agents.find((a) => a.name === agent);
      if (!match) {
        throw new Error(`Unknown agent: ${agent}`);
      }
      return attachTerminal({
        agent,
        worktreePath: match.worktree,
        ...(typeof cols === 'number' ? { cols } : {}),
        ...(typeof rows === 'number' ? { rows } : {}),
        webContents: event.sender,
      });
    },
  );

  ipcMain.handle(
    IPC_CHANNELS.terminalWrite,
    (_event, terminalId: unknown, data: unknown) => {
      if (typeof terminalId === 'string' && typeof data === 'string') {
        writeTerminal(terminalId, data);
      }
    },
  );

  ipcMain.handle(
    IPC_CHANNELS.terminalResize,
    (_event, terminalId: unknown, cols: unknown, rows: unknown) => {
      if (
        typeof terminalId === 'string' &&
        typeof cols === 'number' &&
        typeof rows === 'number'
      ) {
        resizeTerminal(terminalId, cols, rows);
      }
    },
  );

  ipcMain.handle(IPC_CHANNELS.terminalDetach, (_event, terminalId: unknown) => {
    if (typeof terminalId === 'string') detachTerminal(terminalId);
  });

  ipcMain.handle(IPC_CHANNELS.appVersion, () => app.getVersion());
}
