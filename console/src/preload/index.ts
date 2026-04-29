import { contextBridge, ipcRenderer } from 'electron';
import type { RanchApi } from '../shared/types.js';

const IPC_CHANNELS = {
  configGet: 'ranch:config:get',
  appVersion: 'ranch:app:version',
} as const;

const api: RanchApi = {
  config: {
    get: () => ipcRenderer.invoke(IPC_CHANNELS.configGet),
  },
  app: {
    version: () => ipcRenderer.invoke(IPC_CHANNELS.appVersion),
  },
};

contextBridge.exposeInMainWorld('ranch', api);
