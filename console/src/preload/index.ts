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
  notesGetAll: 'ranch:notes:getAll',
  notesSet: 'ranch:notes:set',
  runsList: 'ranch:runs:list',
  runsGet: 'ranch:runs:get',
  runsCleanupAbandoned: 'ranch:runs:cleanupAbandoned',
  runsApprove: 'ranch:runs:approve',
  runsReject: 'ranch:runs:reject',
  runsNote: 'ranch:runs:note',
  runsStop: 'ranch:runs:stop',
  terminalEnv: 'ranch:terminal:env',
  terminalAttach: 'ranch:terminal:attach',
  terminalWrite: 'ranch:terminal:write',
  terminalResize: 'ranch:terminal:resize',
  terminalDetach: 'ranch:terminal:detach',
  terminalKillSession: 'ranch:terminal:killSession',
  terminalSendKeys: 'ranch:terminal:sendKeys',
  terminalData: 'terminal:data',
  terminalExit: 'terminal:exit',
  appVersion: 'ranch:app:version',
  appRevealInFinder: 'ranch:app:revealInFinder',
  appOpenExternal: 'ranch:app:openExternal',
  dockerEnv: 'ranch:docker:env',
  dockerSnapshot: 'ranch:docker:snapshot',
  dockerUp: 'ranch:docker:up',
  dockerDown: 'ranch:docker:down',
  dockerRestart: 'ranch:docker:restart',
  dockerLogs: 'ranch:docker:logs',
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
  notes: {
    getAll: () => ipcRenderer.invoke(IPC_CHANNELS.notesGetAll),
    set: (agent, label) =>
      ipcRenderer.invoke(IPC_CHANNELS.notesSet, agent, label),
  },
  runs: {
    list: (limit) => ipcRenderer.invoke(IPC_CHANNELS.runsList, limit),
    get: (id) => ipcRenderer.invoke(IPC_CHANNELS.runsGet, id),
    cleanupAbandoned: () =>
      ipcRenderer.invoke(IPC_CHANNELS.runsCleanupAbandoned),
    approve: (id, note) =>
      ipcRenderer.invoke(IPC_CHANNELS.runsApprove, id, note),
    reject: (id, reason) =>
      ipcRenderer.invoke(IPC_CHANNELS.runsReject, id, reason),
    note: (id, text) => ipcRenderer.invoke(IPC_CHANNELS.runsNote, id, text),
    stop: (id) => ipcRenderer.invoke(IPC_CHANNELS.runsStop, id),
  },
  terminal: {
    env: () => ipcRenderer.invoke(IPC_CHANNELS.terminalEnv),
    attach: (agent, opts) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalAttach, agent, opts ?? {}),
    write: (terminalId, data) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalWrite, terminalId, data),
    resize: (terminalId, cols, rows) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalResize, terminalId, cols, rows),
    detach: (terminalId) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalDetach, terminalId),
    killSession: (agent) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalKillSession, agent),
    sendKeys: (agent, keys) =>
      ipcRenderer.invoke(IPC_CHANNELS.terminalSendKeys, agent, keys),
    onData: (handler) =>
      subscribe<TerminalDataEvent>(IPC_CHANNELS.terminalData, handler),
    onExit: (handler) =>
      subscribe<TerminalExitEvent>(IPC_CHANNELS.terminalExit, handler),
  },
  app: {
    version: () => ipcRenderer.invoke(IPC_CHANNELS.appVersion),
    revealInFinder: (path) =>
      ipcRenderer.invoke(IPC_CHANNELS.appRevealInFinder, path),
    openExternal: (url) =>
      ipcRenderer.invoke(IPC_CHANNELS.appOpenExternal, url),
  },
  docker: {
    env: () => ipcRenderer.invoke(IPC_CHANNELS.dockerEnv),
    snapshot: () => ipcRenderer.invoke(IPC_CHANNELS.dockerSnapshot),
    up: (agent) => ipcRenderer.invoke(IPC_CHANNELS.dockerUp, agent),
    down: (agent) => ipcRenderer.invoke(IPC_CHANNELS.dockerDown, agent),
    restart: (agent) => ipcRenderer.invoke(IPC_CHANNELS.dockerRestart, agent),
    logs: (agent, tail) =>
      ipcRenderer.invoke(IPC_CHANNELS.dockerLogs, agent, tail),
  },
};

contextBridge.exposeInMainWorld('ranch', api);
