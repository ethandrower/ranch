/**
 * Preload script — the bridge between renderer (sandboxed Chromium) and
 * main (Node.js). Runs in the renderer's process but with elevated
 * privileges before the page loads.
 *
 * Two IPC patterns surfaced through this file:
 *   1. Request/response — `ipcRenderer.invoke(channel, ...args)` returns
 *      a Promise resolving to whatever the matching ipcMain.handle returned.
 *   2. Push (main → renderer) — `ipcRenderer.on(channel, handler)`.
 *      We expose subscribers as functions returning an unsubscribe so
 *      callers can wire them into React `useEffect` cleanup.
 *
 * `contextBridge.exposeInMainWorld('ranch', api)` injects a typed
 * `window.ranch` into every page. The renderer can ONLY call methods
 * we put on this object — that's the security wall.
 */

import { contextBridge, ipcRenderer } from 'electron';
import type {
  RanchApi,
  TerminalDataEvent,
  TerminalExitEvent,
  Unsubscribe,
} from '../shared/types.js';

const IPC_CHANNELS = {
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
  terminalData: 'terminal:data',
  terminalExit: 'terminal:exit',
  appVersion: 'ranch:app:version',
} as const;

/**
 * Subscribe to a push channel and return an unsubscribe function.
 * IpcRenderer's `on` listener takes (event, ...args); we discard the
 * event and forward the first arg payload.
 */
function subscribe<T>(
  channel: string,
  handler: (payload: T) => void,
): Unsubscribe {
  const listener = (_event: unknown, payload: T): void => {
    handler(payload);
  };
  ipcRenderer.on(channel, listener);
  return () => {
    ipcRenderer.removeListener(channel, listener);
  };
}

const api: RanchApi = {
  config: {
    get: () => ipcRenderer.invoke(IPC_CHANNELS.configGet),
  },
  worktrees: {
    list: () => ipcRenderer.invoke(IPC_CHANNELS.worktreesList),
    session: (agent) =>
      ipcRenderer.invoke(IPC_CHANNELS.worktreesSession, agent),
    git: (agent) => ipcRenderer.invoke(IPC_CHANNELS.worktreesGit, agent),
    processSnapshot: () =>
      ipcRenderer.invoke(IPC_CHANNELS.worktreesProcessSnapshot),
  },
  terminal: {
    env: () => ipcRenderer.invoke(IPC_CHANNELS.terminalEnv),
    attach: (agent, cols, rows) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalAttach, agent, cols, rows),
    write: (terminalId, data) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalWrite, terminalId, data),
    resize: (terminalId, cols, rows) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalResize, terminalId, cols, rows),
    detach: (terminalId) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalDetach, terminalId),
    onData: (handler) =>
      subscribe<TerminalDataEvent>(IPC_CHANNELS.terminalData, handler),
    onExit: (handler) =>
      subscribe<TerminalExitEvent>(IPC_CHANNELS.terminalExit, handler),
  },
  app: {
    version: () => ipcRenderer.invoke(IPC_CHANNELS.appVersion),
  },
};

contextBridge.exposeInMainWorld('ranch', api);
