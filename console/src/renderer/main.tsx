import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App.js';
import { HandsConsoleApp } from './HandsConsole/HandsConsoleApp.js';
import './styles.css';

const rootEl = document.getElementById('root');
if (!rootEl) {
  throw new Error('Root element #root not found');
}

// View gate — the new HandsConsole UI is opt-in for now so the pty-based
// console keeps working untouched. Activates via:
//   - ?view=hands query param (set in dev/launch URL)
//   - VITE_RANCH_VIEW=hands env var at build time
// Default route renders the existing terminal-driven App.
const viewParam = new URLSearchParams(window.location.search).get('view');
const useHandsView =
  viewParam === 'hands' ||
  import.meta.env.VITE_RANCH_VIEW === 'hands';

createRoot(rootEl).render(
  <StrictMode>
    {useHandsView ? <HandsConsoleApp /> : <App />}
  </StrictMode>,
);
