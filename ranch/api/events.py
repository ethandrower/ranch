"""In-process event bus for SSE streaming.

The sidecar publishes events here (dossier writes, CI flips, new review
comments) and any connected /api/stream client gets them via SSE.

Process-local — works because the sidecar AND the agent runtimes share a
process when launched via `ranch serve`. For the H22 multi-process model
this graduates to a pub/sub-on-DB-row pattern; for now in-memory is fine.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

# Subscribers: each renderer connection holds an asyncio.Queue we push into.
_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()


def publish(event_type: str, payload: dict[str, Any]) -> None:
    """Fan an event out to every active subscriber. Non-blocking — drops
    on a full subscriber queue rather than back-pressuring the writer.
    """
    msg = {"type": event_type, "ts": time.time(), "data": payload}
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # Subscriber is slow — drop rather than block dossier writes.
            # A re-fetch on reconnect catches them back up.
            pass


async def subscribe() -> AsyncIterator[str]:
    """Yield SSE-formatted lines for one client. Generator lifetime ==
    client lifetime; cleanup happens in the `finally`."""
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
    _subscribers.add(q)
    try:
        # Yield the raw JSON payload only. EventSourceResponse (in app.py)
        # does the `data: ...\n\n` SSE framing — pre-framing it here too
        # produced a double `data: data: {...}` wire format that failed
        # JSON.parse in the browser EventSource and silently dropped every
        # event.
        # Initial hello so the client knows the channel opened.
        yield json.dumps({"type": "hello", "ts": time.time()})
        while True:
            msg = await q.get()
            yield json.dumps(msg)
    finally:
        _subscribers.discard(q)
