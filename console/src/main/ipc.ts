/**
 * IPC handler registration.
 *
 * One channel per typed RanchApi method. The channel string lives both
 * here (registration) and in src/preload/index.ts (caller). They MUST
 * stay in sync — easy to grep, easy to keep small.
 *
 * `ipcMain.handle(channel, fn)` registers a request/response handler.
 * Renderer-side `ipcRenderer.invoke(channel, ...args)` returns a
 * Promise resolving to whatever this handler returns.
 */

import { app, ipcMain } from 'electron';
import { loadRanchConfig } from './config.js';
import { listWorktrees } from './worktrees.js';
import { getActiveSession } from './transcript.js';

export const IPC_CHANNELS = {
  configGet: 'ranch:config:get',
  worktreesList: 'ranch:worktrees:list',
  worktreesSession: 'ranch:worktrees:session',
  appVersion: 'ranch:app:version',
} as const;

export function registerIpcHandlers(): void {
  ipcMain.handle(IPC_CHANNELS.configGet, async () => {
    return loadRanchConfig();
  });

  ipcMain.handle(IPC_CHANNELS.worktreesList, async () => {
    return listWorktrees();
  });

  ipcMain.handle(
    IPC_CHANNELS.worktreesSession,
    async (_event, agent: unknown) => {
      if (typeof agent !== 'string' || !agent) {
        throw new Error('worktrees.session requires an agent name');
      }
      // Re-read config on each call: it's tiny and rarely changes.
      const config = await loadRanchConfig();
      const match = config.agents.find((a) => a.name === agent);
      if (!match) {
        throw new Error(`Unknown agent: ${agent}`);
      }
      return getActiveSession(match.worktree);
    },
  );

  ipcMain.handle(IPC_CHANNELS.appVersion, () => {
    return app.getVersion();
  });
}
