import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { HandsConsoleApp } from './HandsConsole/HandsConsoleApp.js';
import './styles.css';

// Pure-web entry. The HandsConsole talks to the FastAPI sidecar over
// HTTP + SSE (see HandsConsole/api.ts) — no Electron, no preload bridge.
// The legacy pty-based terminal console (App.tsx) is retired from the web
// build; interactive drop-in will return via a server-side PTY-over-WS
// bridge.
const rootEl = document.getElementById('root');
if (!rootEl) {
  throw new Error('Root element #root not found');
}

createRoot(rootEl).render(
  <StrictMode>
    <HandsConsoleApp />
  </StrictMode>,
);
