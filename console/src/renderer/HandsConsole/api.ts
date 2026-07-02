/**
 * HTTP client for the ranch sidecar (P2).
 *
 * Default base URL: http://127.0.0.1:8421 (the sidecar's default port). Override
 * via VITE_RANCH_API_URL at build/dev time. All requests are localhost-only;
 * never expose the sidecar externally.
 */
import type { HandSummary, HandView, Ticket } from './types';

export const API_BASE =
  import.meta.env.VITE_RANCH_API_URL ?? 'http://127.0.0.1:8421';

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });
  if (!r.ok) {
    throw new ApiError(r.status, await r.text().catch(() => r.statusText));
  }
  return r.json() as Promise<T>;
}

export async function fetchHandSummaries(): Promise<HandSummary[]> {
  return jsonFetch<HandSummary[]>('/api/hands');
}

export async function fetchHand(name: string): Promise<HandView> {
  return jsonFetch<HandView>(`/api/hands/${encodeURIComponent(name)}`);
}

export async function fetchTicket(key: string): Promise<Ticket & { run_id?: number }> {
  return jsonFetch<Ticket & { run_id?: number }>(`/api/tickets/${encodeURIComponent(key)}`);
}

export async function fetchStepDetails(ticketKey: string): Promise<Record<string, string>> {
  return jsonFetch<Record<string, string>>(`/api/tickets/${encodeURIComponent(ticketKey)}/step-details`);
}

export interface JiraComment {
  id: string;
  author: string;
  body: string;
  created: string;
  updated?: string;
}

export interface JiraContext {
  key: string;
  summary: string;
  description: string;
  status: string;
  priority: string | null;
  type: string | null;
  labels: string[];
  assignee?: { name?: string; email?: string };
  reporter?: { name?: string };
  created: string;
  updated: string;
  epic_key: string | null;
  comments: JiraComment[];
}

export async function fetchJiraContext(ticketKey: string): Promise<JiraContext> {
  return jsonFetch<JiraContext>(`/api/tickets/${encodeURIComponent(ticketKey)}/jira-context`);
}

export interface ActivityEntry {
  icon: string;
  title: string;
  detail?: string | null;
  ago: string;
}

export async function fetchTicketActivity(ticketKey: string): Promise<ActivityEntry[]> {
  return jsonFetch<ActivityEntry[]>(`/api/tickets/${encodeURIComponent(ticketKey)}/activity`);
}

export interface TicketDiff {
  ok: boolean;
  reason?: string;
  branch?: string | null;
  cwd?: string;
  stat?: string;
  patch?: string;
  truncated?: boolean;
  untracked?: string[];
}

export async function fetchTicketDiff(ticketKey: string): Promise<TicketDiff> {
  return jsonFetch<TicketDiff>(`/api/tickets/${encodeURIComponent(ticketKey)}/diff`);
}

export async function approveRun(runId: number, note = ''): Promise<{ unblocked?: number }> {
  return jsonFetch<{ unblocked?: number }>(`/api/runs/${runId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ text: note }),
  });
}

export async function rejectRun(runId: number, reason = ''): Promise<void> {
  await jsonFetch(`/api/runs/${runId}/reject`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  });
}

export async function noteRun(runId: number, text: string): Promise<void> {
  await jsonFetch(`/api/runs/${runId}/note`, {
    method: 'POST',
    body: JSON.stringify({ text }),
  });
}

export async function stopRun(runId: number): Promise<void> {
  await jsonFetch(`/api/runs/${runId}/stop`, { method: 'POST' });
}

export async function kickoffRun(runId: number): Promise<void> {
  await jsonFetch(`/api/runs/${runId}/kickoff`, { method: 'POST' });
}

export async function blockRun(runId: number, blockerTicket: string, reason: string): Promise<void> {
  await jsonFetch(`/api/runs/${runId}/block`, {
    method: 'POST',
    body: JSON.stringify({ blocker_ticket: blockerTicket, reason }),
  });
}

export async function unblockRun(runId: number): Promise<void> {
  await jsonFetch(`/api/runs/${runId}/unblock`, { method: 'POST' });
}

// ─── SSE ──────────────────────────────────────────────────────────

export interface SseEvent {
  type: string;
  ts: number;
  data?: Record<string, unknown>;
}

export type SseHandler = (event: SseEvent) => void;

/**
 * Subscribe to the sidecar's event stream. Returns a cleanup function.
 *
 * Auto-reconnects on transient errors with a 1s backoff. The browser's
 * EventSource handles initial connection + parses `data:` frames for us.
 */
export function subscribeToStream(onEvent: SseHandler): () => void {
  let closed = false;
  let es: EventSource | null = null;

  function connect() {
    if (closed) return;
    es = new EventSource(`${API_BASE}/api/stream`);
    es.onmessage = (msg) => {
      try {
        const parsed = JSON.parse(msg.data) as SseEvent;
        onEvent(parsed);
      } catch {
        // Malformed payload — skip.
      }
    };
    es.onerror = () => {
      es?.close();
      es = null;
      if (!closed) setTimeout(connect, 1000);
    };
  }

  connect();
  return () => {
    closed = true;
    es?.close();
  };
}
