# ranch — DEV worktree (`feat/hands-console-ui`)

This worktree is the **rebuild-the-console** workstream. It's deliberately
isolated from the live ranch instance running out of
`/Users/ethand320/code/citemed/ranch/`.

**Stay in this directory for all work on the new console UI.** Don't reach
into the main checkout — its Electron process is holding real hand sessions.

---

## How to run the dev console

```sh
./scripts/dev.sh
```

That script handles every isolation knob (see below). On first run it will
`pnpm install` into this worktree's `console/node_modules/`.

## Isolation matrix

| Source of conflict | Live (`~/code/citemed/ranch/`) | DEV (this worktree) |
| --- | --- | --- |
| Electron `userData` | `~/Library/Application Support/ranch-console/` | `~/Library/Application Support/ranch-console-dev/` (via `package.json` name = `ranch-console-dev`) |
| Vite renderer port | `5173` | `5174` (`strictPort: true`) |
| FastAPI sidecar port (future) | (none yet) | `8421` via `RANCH_API_PORT` |
| Ranch SQLite | `~/.ranch/ranch.db` | `.ranch-dev/ranch.db` (this worktree) via `RANCH_DATABASE_URL` |
| Ranch home (config, scopes, hands) | `~/.ranch/` | `.ranch-dev/` via `RANCH_HOME` |
| Singleton lock | none (verified — `requestSingleInstanceLock` not called) | none |
| Git working tree | `main` branch | `feat/hands-console-ui` branch |

## Process hygiene — the kill-switch rules

**Never run these commands while the live ranch is up:**

- `pkill electron` — kills both dev and live Electron
- `pkill -f ranch` — would tear down live tmux sessions and the live console
- `killall node` — kills both Vite dev servers
- `rm -rf ~/Library/Application\ Support/ranch-console*` — clobbers live state

**To stop the dev instance only:**

```sh
# The dev electron-vite process — narrowest match
pgrep -af "ranch-newconsole/console.*electron-vite" | awk '{print $1}' | xargs kill

# Or by port
lsof -nP -iTCP:5174 -sTCP:LISTEN -t | xargs kill
```

**To confirm the live console is healthy** (before/after dev work):

```sh
pgrep -afl ranch-console | grep -v ranch-console-dev | grep -v ranch-newconsole
lsof -nP -iTCP:5173 -sTCP:LISTEN     # live Vite still up?
```

## Worktree maintenance

- This worktree was created with:
  `git worktree add -b feat/hands-console-ui ~/code/citemed/ranch-newconsole 46f512b`
- To remove cleanly when done: from the **main** checkout, run
  `git worktree remove ~/code/citemed/ranch-newconsole`
- Don't `rm -rf` the directory directly — that leaves a stale worktree
  entry in `.git/worktrees/`.

## What lives in `.ranch-dev/`

The dev ranch home. SQLite DB, scopes, notes, config — all the things the
real `~/.ranch/` carries. **Gitignored** — never committed.

## Phase plan

See the plan posted in conversation (just before this worktree was created)
for the P0→P8 phase breakdown. Roughly: P0 view-model + initiative schema,
P1 `blocked_by`, P2 FastAPI sidecar, P3 React port (fixture-driven — the
week-1 demo), P4 live wiring, P5 events log, P6 per-step details, P7/P8
coexistence + polish.
