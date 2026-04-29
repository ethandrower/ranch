/**
 * Preload script — the bridge between renderer (sandboxed Chromium) and
 * main (Node.js). Runs in the renderer's process but with elevated
 * privileges before the page loads.
 *
 * `contextBridge.exposeInMainWorld('ranch', api)` injects a typed
 * `window.ranch` into every page. The renderer can ONLY call methods
 * we put on this object — everything else stays on the other side
 * of the wall. That's the security guarantee.
 */

import { contextBridge, ipcRenderer } from 'electron';
import type { RanchApi } from '../shared/types.js';

const IPC_CHANNELS = {
  configGet: 'ranch:config:get',
  worktreesList: 'ranch:worktrees:list',
  worktreesSession: 'ranch:worktrees:session',
  appVersion: 'ranch:app:version',
} as const;

const api: RanchApi = {
  config: {
    get: () => ipcRenderer.invoke(IPC_CHANNELS.configGet),
  },
  worktrees: {
    list: () => ipcRenderer.invoke(IPC_CHANNELS.worktreesList),
    session: (agent) =>
      ipcRenderer.invoke(IPC_CHANNELS.worktreesSession, agent),
  },
  app: {
    version: () => ipcRenderer.invoke(IPC_CHANNELS.appVersion),
  },
};

contextBridge.exposeInMainWorld('ranch', api);
