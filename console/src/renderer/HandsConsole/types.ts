/**
 * View-model shapes — must match ranch/view/hands.py output exactly.
 * The HTTP sidecar serves these as JSON; the React UI consumes them.
 *
 * Do NOT add fields the backend doesn't emit. If you need a new field,
 * add it to the Python view-model first, then update this file.
 */

export type Stage =
  | 'triage'
  | 'scope'
  | 'plan'
  | 'code'
  | 'verify'
  | 'pre_push'
  | 'deploy'
  | 'pr_open'
  | 'review'
  | 'merge';

export interface NowBlock {
  line: string;
  meta: string;
}

export interface DiffEntry {
  file: string;
  ins: number;
  del: number;
}

export interface CommentPreview {
  author: string;
  body: string;
}

export interface Ticket {
  key: string;
  initiative: string | null;
  epic: string | null;
  stage: Stage;
  summary: string;
  goal: string;
  done: string[];
  now?: NowBlock;
  attention?: boolean;
  checkpoint?: string;
  blocked_by?: string;
  blocked_reason?: string;
  pr_id?: string;
  ci?: 'queued' | 'running' | 'passed' | 'failed' | 'stopped';
  diff?: DiffEntry[];
  decide_kind?: 'respond_to_review';
  comments_preview?: CommentPreview[];
  deploy_rec?: 'deploy' | 'no-deploy' | 'needs-review';
  deploy_reason?: string;
  next_checkpoint?: string;
  next_eta_seconds?: number | null;
  next_progress_pct?: number;
  hint?: string;
  adhoc?: boolean;
  run_id?: number;
  // Operator-kickoff flow: present when the hand has auto-discovered
  // this ticket via triage and queued it for the operator to kick off.
  queued?: boolean;
  triage_score?: number;
}

export interface RoutineState {
  state?: 'active' | 'idle';
  last_poll_seconds_ago?: number;
  next_in?: number;
  status?: 'ok' | 'warn';
  watching_count?: number;
  note?: string;
}

export interface EventLogEntry {
  icon: string;
  severity: 'good' | 'bad' | 'info' | 'warn';
  title: string;
  detail?: string;
  ago: string;
}

export interface HandView {
  label: string;
  status: 'running' | 'idle';
  initiatives: string[];
  default_initiative: string | null;
  initiative_labels: Record<string, string>;
  tickets: Ticket[];
  adhoc: Ticket[];
  events_log: EventLogEntry[];
  routines: Record<string, RoutineState>;
}

export interface HandSummary {
  name: string;
  label: string;
  status: 'running' | 'idle';
  attention_count: number;
  ticket_count: number;
  adhoc_count: number;
}
