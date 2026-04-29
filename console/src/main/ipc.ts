import { app, ipcMain } from 'electron';
import { loadRanchConfig } from './config.js';

export const IPC_CHANNELS = {
  configGet: 'ranch:config:get',
  appVersion: 'ranch:app:version',
} as const;

export function registerIpcHandlers(): void {
  ipcMain.handle(IPC_CHANNELS.configGet, async () => {
    return loadRanchConfig();
  });

  ipcMain.handle(IPC_CHANNELS.appVersion, () => {
    return app.getVersion();
  });
}
