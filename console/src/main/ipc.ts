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

import { app, ipcMain, shell } from 'electron';
import { loadRanchConfig } from './config.js';
import { listWorktrees } from './worktrees.js';
import { getActiveSession } from './transcript.js';
import { getWorktreeGitState } from './git.js';
import { snapshotProcessState } from './process.js';
import {
  getDockerEnv,
  snapshotDockerState,
  dockerStackUp,
  dockerStackDown,
  dockerStackRestart,
  dockerStackReset,
  dockerStackLogs,
  resolveAgentDocker,
} from './docker.js';
import { getAllNotes, setNote } from './notes.js';
import {
  listRuns,
  getRun,
  cleanupAbandonedRuns,
  approveRun,
  rejectRun,
  noteRun,
  stopRun,
  dispatchRun,
} from './runs.js';
import type { AgentConfig, DispatchOptions } from '../shared/types.js';
import {
  attachTerminal,
  detachTerminal,
  getTerminalEnv,
  killTmuxSession,
  resizeTerminal,
  sendKeysToSession,
  writeTerminal,
} from './pty.js';

export const IPC_CHANNELS = {
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
  runsDispatch: 'ranch:runs:dispatch',
  terminalEnv: 'ranch:terminal:env',
  terminalAttach: 'ranch:terminal:attach',
  terminalWrite: 'ranch:terminal:write',
  terminalResize: 'ranch:terminal:resize',
  terminalDetach: 'ranch:terminal:detach',
  terminalKillSession: 'ranch:terminal:killSession',
  terminalSendKeys: 'ranch:terminal:sendKeys',
  // Push channels (main → renderer). No registration needed in main —
  // we just call webContents.send(...). Listed here for grep-ability.
  terminalData: 'terminal:data',
  terminalExit: 'terminal:exit',
  appVersion: 'ranch:app:version',
  appRevealInFinder: 'ranch:app:revealInFinder',
  appOpenExternal: 'ranch:app:openExternal',
  dockerEnv: 'ranch:docker:env',
  dockerSnapshot: 'ranch:docker:snapshot',
  dockerResolve: 'ranch:docker:resolve',
  dockerUp: 'ranch:docker:up',
  dockerDown: 'ranch:docker:down',
  dockerRestart: 'ranch:docker:restart',
  dockerReset: 'ranch:docker:reset',
  dockerLogs: 'ranch:docker:logs',
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

  ipcMain.handle(IPC_CHANNELS.notesGetAll, async () => getAllNotes());

  ipcMain.handle(
    IPC_CHANNELS.notesSet,
    async (_event, agent: unknown, label: unknown) => {
      if (typeof agent !== 'string' || !agent) {
        throw new Error('notes.set requires an agent name');
      }
      if (typeof label !== 'string') {
        throw new Error('notes.set requires a string label');
      }
      return setNote(agent, label);
    },
  );

  ipcMain.handle(IPC_CHANNELS.runsList, async (_event, limit: unknown) => {
    const n = typeof limit === 'number' ? limit : 50;
    return listRuns(n);
  });

  ipcMain.handle(IPC_CHANNELS.runsGet, async (_event, id: unknown) => {
    if (typeof id !== 'number') {
      throw new Error('runs.get requires a numeric id');
    }
    return getRun(id);
  });

  ipcMain.handle(IPC_CHANNELS.runsCleanupAbandoned, async () => {
    return cleanupAbandonedRuns();
  });

  ipcMain.handle(
    IPC_CHANNELS.runsApprove,
    async (_event, id: unknown, note: unknown) => {
      if (typeof id !== 'number') throw new Error('runs.approve needs id');
      await approveRun(id, typeof note === 'string' ? note : undefined);
    },
  );

  ipcMain.handle(
    IPC_CHANNELS.runsReject,
    async (_event, id: unknown, reason: unknown) => {
      if (typeof id !== 'number') throw new Error('runs.reject needs id');
      await rejectRun(id, typeof reason === 'string' ? reason : undefined);
    },
  );

  ipcMain.handle(
    IPC_CHANNELS.runsNote,
    async (_event, id: unknown, text: unknown) => {
      if (typeof id !== 'number') throw new Error('runs.note needs id');
      if (typeof text !== 'string') throw new Error('runs.note needs text');
      await noteRun(id, text);
    },
  );

  ipcMain.handle(IPC_CHANNELS.runsStop, async (_event, id: unknown) => {
    if (typeof id !== 'number') throw new Error('runs.stop needs id');
    await stopRun(id);
  });

  ipcMain.handle(IPC_CHANNELS.runsDispatch, async (_event, opts: unknown) => {
    if (typeof opts !== 'object' || opts === null) {
      throw new Error('runs.dispatch needs an options object');
    }
    const o = opts as Partial<DispatchOptions>;
    if (typeof o.agent !== 'string' || !o.agent.trim()) {
      throw new Error('runs.dispatch needs agent');
    }
    if (typeof o.brief !== 'string' || !o.brief.trim()) {
      throw new Error('runs.dispatch needs brief');
    }
    const ticket =
      typeof o.ticket === 'string' && o.ticket.trim() ? o.ticket : undefined;
    return dispatchRun({
      agent: o.agent,
      ...(ticket ? { ticket } : {}),
      brief: o.brief,
      free: o.free === true,
      autoApprove: o.autoApprove === true,
    });
  });

  ipcMain.handle(IPC_CHANNELS.terminalEnv, async () => getTerminalEnv());

  ipcMain.handle(
    IPC_CHANNELS.terminalAttach,
    async (event, agent: unknown, opts: unknown) => {
      if (typeof agent !== 'string' || !agent) {
        throw new Error('terminal.attach requires an agent name');
      }
      const config = await loadRanchConfig();
      const match = config.agents.find((a) => a.name === agent);
      if (!match) {
        throw new Error(`Unknown agent: ${agent}`);
      }
      const o = (opts as Record<string, unknown> | undefined) ?? {};
      return attachTerminal({
        agent,
        worktreePath: match.worktree,
        ...(typeof o['cols'] === 'number' ? { cols: o['cols'] as number } : {}),
        ...(typeof o['rows'] === 'number' ? { rows: o['rows'] as number } : {}),
        ...(typeof o['command'] === 'string' && o['command']
          ? { command: o['command'] as string }
          : {}),
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

  ipcMain.handle(
    IPC_CHANNELS.terminalKillSession,
    async (_event, agent: unknown) => {
      if (typeof agent !== 'string' || !agent) {
        throw new Error('terminal.killSession requires an agent name');
      }
      await killTmuxSession(agent);
    },
  );

  ipcMain.handle(
    IPC_CHANNELS.terminalSendKeys,
    async (_event, agent: unknown, keys: unknown) => {
      if (typeof agent !== 'string' || !agent) {
        throw new Error('terminal.sendKeys requires an agent name');
      }
      if (!Array.isArray(keys) || !keys.every((k) => typeof k === 'string')) {
        throw new Error('terminal.sendKeys requires a string array');
      }
      await sendKeysToSession(agent, keys as string[]);
    },
  );

  ipcMain.handle(IPC_CHANNELS.appVersion, () => app.getVersion());

  ipcMain.handle(IPC_CHANNELS.appRevealInFinder, (_event, path: unknown) => {
    if (typeof path !== 'string' || !path) return;
    // showItemInFolder reveals the path in Finder (selecting the item itself
    // if it's a file, or showing the directory). Safer than openPath which
    // would try to "open" the directory in whatever the default handler is.
    shell.showItemInFolder(path);
  });

  ipcMain.handle(IPC_CHANNELS.appOpenExternal, async (_event, url: unknown) => {
    if (typeof url !== 'string' || !url) return;
    // Allowlist: http(s) and the file:// scheme. Anything else is rejected
    // — we don't want a stray IPC message launching a `file://` script or a
    // custom-protocol handler we didn't authorize.
    if (!/^(https?:|file:)\/\//.test(url)) return;
    await shell.openExternal(url);
  });

  ipcMain.handle(IPC_CHANNELS.dockerEnv, async () => getDockerEnv());

  ipcMain.handle(IPC_CHANNELS.dockerSnapshot, async () => {
    const config = await loadRanchConfig();
    const agents: Record<string, string> = {};
    for (const a of config.agents) agents[a.name] = a.worktree;
    return snapshotDockerState({ agents });
  });

  async function resolveAgentWorktree(agent: unknown): Promise<{
    agent: string;
    worktreePath: string;
    dockerConfig: AgentConfig['docker'];
  }> {
    if (typeof agent !== 'string' || !agent) {
      throw new Error('docker call requires an agent name');
    }
    const config = await loadRanchConfig();
    const match = config.agents.find((a) => a.name === agent);
    if (!match) throw new Error(`Unknown agent: ${agent}`);
    return {
      agent,
      worktreePath: match.worktree,
      dockerConfig: match.docker,
    };
  }

  ipcMain.handle(IPC_CHANNELS.dockerResolve, async (_event, agent: unknown) => {
    const {
      agent: a,
      worktreePath,
      dockerConfig,
    } = await resolveAgentWorktree(agent);
    return resolveAgentDocker(a, worktreePath, dockerConfig);
  });

  ipcMain.handle(IPC_CHANNELS.dockerUp, async (_event, agent: unknown) => {
    const {
      agent: a,
      worktreePath,
      dockerConfig,
    } = await resolveAgentWorktree(agent);
    return dockerStackUp(a, worktreePath, dockerConfig);
  });

  ipcMain.handle(IPC_CHANNELS.dockerDown, async (_event, agent: unknown) => {
    const {
      agent: a,
      worktreePath,
      dockerConfig,
    } = await resolveAgentWorktree(agent);
    return dockerStackDown(a, worktreePath, dockerConfig);
  });

  ipcMain.handle(IPC_CHANNELS.dockerRestart, async (_event, agent: unknown) => {
    const {
      agent: a,
      worktreePath,
      dockerConfig,
    } = await resolveAgentWorktree(agent);
    return dockerStackRestart(a, worktreePath, dockerConfig);
  });

  ipcMain.handle(IPC_CHANNELS.dockerReset, async (_event, agent: unknown) => {
    const {
      agent: a,
      worktreePath,
      dockerConfig,
    } = await resolveAgentWorktree(agent);
    return dockerStackReset(a, worktreePath, dockerConfig);
  });

  ipcMain.handle(
    IPC_CHANNELS.dockerLogs,
    async (_event, agent: unknown, tail: unknown) => {
      const {
        agent: a,
        worktreePath,
        dockerConfig,
      } = await resolveAgentWorktree(agent);
      const t = typeof tail === 'number' ? tail : 200;
      return dockerStackLogs(a, worktreePath, t, dockerConfig);
    },
  );
}
