import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Pure-web build for the HandsConsole — no Electron.
// The renderer lives in src/renderer and talks to the FastAPI sidecar
// (default http://127.0.0.1:8421) over HTTP + SSE. For a single-origin
// production build, serve dist-web/ from FastAPI StaticFiles and set
// VITE_RANCH_API_URL='' so requests are same-origin relative.
export default defineConfig({
  root: 'src/renderer',
  base: './',
  plugins: [react()],
  server: {
    port: 5174,
    strictPort: true,
  },
  build: {
    outDir: '../../dist-web',
    emptyOutDir: true,
  },
});
