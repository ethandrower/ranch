"""Initiative resolution — the bridge between Jira labels and ranch's
HandInitiative scope.

Source of truth: the Jira label `ranch-initiative:<key>` on a ticket.
Operator can override at dispatch time via `--initiative <key>` on the
CLI. The view-model + sidecar surface the resolved value as
`Run.initiative_key`.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select

from .db import db_session
from .models import HandInitiative, Initiative


LABEL_PREFIX = "ranch-initiative:"
ROUTING_LABEL_PREFIX = "ranch-"


def route_label_for_hand(hand_name: str) -> str:
    """The Jira label that routes a ticket to a specific hand.

    Example: route_label_for_hand("max") -> "ranch-max".

    Operator workflow: create a ticket, assign it to the ranch-hand user
    account, add the `ranch-<hand>` label. The hand's triage loop picks
    it up; no other hand will. Multiple labels can be added to fan a
    ticket out to multiple hands, though that's an unusual case.
    """
    return f"{ROUTING_LABEL_PREFIX}{hand_name.strip().lower()}"


def extract_initiative(labels: Iterable[str]) -> Optional[str]:
    """Return the first `ranch-initiative:<key>` label's key, or None.

    Labels are matched case-insensitively and trimmed. We deliberately
    take the first match rather than raising on multiples — Jira UI lets
    operators add multiple labels and we want to fail open, not crash
    triage. Multi-initiative tickets are vanishingly rare.
    """
    for label in labels or ():
        if not isinstance(label, str):
            continue
        stripped = label.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith(LABEL_PREFIX):
            return stripped[len(LABEL_PREFIX):].strip().lower()
    return None


def initiatives_for_hand(hand_name: str) -> list[str]:
    """Return the initiative keys this hand watches, in sort order.

    Used by triage to scope the JQL query and by the hand scheduler to
    filter pickup candidates.
    """
    with db_session() as session:
        rows = session.execute(
            select(HandInitiative.initiative_key)
            .where(HandInitiative.hand_name == hand_name)
            .order_by(HandInitiative.sort_order)
        ).all()
    return [r[0] for r in rows]


def default_initiative_for_hand(hand_name: str) -> Optional[str]:
    """The hand's default initiative — used when dispatch doesn't specify
    and the ticket has no `ranch-initiative:` label."""
    with db_session() as session:
        row = session.execute(
            select(HandInitiative.initiative_key)
            .where(HandInitiative.hand_name == hand_name)
            .where(HandInitiative.is_default == 1)
            .limit(1)
        ).first()
    if row:
        return row[0]
    # Fall back to first in sort order
    keys = initiatives_for_hand(hand_name)
    return keys[0] if keys else None


def initiative_exists(key: str) -> bool:
    with db_session() as session:
        return session.execute(
            select(Initiative.key).where(Initiative.key == key).limit(1)
        ).first() is not None


def jql_label_clause(initiative_keys: Iterable[str]) -> str:
    """Build a JQL clause that matches tickets carrying any of the
    `ranch-initiative:<key>` labels for the given keys.

    Returns the empty string if no keys are given (caller should treat
    that as "no scoping"). Sample output:

        labels in ("ranch-initiative:ref-mgmt", "ranch-initiative:misc")
    """
    keys = [k.strip() for k in initiative_keys if isinstance(k, str) and k.strip()]
    if not keys:
        return ""
    quoted = ", ".join(f'"{LABEL_PREFIX}{k}"' for k in keys)
    return f"labels in ({quoted})"


def resolve_initiative_for_run(
    *,
    operator_override: Optional[str],
    ticket_labels: Iterable[str],
    hand_name: str,
) -> Optional[str]:
    """Resolution precedence for a new Run's initiative_key:

    1. Operator explicit override via `ranch dispatch --initiative <key>`
    2. Jira label `ranch-initiative:<key>` on the ticket
    3. Hand's default initiative (if any)
    4. None (ungrouped — lands in 'misc' if the hand has it, else
       invisible in the board-per-initiative UI)

    Validates that the resolved key exists in the Initiative table — if
    not, treats it as None and lets the operator notice.
    """
    candidates = [operator_override, extract_initiative(ticket_labels),
                  default_initiative_for_hand(hand_name)]
    for cand in candidates:
        if cand and initiative_exists(cand):
            return cand
    return None
