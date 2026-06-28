# Ranch — Connector & Employee Model

> Status: design note (2026-06) for the generalized platform (see `vision.md`). Captures decisions reached in design discussion; not all built.

## Connectors: two facets, one transport

Every external system is a **connector** that can expose either or both of two facets. The transport layer below them is shared (auth/token store, connection, rate-limit, retry/backoff — see `channel-connectors-spec.md`); the interaction shape on top is not.

| | **Tool facet** | **Channel facet** |
|---|---|---|
| Shape | Request → response (RPC) | Bidirectional stream of messages |
| Driver | Agent **pulls** | World **pushes** |
| Timing | Synchronous, agent-initiated | Async, event-driven, proactive |
| Result | Structured data the agent reasons over | A reply in a thread |
| Maps onto | MCP tools / SDK tool-use | The connector spec / the Event channel |
| Examples | JQL search, create PR, run tests, deploy | Slack DM, email thread, PR-comment thread |

**Do not collapse these into one interface.** Forcing transactional calls (JQL) through a conversational `onMessage`/`send` shape is the over-unification trap.

### Jira is the canonical dual-facet connector

- **Tool facet:** JQL search, issue CRUD, transitions, assignment.
- **Channel facet:** comment threads, @mentions, "assigned to you" notifications.

Same auth, same client, two facets. Bitbucket is identical (create-PR = tool; review-comment thread = channel). Slack ≈ pure channel facet. A test runner ≈ pure tool facet.

**Rule — don't double-implement.** Transactional Jira/Bitbucket already exist in Python (`ranch/` — triage JQL, `bb` PR ops). Keep those as the tool facet. Build **channel** facets (Slack first) as new TS connectors. A dual-facet system's channel facet can wrap the existing Python tool paths rather than reimplement a client.

## Employee types: a manifest

An employee type = **role** (system prompt) + granted **tools** + wired **channels** + **goals**. Like onboarding a human: give them software licenses (tools) and accounts (channels) for their role.

Example — engineer hand:
- Tools: `git`, `run_tests`, `jql_search`, `create_pr`, `deploy`, `run_acceptance`
- Channels: Slack, PR-comment threads, Jira comments
- Role: engineer system prompt + develop-branch rules

Future types (support, research, PM) reuse the substrate with a different manifest.

## Memory boundary: declarative vs learned (extends issue #92)

The OpenClaw failure mode is putting *everything* in flat text files; that breaks query, accumulation/supersession, relations, provenance, and concurrency for anything that learns. The fix is not "DB for everything" — it's filing by category:

| | **Flat files (git)** | **DB (`ranch.db`)** |
|---|---|---|
| What | Declarative config: employee/role definitions, prompts, tool/channel manifests, skill *catalog* | Learned/accumulated: lessons, feedback, dossiers, decisions, run history, **which** skills/tools proved effective |
| Why there | Human-authored, reviewed, diffable, rollback-able | Queried, related, provenance-tracked, deduped/superseded |

**Doctrine:** learned state lives in SQLite; declarative config lives in git; nothing important lives in an undifferentiated text blob.

Connectors are **stateless transport, upstream of the DB** — so we adopt OpenClaw's connector *code* without its flat-file *memory model*. Inbound data flows: connector → structured `ranch.db` (Event channel) → reflection/lessons.

## Mapping onto today's ranch

- **Tool facet** ≈ existing in-process MCP tools (`record_state`, `run_acceptance`) + Python Jira/`bb`.
- **Channel facet** ≈ the roadmap's Event channel + the **unblock-monitoring gap** (`ARCHITECTURE.md §7`) generalized — the "PR comment landed / CI flipped / deploy done" unblock signals *are* inbound channel events.
- **Cockpit** ≈ `console/` (Electron) — evolving toward the people/capacity view.

## Packaging & licensing

Repo is **AGPL-3.0** (right for the citemed orchestration brain; poison for a reusable library). The generic connectors must stay permissively licensed for reach.

```
ranch/                       # repo root — AGPL-3.0
├── ranch/                   # Python: orchestration brain (citemed-aware)
├── console/                 # TS/Electron: operator UI (citemed-aware)
├── packages/
│   └── channels/            # TS: GENERIC connectors — own MIT/Apache LICENSE, zero citemed imports, publishable
├── services/
│   └── channel-gateway/     # TS: citemed glue — loads channels, bridges inbound→ranch DB, outbound←orchestrator
└── (add) pnpm-workspace.yaml + root package.json
```

- `packages/channels` carries its **own permissive LICENSE** (per-directory licensing; distinct from root AGPL). Keeps citemed's brain copyleft, the connectors adoptable, extraction-to-OSS cheap later.
- Dependency direction is one-way: `channels` imports nothing citemed/`ranch`. Glue → channels, never the reverse.
- Decision still open: **MIT vs Apache-2.0** for the channels package.
- Derived from OpenClaw (MIT) — track attribution.
