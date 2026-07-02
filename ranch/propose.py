"""H6 — `ranch propose <ticket>`: bounded plan + acceptance criteria.

Runs a short, file-system-read-only SDK session that produces an
implementation plan and structured acceptance criteria. Parks at
plan_ready (or simply emits a final dossier in --free mode) so the
human can review the plan cheaply before any code gets written.

Phase H6 of the Ranch hand epic (#70).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from .scope import load_scope_markdown

# Tools available during proposing — no Write, no Edit, no push. Read-only
# exploration of the worktree + dossier emission. Bash is allowed but the
# brief tells the agent not to use it for state changes.
PROPOSE_ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "Bash",
    "mcp__ranch__record_state",
    "mcp__ranch__record_checkpoint",
    "mcp__ranch__log_decision",
]

# Default time budget for a propose session. Long enough that a real-codebase
# plan can land — 180s was fine against an empty/fake worktree but too short to
# explore actual code (model + grep/read tool latency, plus rate-limit retries
# that eat wall-clock) before parking a plan. Tunable via `--budget` on the CLI.
DEFAULT_PROPOSE_BUDGET_SECONDS = 600.0


PROPOSE_SYSTEM_PROMPT = """\
You are scoping a ticket for implementation. Your job is to PROPOSE a
plan + acceptance criteria — NOT to write any code.

## Constraints — HARD RULES

- You may use Read, Grep, Glob to explore the codebase.
- You may use Bash for READ-ONLY commands (ls, cat, git log, git diff,
  git status, find — never anything that mutates state).
- You may NOT use Edit, Write, git add/commit/push, npm install, or any
  command that creates, modifies, or deletes files. The tools you'd
  need for those are not in your toolset; if you try one, it will be
  rejected.
- Do NOT create branches or run tests during proposing.

## What to produce

Emit `record_state` calls as you work, narrating your exploration and
landing on the final proposal. Your FINAL record_state call MUST have:

- state = "parked"
- blocker = "Awaiting plan approval"
- options = [
    {label: "approve", description: "Proceed with this plan"},
    {label: "reject", description: "Send feedback and re-propose"}
  ]
- plan = the ordered implementation steps you'd take during a real run
- details = a multi-paragraph narrative containing:
    1. **Summary** — what you're going to build, in 2-3 sentences
    2. **Plan** — repeat the ordered steps with rationale per step
    3. **Acceptance criteria** — human-readable description of what will prove it works
    4. **Complexity** — S/M/L with one-sentence rationale
    5. **Risks / open questions** — anything the operator should know
- acceptance = STRUCTURED machine-runnable checks (an array). This is the
  contract used by the `run_acceptance` tool during execute (H8). Each item:
    - kind = "unit_test" | "script" | "http"
    - name = short label
    - For unit_test / script: cmd (shell command) + pass_pattern (substring or
      regex in stdout that proves pass)
    - For http: url + expected_status (default 200) + optional expected_body_contains
  Aim for 2-5 checks. They must be RUNNABLE from the worktree root. Do NOT
  invent commands; only use what genuinely exists (e.g. pytest if there's a
  tests/ dir, curl for endpoints the ticket adds, etc.). If a kind isn't
  applicable, simply omit it — don't pad.

Plain markdown is fine — the operator reads `details` in the Confluence-expand
view of the dossier.

## Style

- Be concrete. No "we'll figure that out later" hand-waving.
- Cite specific files (`ranch/cli.py:469`) where relevant.
- If the scope is too ambiguous to propose responsibly, say so in `details`
  and surface a clear "this needs more info from the operator" option
  instead of guessing.
"""


def build_propose_brief(ticket: str, scope_md: str, feedback: str | None = None) -> str:
    """Build the initial user message for a propose session.

    The scope bundle (from H5) is inlined so the agent doesn't need to
    re-query Jira or browse comments during its bounded session.

    `feedback` is the operator's note when a previous plan was sent back for
    revision (the refine loop) — surfaced so the agent revises accordingly.
    """
    feedback_block = ""
    if feedback and feedback.strip():
        feedback_block = (
            "\n────────────── OPERATOR FEEDBACK ──────────────\n"
            "The operator reviewed your PREVIOUS plan for this ticket and sent\n"
            "it back for revision with this feedback:\n\n"
            f"{feedback.strip()}\n\n"
            "Produce a REVISED plan that directly addresses it.\n"
            "────────────────────────────────────────────────\n"
        )
    return textwrap.dedent(f"""\
        Ticket: {ticket}

        You are proposing an implementation plan for this ticket. Below is the
        pre-flight context bundle assembled by `ranch scope`. Read it, explore
        the relevant code, and produce a plan + acceptance criteria as
        described in your system prompt.

        Your final `record_state` call must park (state=parked) with the
        plan in `plan`, the structured proposal in `details`, and the
        approve/reject options surfaced.

        ────────────── SCOPE BUNDLE ──────────────
        {scope_md.strip()}
        ──────────────────────────────────────────
        {feedback_block}
        Begin.
    """)


class ProposeError(RuntimeError):
    """Raised when propose can't run (missing scope, unknown agent, etc.)."""


def resolve_scope_markdown(ticket: str) -> str:
    """Look up the saved scope bundle for a ticket. Raises if not found.

    The ranch hand calls `ranch scope --save` before `ranch propose`, so the
    bundle should always exist by the time we get here. Users running propose
    by hand are expected to run scope first.
    """
    md = load_scope_markdown(ticket)
    if md is None:
        raise ProposeError(
            f"No saved scope for {ticket}. Run `ranch scope {ticket} --save` first."
        )
    return md
