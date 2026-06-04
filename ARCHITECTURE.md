# Ranch — Architecture

A graphical reference for how the autonomous loop is wired, what every
component owns, where state lives, and (importantly) **what's built
today vs. what's still on the board**.

Living document — diagrams update as the system does. If you see a
diagram that doesn't match the code, it's a bug. Open a PR.

---

## At a glance

Ranch is a **memory + orchestration layer above Claude Code**. It doesn't
replace the interactive CC session — it sits around it. Every
persistent fact (lessons, dossier history, decisions, system prompts)
lives in our SQLite DB at `~/.ranch/ranch.db` or in this repo. We
deliberately don't depend on any Anthropic-hosted memory primitive
(see [#92](https://github.com/ethandrower/ranch/issues/92)).

The user-visible product is two surfaces:
- The **Electron console** at `console/` for the operator
- The **`ranch` CLI** for everything the console doesn't surface yet

Behind both: a SQLite DB, a daemon (the **ranch hand**), and a set of
**MCP tools** that ride along inside each Claude SDK session.

---

## 1. Component graph — what runs where

```mermaid
flowchart LR
  subgraph Operator["Operator's machine"]
    direction TB
    Console["Electron console<br/>(console/src/renderer)"]
    CLI["ranch CLI<br/>(ranch/cli.py)"]
    Hand["ranch hand<br/>daemon<br/>(ranch/hand.py)"]
    Orch["Orchestrator<br/>(ranch/runner/orchestrator.py)"]
    SDK["ClaudeSDKClient<br/>(claude-code-sdk)"]
    MCP["ranch MCP server<br/>(record_state, record_checkpoint,<br/>log_decision, run_acceptance)"]
    DB[(SQLite<br/>~/.ranch/ranch.db)]
    Worktree["Agent worktree<br/>(per-agent git checkout)"]
    Docker["Docker compose stack<br/>(per-agent)"]
  end

  subgraph External["External systems"]
    Jira["Atlassian Jira<br/>(REST API)"]
    BB["Bitbucket<br/>(bb CLI)"]
    GH["GitHub<br/>(gh CLI)"]
    Dokku["Dokku staging<br/>(SSH)"]
    Anthropic["Anthropic API<br/>(Claude models)"]
  end

  Console -- "IPC<br/>(console/src/main/*.ts)" --> DB
  Console -- "spawn" --> CLI
  Console -- "docker compose<br/>logs -f" --> Docker
  CLI -- "writes/reads" --> DB
  Hand -- "polls/writes" --> DB
  Hand -- "triage" --> Jira
  Hand -- "spawns" --> Orch
  Orch -- "spawns + hooks" --> SDK
  SDK -- "in-process<br/>tools" --> MCP
  SDK -- "HTTPS" --> Anthropic
  MCP -- "PostToolUse hooks<br/>write to" --> DB
  Orch -- "PR/comment ops" --> BB
  Orch -- "PR/comment ops" --> GH
  Orch -- "git ops" --> Worktree

  style DB fill:#1e3a2e,stroke:#5cb85c,color:#e6e9ef
  style MCP fill:#3a1e3a,stroke:#b85cb8,color:#e6e9ef
  style Hand fill:#3a2e1e,stroke:#b8945c,color:#e6e9ef
  style Anthropic fill:#1e1e3a,stroke:#5c80b8,color:#e6e9ef
  style Jira fill:#1e1e3a,stroke:#5c80b8,color:#e6e9ef
  style BB fill:#1e1e3a,stroke:#5c80b8,color:#e6e9ef
  style GH fill:#1e1e3a,stroke:#5c80b8,color:#e6e9ef
  style Dokku fill:#1e1e3a,stroke:#5c80b8,color:#e6e9ef
```

Key relationships:

- **Two writers, one DB.** Both the CLI/hand (Python) and the console
  (Electron main process, via the `sqlite3` binary) read from
  `~/.ranch/ranch.db`. Schema is owned by Python via SQLAlchemy
  (`ranch/models.py`).
- **MCP tools are in-process.** `record_state`, `record_checkpoint`,
  `log_decision`, and `run_acceptance` live in `ranch/runner/tools.py`
  and ride inside every SDK session as `mcp_servers={"ranch": ranch_mcp}`.
- **PostToolUse hooks are how state escapes the session into the DB.**
  Each MCP tool has a matching hook in `ranch/runner/` that validates +
  persists the payload.
- **Anthropic-hosted state is deliberately minimal.** The only thing
  Anthropic holds for us across SDK calls is the session's running
  conversation tape (resumed via `resume=sdk_session_id`). No memory
  stores, no Files API, no hosted agent registry.

---

## 2. Pilot loop — sequence

The full ticket → PR flow with every gate, every hand-off, every
auto-vs-human-approval.

```mermaid
sequenceDiagram
  autonumber
  participant Op as Operator
  participant Hand as Ranch hand<br/>(daemon)
  participant Jira
  participant BB as Bitbucket
  participant Orch as Orchestrator<br/>(propose/execute)
  participant Agent as Claude SDK<br/>session
  participant MCP as ranch MCP<br/>(record_state /<br/>run_acceptance)
  participant DB as SQLite DB

  Op->>Hand: ranch hand start max --project ECD
  loop every poll_seconds
    Hand->>DB: any active run?
    Hand->>DB: any approved parked propose?
    alt approved → fire execute (H11 v2)
      Hand->>DB: pre-seed Dossier (carry plan + acceptance)
      Hand->>Orch: spawn (free=False, auto_approve_kinds={plan_ready, tests_green})
      Orch->>Agent: execute brief (with plan)
      Agent->>MCP: record_state (planning)
      Agent->>MCP: record_checkpoint(plan_ready)
      Note over Orch,DB: auto-approved (vetted at propose)
      Agent->>Agent: code, then verify
      Agent->>MCP: run_acceptance
      MCP->>DB: write JudgeRun, return pass/fail
      alt failed
        Agent->>Agent: fix + re-run
      else passed
        Agent->>MCP: record_state(parked, pre_push ready)
        Agent->>MCP: record_checkpoint(pre_push)
        Note over Orch: HUMAN GATE — orchestrator parks
      end
      Op-->>DB: ranch approve <run_id>
      Note over Orch: gate releases, agent continues
      Agent->>BB: git push + bb pr create
    else no approved propose
      Hand->>DB: any recent parked? (24h window)
      alt yes → idle (await review)
        Hand->>Hand: sleep
      else no → triage for new work
        Hand->>Jira: searchJiraIssuesUsingJql<br/>(assignee=currentUser AND statusCategory != Done)
        Jira-->>Hand: ranked tickets
        Hand->>Jira: get_ticket + list_sisters (top pick)
        Hand->>BB: bb pr list (PR discovery)
        Hand->>DB: save scope bundle
        Hand->>Orch: spawn propose (read-only tools, 180s budget)
        Orch->>Agent: scope-augmented brief
        Agent->>MCP: record_state (researching → parked)
        Agent->>DB: parked with plan + acceptance + options
        Note over Op: HUMAN GATE — operator reviews dossier
      end
    end
  end
  Op-->>DB: ranch pr draft / open
  Note over Op,BB: H10 — PR drafted from accumulated dossier
```

Notes:
- The **PROPOSE → EXECUTE handoff is a structured contract** (the
  parked dossier's `acceptance` field), not freeform markdown. This is
  the whole point of H8.
- **Two operator gates per ticket** — at `plan_ready` (the plan) and
  `pre_push` (the diff). By design. Tune via `auto_approve_kinds` per
  hand if you want to lower it.

---

## 3. Hand decision loop — flowchart

What the hand does on every poll cycle (default every 30s). This is
`RanchHand.run()` in `ranch/hand.py` rendered as a tree.

```mermaid
flowchart TD
  Start([poll tick]) --> Stop{stop sentinel<br/>set?}
  Stop -- yes --> Exit([exit])
  Stop -- no --> Active{any active<br/>run?}
  Active -- yes --> Wait1[idle log<br/>+ sleep]
  Wait1 --> Start
  Active -- no --> Approved{any approved<br/>parked propose?}
  Approved -- yes --> Carry[pre-seed Dossier with<br/>plan + acceptance]
  Carry --> FireExec[spawn Orchestrator<br/>execute mode]
  FireExec --> Start
  Approved -- no --> Recent{recent parked?<br/>(24h window)}
  Recent -- yes --> Wait2[idle log<br/>+ sleep]
  Wait2 --> Start
  Recent -- no --> Triage[ranch triage<br/>--json]
  Triage --> Got{got<br/>candidate?}
  Got -- no --> Wait3[idle log<br/>+ sleep]
  Wait3 --> Start
  Got -- yes --> Scope[ranch scope --save]
  Scope --> Propose[spawn Orchestrator<br/>propose mode]
  Propose --> Park([parked, awaiting<br/>operator review])
  Park --> Start

  classDef built fill:#1e3a2e,stroke:#5cb85c,color:#e6e9ef
  classDef gate fill:#3a2e1e,stroke:#b8945c,color:#e6e9ef
  class Active,Approved,Carry,FireExec,Recent,Triage,Scope,Propose,Got,Stop built
  class Park gate
```

**What's NOT in this loop yet** (would extend the `Approved` branch with
more unblock kinds):

```mermaid
flowchart LR
  ParkedHand[parked /<br/>completed run] -->|approve interjection| Now([fires execute ✓])
  ParkedHand -.->|PR review comment landed| Future1[respond-pr flow<br/>✗ NOT WIRED]
  ParkedHand -.->|CI status flipped| Future2[resume + emit dossier<br/>✗ NOT WIRED]
  ParkedHand -.->|deploy completed| Future3[verify on labs URL<br/>✗ NOT WIRED]
  ParkedHand -.->|ticket B in portfolio<br/>now unblocked| Future4[swap focus<br/>✗ NOT WIRED]

  classDef built fill:#1e3a2e,stroke:#5cb85c,color:#e6e9ef
  classDef gap fill:#3a1e1e,stroke:#b85c5c,color:#e6e9ef
  class Now built
  class Future1,Future2,Future3,Future4 gap
```

The four dashed paths are the "monitoring for blocks and unblocks"
layer — see **H11 v2 full** (currently issue body of #81) and the
new ticket for unblock-monitoring extensions.

---

## 4. Memory ownership boundary

Where every persistent fact lives. The boundary is enforced by
[#92 (Memory ownership)](https://github.com/ethandrower/ranch/issues/92).
**Anything that makes our agents smarter** — prompts, lessons, dossier
history, decisions — lives below the line. **Anthropic services live
above the line and own nothing across SDK calls except the running
conversation tape.**

```mermaid
flowchart TD
  subgraph above["Anthropic-hosted (NOT used for our memory)"]
    direction LR
    Conv["sdk_session_id<br/>+ resume= conversation tape<br/>(acceptable: per-session only)"]:::ok
    Memstores["Memory Stores<br/>(/mnt/memory)"]:::banned
    Managed["Managed Agents memory"]:::banned
    HostedReg["Hosted agent registry"]:::banned
    FilesAPI["Files API persistence"]:::banned
  end

  subgraph below["Ranch-owned (SQLite + repo)"]
    direction TB
    Tables[("ranch.db tables<br/>Run, Dossier, Checkpoint,<br/>Interjection, Feedback,<br/>Lesson, ReflectionRun,<br/>ReviewComment")]
    Prompts["System prompts<br/>(in-repo .py constants)"]
    ReflectionPrompt["Reflection prompt<br/>(ranch/reflect.py)"]
    Append["append_system_prompt<br/>injection mechanism"]
    CLAUDE_md["Per-worktree CLAUDE.md<br/>(in app repo)"]
  end

  Boundary{{"Memory ownership boundary<br/>(everything that makes agents smarter stays below)"}}

  above -.cannot cross.-> Boundary
  Boundary --> below

  classDef ok fill:#1e3a2e,stroke:#5cb85c,color:#e6e9ef
  classDef banned fill:#3a1e1e,stroke:#b85c5c,color:#e6e9ef
```

The accepted Anthropic surface (`sdk_session_id` + `resume=`) is the
running tape of one SDK call, not "smart memory" the system uses
across sessions. When `ranch hand` re-enters a paused session, it does
so via that resume primitive — which is fine.

If you ever feel tempted to lean on Memory Stores or Managed Agents
memory because they'd save code: **don't**. Write down what you'd put
there as a new SQLAlchemy table or a new column instead. See #92 for
the long version.

---

## 5. MCP tool surface — what the agent sees

Inside every SDK session the agent has these four ranch-specific tools
plus the standard Read/Write/Edit/Bash/Grep/Glob set. Each tool's body
just acknowledges; the **PostToolUse hooks** are where the actual
behavior lives.

```mermaid
flowchart LR
  Agent[Claude SDK<br/>session]
  Agent -->|"record_state(plan, just_did,<br/>state, blocker, options,<br/>details, acceptance, files_touched)"| RSHook[make_dossier_hook<br/>ranch/runner/dossier.py]
  Agent -->|"record_checkpoint(kind, summary,<br/>payload)"| CHook[make_checkpoint_hook<br/>ranch/runner/checkpoints.py]
  Agent -->|"log_decision(decision,<br/>rationale)"| LHook["(no hook — body only)"]
  Agent -->|"run_acceptance(checks?)"| JHook[make_judge_hook<br/>ranch/runner/judge_hook.py]

  RSHook -->|writes| DTable[("Dossier table")]
  CHook -->|writes| CTable[("Checkpoint table")]
  CHook -.->|"if APPROVAL_REQUIRED<br/>BLOCKS for !approve/!reject"| Gate{operator<br/>gate}
  JHook -->|spawns subprocess<br/>+ httpx| Run["ranch.judge.run_acceptance"]
  Run -->|writes structured<br/>results to| Reply["additionalContext on<br/>tool result (agent reads)"]

  classDef hook fill:#3a2e1e,stroke:#b8945c,color:#e6e9ef
  class RSHook,CHook,JHook hook
```

Key bits:
- **`record_checkpoint(kind in {plan_ready, pre_push, ...})` BLOCKS** the
  agent's tool call until the operator decides (or `auto_approve_kinds`
  fires). The decision returns as `additionalContext` on the same tool
  result — agent's next turn sees it attached to its own call.
- **`run_acceptance` is non-blocking** but returns pass/fail per check
  in `additionalContext`. Per-session budget guard (default 8 calls)
  prevents iterate-forever loops.
- **`record_state` is purely informational.** The dossier table is the
  primary UI fuel (Confluence-expand timeline per #72).

---

## 6. Status — what's built vs. what's not

| Capability | Status | Where |
|---|---|---|
| Triage assigned Jira tickets, score by viability | ✅ shipped | #74 (PR #86) |
| Scope ticket (epic + sisters + open PRs + design) | ✅ shipped | #75 (PR #87) |
| Bounded propose with structured acceptance | ✅ shipped | #76 (PR #90) |
| Self-judge integration loop (`run_acceptance`) | ✅ shipped | #78 (PR #93) |
| PR draft + open from accumulated artifacts | ✅ shipped | #80 (PR #94) |
| Ranch hand daemon (poll + triage → scope → propose) | ✅ shipped | #81 (PR #91) |
| Hand auto-fires execute on operator approval | ✅ shipped | #81 (PR #95) |
| Dossier schema, MCP tool, persistence, live CLI views | ✅ shipped | #71-72 (PRs #82-84, #88) |
| **Streaming local docker logs in console** | ✅ shipped | #101 (PR #102) |
| PR review feedback loop primitives | ✅ shipped | Phase 5a/5b (pre-H) |
| **PR review polling integrated into hand loop** | ❌ not wired | gap — see new ticket |
| **CI status monitoring (build green/red wake)** | ❌ not built | gap — see new ticket |
| **Labs deploy status monitoring** | ❌ not built | gap — see new ticket |
| **Multi-ticket juggling per hand** | ❌ not built | H11 v2 full (under #81) |
| Console rendering of dossier (Confluence-expand) | ❌ not built | #72 |
| Per-hand kanban swim-lane | ❌ not built | #85 |
| Interactive takeover (pause SDK → claude --resume → hand back) | ❌ not built | #73 |
| Rewind to stage | ❌ not built | #89 |
| Streaming remote Dokku logs | ❌ not built | #101 Phase 2 |
| All five operator-action UI tickets | ❌ not built | #96-100 |

---

## 7. The unblock-monitoring gap — what's actually missing

This is the gap the user keeps hitting:

> "build takes 20 minutes and I switch tasks then forget about it"

Today the hand only knows one unblock kind: the operator hit `ranch
approve`. The intended-but-unbuilt layer:

```mermaid
flowchart TB
  subgraph TodayLoop["What the hand polls today"]
    A1[Active run?]
    A2[Approved parked propose?]
    A3[Recent parked?]
    A4[Triage]
  end

  subgraph Missing["Should also poll (NOT WIRED)"]
    direction TB
    B1["PR review comments arrived?<br/>(bb pr view --comments)"]
    B2["CI status flipped?<br/>(bb run list --pr <id>)"]
    B3["Labs deploy completed?<br/>(curl health endpoint)"]
    B4["Another ticket in my portfolio<br/>parked at plan_ready, ready to resume?"]
  end

  Trigger([on any 'changed':<br/>resume the affected run +<br/>emit dossier update +<br/>optional native notification]) 
  B1 --> Trigger
  B2 --> Trigger
  B3 --> Trigger
  B4 --> Trigger

  classDef built fill:#1e3a2e,stroke:#5cb85c,color:#e6e9ef
  classDef gap fill:#3a1e1e,stroke:#b85c5c,color:#e6e9ef
  class A1,A2,A3,A4 built
  class B1,B2,B3,B4,Trigger gap
```

This is what the **new H20 ticket** scopes — see the ticket body for
the full design.

---

## 8. Pointers

- **Issue epic:** [#70](https://github.com/ethandrower/ranch/issues/70)
  (Ranch hand — self-driving fleet)
- **Memory ownership doctrine:** [#92](https://github.com/ethandrower/ranch/issues/92)
- **UI scope (specced, not built):** #72, #73, #85, #89, #96, #97, #98, #99, #100
- **The CLI surface today** lives in `USAGE.md` (already current with
  the H stack)
- **Roadmap prose** lives in `ROADMAP.md` (Phase H section)

If you're new and want a 5-minute tour: read this file top-to-bottom,
then skim `USAGE.md` for the commands that map to each diagram.
