# Ranch Console

Local-first Electron console for ranch (Claude Code agent fleet manager).

This is the **Phase A1** scaffold — Electron + Vite + React + strict TypeScript, with an IPC bridge that loads `~/.ranch/config.toml` and surfaces it to the renderer. No real features yet; the worktree grid, terminal embed, dispatch, docker orchestration, and inbox come in subsequent phases.

See [`../ROADMAP.md`](../ROADMAP.md) for the full architecture and phase plan.

## Develop

Requires Node 22+ and pnpm 10+.

```bash
cd console
pnpm install
pnpm dev          # launch with hot reload
pnpm typecheck    # strict TS
pnpm lint         # ESLint
pnpm format       # Prettier write
pnpm build        # production bundle
```

## Layout

```
console/
├── src/
│   ├── main/        # Electron main process — config loader, IPC handlers, BrowserWindow
│   ├── preload/     # contextBridge — exposes typed window.ranch.* surface
│   ├── renderer/    # React app — single window, four placeholder panes
│   └── shared/      # Types shared across main/preload/renderer
├── electron.vite.config.ts
├── tsconfig.json        # renderer + preload + shared (DOM lib)
├── tsconfig.node.json   # main + shared (Node lib)
└── package.json
```

The IPC surface lives in `src/shared/types.ts` (the `RanchApi` interface). When a new capability is added (in subsequent phases), it goes there first, then in the preload bridge, then in `src/main/ipc.ts`.

## What works after A1

- `pnpm dev` opens a window
- Renderer calls `window.ranch.config.get()` and renders the agent count from `~/.ranch/config.toml`
- Strict TypeScript across all three processes (main, preload, renderer)
- ESLint + Prettier wired

## What's next

- **A2** — RunEvent bus + schema (the load-bearing primitive)
- **A3** — project registry (`~/.ranch/projects.toml`)
- **A4** — worktree grid view (read-only, live from the bus)
- **A6** — PTY + xterm.js + tmux integration
- **A7** — interactive dispatch UI

See [GitHub issues](https://github.com/ethandrower/ranch/issues?q=is%3Aopen+label%3Aphase-console) labeled `phase-console`.
