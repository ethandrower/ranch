# Ranch — Vision

> Status: north-star direction (2026-06). Not a status doc — see `ROADMAP.md`/`ARCHITECTURE.md` for what's built.

## One line

An **AI-native company OS**: orchestrate the tools a company already uses (Jira, Confluence, GitHub/Bitbucket, Slack) behind one unifying layer those tools structurally lack — a **people / capacity view** over a workforce that is part human, part AI.

## The problem

Every modern work tool centers an **artifact**:

- Jira centers the **issue**. Confluence centers the **page**. Slack centers the **channel**. GitHub centers the **repo**.

The *employee* is a foreign key in all of them (an assignee, an author, a @handle). The *AI* employee is worse — a bolted-on bot or API client, never a first-class worker. There is no tool whose root object is the worker, and none that treats human and AI workers as the same type. So as you add AI employees, the entire stack works **against** them.

## The thesis

Re-center on the **employee (human or AI)** as the root object. Tasks, docs, code, and comms hang off employees as the things they produce and consume. **Roles, goals, and accumulated memory become first-class — because that's what an employee has.** Incumbents can't retrofit this; it would mean re-architecting around a primitive they don't have.

The unit is the company's **roster of workers**, which is inherently per-company — hence *one platform per company*, that grows as humans and AI employees accumulate for specific roles.

## The strategy: orchestrate, don't replace

We do **not** rebuild Jira / Confluence / GitHub / Slack. Trying to out-build five mature product categories at once (docs, work-tracking, code/QA, comms, performance-management) ships a worse version of each and wins none.

Instead:

- Connect to the existing tools as **connectors** (two-facet model — see `connector-model.md`).
- Build natively **only** the layer the incumbents lack: the unified employee model (roles + goals + memory) and the people/capacity view over it.
- **Absorb** a surface into the platform only when it is so anti-agent that ownership beats integration — incrementally, surface by surface, paid for by demonstrated pain. Replacement is the long arc, earned, not the opening move.

## The unifying view: people / capacity

The view no incumbent can produce, because it requires the unified employee model to exist:

- **Who** each employee is (human or AI), their role, and current work.
- **Progress toward goals**, with idea → epic → story → code drill-down (optional depth).
- **Who is blocked / stuck** — detected across tools, not self-reported.
- **Who has headroom** — capacity as a live, allocatable resource. For the AI half, capacity can be *added on demand*.

## The wedge

Lead with **goals + observability for AI employees** — APM for knowledge work. Every action an AI employee takes is observable, so progress-against-goal is measurable in a way it never was for humans (who could only be self-reported and reviewed quarterly). Dogfood on the existing mixed citemed team (owner + 4 AI engineer hands). Extend to human employees later via integrations + self-report.

The observability-against-goals view is the ten-second demo of the whole thesis; the idea→code drill-down is its tasks-and-code face.

## Why ranch can build this

This is the **generalization of what ranch already is** — a manager's cockpit for AI hands with DB-owned memory and a learning loop. Not a pivot; the next phases generalized. The connector model, the declarative-vs-learned memory boundary, the employee-type manifest, and the Electron console are the pieces, already on the board.

Unfair advantage: ranch is *already operating* a human+AI team doing real work, so it can dogfood what others can only theorize.

## Non-goals (for now)

- Not a Jira/Confluence/Slack/GitHub replacement.
- Not a general chatbot framework.
- Not hosted-memory-dependent (see `ARCHITECTURE.md §4` / memory-ownership doctrine).

## Glossary (locked vocabulary)

- **Employee** — any worker in the platform, human or AI. The root object; the unit of the people/capacity view. A worker *has* a role, goals, memory, and channels.
- **Hand** — an **AI employee** specifically (the ranch term). Every hand is an employee; not every employee is a hand. "Engineer hands" = the AI engineering team.
- **Operator / owner / manager** — the human running the platform, orchestrating employees.
- **Employee type** — a role template (engineer, support, research, PM…) = role prompt + granted tools + wired channels + goals (see `connector-model.md`).

## Inference & lock-in posture

Claude Code (on the Max subscription) is the **default inference path** — cheapest (subscription-flat) and currently best for coding hands — but not the *only* one. Lock-in is mitigated at two seams: (1) memory/learning already lives in `ranch.db`, not a vendor store (`ARCHITECTURE.md §4`), so the expensive lock-in is already neutral; (2) execution sits behind an **employee-runtime adapter** interface, with Claude Code as the first adapter and model-API / OpenClaw-style adapters addable later via a model gateway. Pay the portability tax only when a real need forces it (a customer on another provider, a non-coding employee type).
