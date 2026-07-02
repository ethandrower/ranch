# Ranch — The Foreman Model

> Status: design spec (2026-06). The harness architecture for the Ranch agent
> fleet. Supersedes the in-session blocking-checkpoint design in
> `runner/orchestrator.py` (see §11, Migration). Anchored to *Loop Engineering:
> The Anthropic Playbook* (HuaShu / Osmani / Cherny / Steinberger, Jun 2026) —
> referred to below as **the paper**.

## One line

The **Foreman** is the loop that runs the Ranch fleet: a durable, MCP-connected
decision layer that dispatches one-shot worker agents, verifies their output
with independent evaluators, talks to Jira/Slack/Bitbucket itself, and keeps
human doors open. Claude Code is **one kind of worker** at its disposal — not
the system.

---

## 1. The core reframe: Claude Code is not the only way to run an LLM

The trap is treating the system as "dumb harness vs. Claude Code (all the
intelligence)." Claude Code is just **one configuration of the Claude Agent
SDK** — the heavy one: full filesystem tools + an autonomous loop over a
working copy. A *coding* agent.

The Foreman can *also* call the SDK with a **small tool-belt and a short
leash**: Jira/Slack MCP only, no repo, one structured response. That is "the
Foreman doing it itself, intelligently." Same SDK, different tool grant +
prompt.

This maps onto the paper's **four-layer stack** (Table I): prompt → context →
harness → **loop**. The Foreman is the loop layer ("make it run itself over and
over"); a Hand is a harness-armed single run ("arm one run: tools, actions,
done").

---

## 2. The roster (locked vocabulary)

Extends the `vision.md` glossary (Employee / Hand / Operator).

- **Foreman** — the harness/loop. Runs the cycle the operator controls, owns
  every routing decision, talks to external systems, and holds the human doors
  open. On a ranch, the foreman runs the outfit day-to-day *on behalf of the
  owner*; he assigns hands and judges results, he doesn't rope every steer.
- **Hand** — a worker (generator) the Foreman dispatches to *do the work*.
  Claude-Code-backed for now. Cowboys that do the work.
- **Brand Inspector** — an independent evaluator the Foreman summons to verify
  **correctness**. Modeled on the livestock brand inspector: a *state* official,
  not on the ranch payroll, who verifies the herd before it's sold and catches
  fraud. **It answers to the standard, not to the Foreman.** Fresh context,
  assumes broken, *acts* (runs tests, drives the app). Ideally a different
  vendor (see §6).
- **Design Inspector** — the same idea for **usability/UX**. Acts via the test
  rig (screenshots the rendered UI), judges against heuristics + design-system
  consistency + a11y, assumes the UX is bad until shown otherwise.
- **Operator** — the human owner running the platform.

> Role ≠ backend. "Brand Inspector" / "Hand" / "Design Inspector" describe the
> *job the Foreman assigns*. The *runtime adapter* behind the job (Claude /
> Codex / …) is independent and swappable (§6). "The Brand Inspector is a
> Codex-backed evaluator" is one valid config.

---

## 3. One turn of the Foreman — the five moves

The paper (§III) says a loop that drops any of the **five moves** is one of five
broken loops (§9). The Foreman's turn installs all five:

| Move (paper) | Foreman step | Ranch status |
|---|---|---|
| **Discovery** — find this turn's work itself | hit Jira MCP, triage assigned tickets | ✅ `triage.py` — promote to a **skill**, not inline prompt |
| **Handoff** — isolated worktree per task | dispatch a Hand into a per-hand **worktree** | ✅ jeffy/arnold/max/kesha pattern |
| **Verification** — a *separate* agent that says "no" | summon the **Brand Inspector** (+ Design Inspector for UI) | ❌ **the gap** — today's self-judge is the nodding loop (§7) |
| **Persistence** — state outside the conversation | write dossier/decisions to `ranch.db`, open PR, comment Jira | ✅ strong (Dossier, pr_draft, memory doctrine #92) |
| **Scheduling** — run round after round | Foreman poll-loop + Slack `@bot` trigger | ✅ `hand.py` polls; local loop is correct (needs local worktrees + docker, Table IV) |

---

## 4. Three execution types — the dispatch rule

Every step in the loop is one of three things. The decision rule is one
question: **does the step need to touch the filesystem / repo?**

| Type | What it is | Tools | Use for |
|---|---|---|---|
| **1. Mechanical** | plain Python, no LLM | — | poll, route, check DB, git status, "active run?" |
| **2. Reasoning call** | Foreman fires a *small* SDK call + MCP → structured JSON | Jira/Slack MCP, no repo | find the work, rank, decide next step, draft a Slack/Jira message |
| **3. Worker session** | spawn a *one-shot* full Hand in a worktree | Read/Write/Edit/Bash + ranch MCP | the actual coding: scope-by-reading-code, write, test, fix |

- **Needs the repo** → type 3 (a Hand). Only the autonomous loop is good at it.
- **Needs judgment, not the repo** → type 2 (Foreman does it itself). Spawning a
  Hand here is a sledgehammer — slow, and you fight it for clean output.
- **No judgment** → type 1.

Example — "find the work you should do" is type 2 (reason over Jira data, no repo
needed). "Write the code" is type 3.

---

## 5. One-shot sessions — the load-bearing change

Today approval is a **synchronous block inside a PostToolUse hook**:
`record_checkpoint(pre_push)` halts the agent's tool call and awaits a human
decision (`runner/checkpoints.py`, `orchestrator.py:95-122`). That one decision
is the root of the pain — a long-lived stateful process the UI must attach to
and drive, handoffs parked mid-tool-call, async unblocks with nowhere to go.

**The fix:** a Hand runs to completion with its own internal loop
(code→test→fix stays *inside* one session), and at a gate it **records its
dossier and exits cleanly** instead of blocking. The Foreman sees "run ended at
`pre_push` with this dossier," owns the decision, and spawns the next one-shot
with `resume=session_id` + new context. **Approval becomes a graph edge, not a
parked thread.**

Rule: **a session boundary is wherever a human or slow external event must
intervene.** Everything between gates stays in one session (don't pay resume
cost on tight inner loops). Only plan-approval, pre-push, PR-review, CI-wait are
boundaries.

What this keeps: the MCP tools (`record_state`, `record_block`), the
dossier/`acceptance` contract, `resume=sdk_session_id`. What it deletes: the
blocking checkpoint hook, the stdin `!cmd` loop, the in-orchestrator
interjection poll. The gate moves *out* to the Foreman.

---

## 6. The runtime-adapter seam — swappable engines

The Foreman talks to workers through a narrow contract, not through Claude Code.
This is the **employee-runtime adapter** already named in `vision.md`.

```
Hand.run(instructions, context, worktree) -> Verdict | Dossier
   • input:  instructions + context (+ worktree path)
   • output: structured result (JSON)
   • effect: operates on a real git worktree
```

**Agnostic = the interface + capability flags, NOT a pile of pre-built
adapters.** Build the seam once (cheap — it's just not hard-coding Claude
assumptions); build a concrete adapter per backend you actually want (CC now,
Codex when you want cross-vendor review, others later) with **zero Foreman
changes**.

The discipline that makes it genuinely agnostic: treat what varies across
backends as **optional capabilities a backend advertises**, with graceful
degradation —

| Varies | Handled as |
|---|---|
| Invocation (CLI / SDK / HTTP) | adapter's private concern |
| Live event stream | optional → cockpit degrades to "running… → result" |
| MCP / tool support | capability flag |
| `resume=session_id` (Flavor A takeover) | capability flag → backends without it skip mid-stream takeover |
| Approval/permission model | adapter normalizes to the Foreman's gate |

Adapters: `ClaudeCodeAdapter` (first), `CodexAdapter` (cross-vendor),
others as needed. The **evaluator is the ideal first foreign backend** — it's
read-mostly (no resume, no convention-fit, just read diff → run tests →
verdict), so it carries the least risk.

---

## 7. Generator / evaluator — the independent Inspector

The paper's hardest and highest-leverage move (§V). An agent grading its own
output praises it:

> "Tuning an independent skeptical evaluator is far more tractable than making a
> generator critical of its own work." (the structural GAN insight)

**Ranch's current `run_acceptance` self-judge is the nodding-loop anti-pattern**
— the same session that wrote the code grades it. Replace it with an independent
**Inspector**:

- **Fresh context, ideally a different model/vendor** (Codex) — "the same model
  with new instructions often keeps its blind spots." A different vendor =
  zero shared blind spots, and lock-in insurance.
- **Defaults to doubt** — "this is BROKEN until proven otherwise."
- **Acts, not reads** — runs the tests; for UI, *drives the app* via the test
  rig (§8): "judge behavior, not intent."
- Returns structured PASS / REJECT-with-reasons. The **Foreman** reads it and
  routes (→ persist & PR · → bounce to a Hand · → escalate `needs-human`).

Two lenses, summoned independently (perspective-diverse verify):
- **Brand Inspector** — correctness (tests, behavior matches ticket).
- **Design Inspector** — usability (UX heuristics, design-system consistency,
  a11y), via screenshots.

UI tickets get both; backend tickets get only the Brand Inspector. Declared per
ticket type.

---

## 8. The deterministic / LLM interlock (Stripe's Minions, paper Fig 5)

Reliability comes from the **constraints, not the model size.** Stripe's pipeline
assembles context deterministically (*not* with the LLM) and hard-codes every
gate it can. Our Foreman adopts the same interlock:

```
Slack @bot trigger          → Foreman (operator kick/approve)          [human]
assemble Jira + scope       → Foreman, deterministic where possible    [type-1/2]
write code                  → Hand                                     [type-3 LLM]
lint / tests gate           → hard-coded, the Hand CANNOT skip         [type-1]
fix failures                → Hand                                     [type-3 LLM]
git commit                  → hard-coded                               [type-1]
independent review          → Inspector(s)                             [type-3 LLM, fresh model]
human review (open door)    → Foreman pauses; Flavor A takeover        [human]
```

Rule: **anything a rule can decide is taken out of the model's hands.** This is
also how the test-rig login problem is solved (§8.2).

---

## 8.1 The test rig — sessions that test themselves

Browser control is what makes verification *real* (the paper's "act, not read").
It is a **first-class component**, wired into both the Hand's self-test (fast
smoke) and the Inspector's authoritative behavioral check.

**Tool split** — the interactive extension is the wrong tool for an autonomous
loop:

| Tool | Job |
|---|---|
| **Playwright MCP** (headless, isolated context per run) | the **loop's** hands-free rig — Hand self-test + Inspector verify. Autonomous, unattended, no live-Chrome dependency. |
| **claude-in-chrome** | the **operator's** interactive tool during Flavor A takeover / ad-hoc debugging, driving the real session. |

The loop never depends on the flaky interactive one.

## 8.2 Take auth out of the agent's hands

The "logs in every time, flakily" pain comes from making the LLM fumble a login
UI — exactly the rule-based work the interlock (§8) says to hard-code.

1. **Seed an authenticated session deterministically** — a type-1 step (not the
   agent) logs in once and saves Playwright `storageState`, or programmatically
   auths against the **known worktree dev server + seed creds** (per-agent dev
   servers, e.g. jeffy on :8001 with seeded test data).
2. **Hand the agent a pre-authenticated browser context.** It opens the app
   already logged in and goes straight to testing. No login UI, no flakiness.

---

## 9. The design dimension

Design is the **UX face** of the same generator/evaluator/human-door structure.

### Design skill (the generator side)
Does **both directions**:
- **Generate** interactive **HTML/JS prototypes** using the **real Tailwind
  config + a component-pattern catalog** (how a CiteMed button / card / modal /
  table actually looks). On-system by construction; the approved look is what
  ships. HTML/JS gives *more* interactivity than Figma click-throughs.
- **Implement** from a provided design source.

### Design source is pluggable (mirrors the runtime-adapter seam)
The Hand is agnostic to the design input:
- **HTML/JS prototype** — loop-generated, fast, on-system. The default.
- **Figma mockup** — designer-produced, hi-fi. Via the Figma MCP + Code Connect
  / `get_design_context` tooling.
- Given either, the Hand implements from it. Given neither, it generates the
  HTML prototype. "Provided and prompted" picks the mode.

### The flow — prototype-first, escalate when warranted, implement from either
```
UI ticket
  → Hand builds an interactive HTML/JS prototype (real Tailwind config)   [fast first draft]
  → taste gate: Design Inspector + operator `needs_design_feedback`
  → branch:
      (a) good enough        → Hand implements real components from the prototype
      (b) needs real design   → escalate_to_designer  (the prototype IS the brief)
                              → designer returns a Figma file
                              → Hand implements from Figma
```

The HTML prototype is **both a shippable path and the brief for a designer**. AI
does the cheap first draft; the designer elevates only the surfaces that deserve
it (net-new visual language, high-visibility/marketing, brand work); the Hand
implements whichever comes back.

> `escalate_to_designer` is the first place the Foreman coordinates a **human
> employee** (the designer) — work handed out, result returned as an input an AI
> Hand consumes. That's the `vision.md` human+AI workforce thesis, concrete.

---

## 10. The three human doors

The paper insists the human checkpoint is **permanent, not scaffolding to
remove** ("the existence of the pause keeps the human in the position of being
able to"). The Foreman holds three:

1. **Flavor A takeover** (code) — the Foreman pauses a workstream; the operator
   resumes the *same session* interactively (`claude --resume <id>` over the
   same worktree, via claude-in-chrome), fixes/redirects, then hands back. Built
   on `resume=session_id` + the shared worktree. We never barge into a live
   mid-tool-call session — we stop at the next clean stop, then take over.
2. **`needs_design_feedback`** (taste) — the Foreman surfaces the rendered
   artifact (prototype URL / screenshot / Figma frame) in the cockpit, captures
   freeform notes, feeds them back as context. Taste is the one thing the loop
   cannot self-generate without becoming an echo chamber.
3. **`escalate_to_designer`** (outbound human handoff) — see §9.

---

## 11. The cockpit & the three disciplines

Once Hands are one-shot and state lives in `ranch.db` + the Foreman's
checkpointer, **the UI is a read-view over durable state**, not a process
driver. If it's closed, the Foreman keeps running and everything is recorded.

- **Plain web app ← SSE/WebSocket ← FastAPI sidecar (`ranch/api/app.py`, :8421)
  ← event stream (`events.py` / `emit_event`) + DB state.** Read-view + action
  buttons (approve / reject / note / take-over) that POST triggers the Foreman
  picks up. **Drop Electron + PTY** (the process-driving part is the rigid
  part). Not LangSmith — that shows prompt/token internals; we want an ops
  cockpit ("max is on ECD-1669, step 4/7, editing serializers.py, next gate
  ~3min").

The paper's §XI disciplines map onto the cockpit's guardrails:

1. **Read a sample, always** (vs comprehension rot) → *this is the cockpit.* A
   window to read a representative sample of what the loop did each day. Not
   polish — operational discipline.
2. **Cap before you ship** (vs token blowout) → per-run + daily + max-retry
   circuit breakers. Generalize the existing `budget_seconds` + 8-call judge
   budget.
3. **Keep one door open** (vs cognitive surrender) → the three human doors (§10).

---

## 12. Five ways a loop goes wrong (design against these)

Each anti-pattern (paper §VI) = one move skipped. The Foreman must install all
five or it *is* one of these:

- **Nodding loop** (verification skipped) → MUST have the independent Inspector.
  *This is the one Ranch currently violates via self-judge.*
- **Amnesiac loop** (persistence skipped) → `ranch.db`. ✅
- **Manual loop** (scheduling skipped) → real trigger/poll. ✅
- **Blind loop** (discovery skipped) → triage-as-skill, Foreman finds its own
  work. ✅
- **Tangled loop** (handoff skipped) → one worktree per task. ✅

---

## 13. Ranch's first-loop checklist (paper Table VI, filled in)

| Element | Ranch answer |
|---|---|
| Discovery source | Jira triage (assigned, not-Done) — as a skill |
| State file / memory | `ranch.db` (Dossier, Checkpoint, Decision) |
| **Evaluator** | **Inspector(s) — fresh model, assumes broken, acts via the test rig** ← build this |
| Isolation | per-hand git worktree |
| Token cap | per-run + daily + max-retry circuit breakers |
| Human doors | Flavor A takeover · `needs_design_feedback` · `escalate_to_designer` |

---

## 14. Gap analysis — what to build

Strong already: persistence, scheduling, discovery, handoff/worktrees,
DB-owned memory, the dossier/acceptance contract, `resume=`.

To build, in order:
1. **One-shot de-block** (§5) — make sessions exit at gates instead of blocking.
   The load-bearing change; framework-independent. Smallest path: keep `hand.py`,
   delete the blocking checkpoint hook.
2. **The Inspector** (§7) — replace self-judge with an independent evaluator
   that acts via the test rig. First as a fresh Claude session; then swap to a
   Codex adapter behind the runtime seam (§6).
3. **The test rig** (§8.1–8.2) — Playwright MCP + deterministic auth seeding.
4. **The cockpit** (§11) — read-view web app over the sidecar; drop Electron.
5. **The design skill + Design Inspector + doors** (§9–10).

Substrate choice (deterministic state machine vs LangGraph vs deep-agents) is
deliberately deferred — the loop is mostly known, so a deterministic Foreman
with explicit type-2 reasoning nodes is the most debuggable. Adopt LangGraph
when the **inbound channel** (Slack/PR/CI events waking the loop) needs durable
resume; reach for the deep-agents planner only for a future portfolio-level
"PM hand."

---

## References
- *Loop Engineering: The Anthropic Playbook for Designing Systems That Prompt
  Your Agents* (HuaShu, Jun 2026) — the five moves, six parts,
  generator/evaluator, five failure modes, three disciplines, Stripe Fig 5.
- `vision.md` — the AI-native company OS; employee-runtime adapter; human+AI
  workforce.
- `connector-model.md` — tool facet vs channel facet; declarative-vs-learned
  memory boundary.
- `ARCHITECTURE.md` — the current (to-be-superseded) in-session design;
  §7 unblock-monitoring gap.
