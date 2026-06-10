/**
 * Bundled fixture matching the prototype's HANDS const, used in P3 so the
 * React UI can be built and verified end-to-end before the live wiring lands
 * in P4. Once the sidecar is connected this file is unused (kept around for
 * Storybook-style isolated component testing).
 */
import type { HandSummary, HandView } from './types';

export const INITIATIVE_LABELS: Record<string, string> = {
  'ref-mgmt': 'Reference Management',
  'scrapers': 'Scrapers infra',
  'perf': 'Performance',
  'misc': 'Misc & one-offs',
};

const max: HandView = {
  label: 'max',
  status: 'running',
  initiatives: ['ref-mgmt', 'misc'],
  default_initiative: 'ref-mgmt',
  initiative_labels: INITIATIVE_LABELS,
  events_log: [
    { icon: '⚡', severity: 'bad', title: 'CI failed on PR #1834 (ECD-1762)', detail: 'build went running → failed', ago: '30s' },
    { icon: '💬', severity: 'info', title: '2 new review comments on ECD-1762', detail: 'from vinod; auto-triage queued', ago: '4m' },
    { icon: '↪', severity: 'info', title: 'Triaged ECD-2073 → propose', detail: 'score 78, top of queue', ago: '12m' },
    { icon: '✓', severity: 'good', title: 'Approved propose ECD-2087 → exec', detail: 'operator approval received', ago: '18m' },
  ],
  routines: {
    jira_triage: { last_poll_seconds_ago: 12, next_in: 18, status: 'ok' },
    pr_comments: { last_poll_seconds_ago: 47, next_in: 73, status: 'ok', watching_count: 2 },
    ci_status:   { last_poll_seconds_ago: 23, next_in: 37, status: 'ok', watching_count: 2 },
    self_review: { state: 'idle' },
  },
  tickets: [
    {
      key: 'ECD-2105', initiative: 'ref-mgmt', epic: 'ECD-2100', stage: 'triage',
      summary: 'Improve search hit caching',
      goal: 'Reduce repeat search-hit DB hits on the lit-review index page.',
      done: [], hint: 'score 78 — in queue',
    },
    {
      key: 'ECD-2078', initiative: 'ref-mgmt', epic: 'ECD-2000', stage: 'triage',
      summary: 'Migrate old search endpoint to v3',
      goal: 'Sunset /api/search/v1 in favor of v3.',
      done: [], hint: 'blocked — waiting on ECD-2073',
      blocked_by: 'ECD-2073',
      blocked_reason: 'Migration shape depends on ECD-2073 plan_ready review.',
    },
    {
      key: 'ECD-2073', initiative: 'ref-mgmt', epic: 'ECD-2000', stage: 'plan',
      summary: 'Performance deploy — Celery worker scaling',
      goal: 'Improve throughput on hot Celery queues by isolating dispatch tasks.',
      done: ['Read epic + sister tickets', 'Pulled prod metrics', 'Drafted 5-step plan'],
      now: { line: 'Parked at plan_ready awaiting operator review.', meta: 'in state 12m · 9.2k tokens' },
      attention: true,
      checkpoint: 'plan_ready',
      hint: 'parked · 12m',
      deploy_rec: 'deploy',
      deploy_reason: 'needs release-phase validation in realistic Procfile',
    },
    {
      key: 'ECD-1880', initiative: 'ref-mgmt', epic: 'ECD-1850', stage: 'code',
      summary: 'Add audit log for migration runs',
      goal: 'Every migration run lands a row in an audit table.',
      done: ['Inspect existing migration hook', 'Failing test for audit writer'],
      now: { line: 'Implementing AuditLogWriter.', meta: 'in state 1m 30s · 11.2k tokens' },
      hint: '▸ 3 of 7 plan steps',
    },
    {
      key: 'ECD-2087', initiative: 'ref-mgmt', epic: 'ECD-2050', stage: 'pre_push',
      summary: 'Restore post-scrape dedup dispatch',
      goal: 'Restore the post-scrape dedup step that was dropped in 0317.',
      done: ['Read scrape dispatch flow', 'Added post-dedup dispatch', 'Pytest 4/4 + integration green'],
      now: { line: 'Acceptance 4/4 green. Diff is +88/-12 across 3 files.', meta: 'in state 2m 14s · 18.4k tokens' },
      attention: true, checkpoint: 'pre_push', hint: 'parked · 2m · deploy?',
      diff: [
        { file: 'lit_reviews/dedup/dispatch.py', ins: 64, del: 8 },
        { file: 'lit_reviews/tasks.py', ins: 22, del: 4 },
        { file: 'tests/test_dedup_dispatch.py', ins: 88, del: 0 },
      ],
      deploy_rec: 'deploy',
      deploy_reason: 'scraper integration check needs remote fleet',
    },
    {
      key: 'ECD-1762', initiative: 'ref-mgmt', epic: 'ECD-1700', stage: 'review',
      summary: 'Citation export — DOI formatting',
      goal: 'Format DOIs consistently across citation export.',
      done: ['Parsed reviewer expectations', 'Triaged 2 comments — both AGREE', "Applied vinod's regex nit"],
      now: { line: "Just pushed fix; CI turned red 30s ago.", meta: 'PR #1834 · CI failed · 2 unresolved' },
      attention: true, pr_id: '1834', ci: 'failed', hint: '2 new comments · 4m',
      decide_kind: 'respond_to_review',
      comments_preview: [
        { author: 'vinod', body: "Should this be Title Case before stripping? We had a bug where 'DOI: 10.x' became 'doi: 10.x'." },
      ],
    },
    {
      key: 'ECD-2055', initiative: 'ref-mgmt', epic: 'ECD-1850', stage: 'merge',
      summary: 'Unified DOI parser — landed last sprint',
      goal: 'Consolidate three DOI parsers.', done: ['Implementation', 'Acceptance 5/5', 'Merged'],
      hint: 'merged 2d ago',
    },
    {
      key: 'ECD-2099', initiative: 'misc', epic: null, stage: 'triage',
      summary: 'Add pagination headers to /api/articles',
      goal: 'Expose Link headers.', done: [], hint: 'score 62 · drive-by',
    },
  ],
  adhoc: [],
};

const jeffy: HandView = {
  label: 'jeffy', status: 'running',
  initiatives: ['ref-mgmt', 'scrapers', 'misc'],
  default_initiative: 'ref-mgmt',
  initiative_labels: INITIATIVE_LABELS,
  events_log: [
    { icon: '✓', severity: 'good', title: 'run_acceptance · 4/4 green', detail: 'about to park at pre_push', ago: '30s' },
  ],
  routines: {},
  tickets: [
    {
      key: 'ECD-2071', initiative: 'scrapers', epic: 'ECD-2060', stage: 'triage',
      summary: 'PubMed search — handle 429 backoff',
      goal: 'Handle 429s cleanly.', done: [], hint: 'score 70',
    },
    {
      key: 'ECD-1853', initiative: 'ref-mgmt', epic: 'ECD-1800', stage: 'verify',
      summary: 'Lit-reviews migration merge fix',
      goal: 'Fix merge conflict in lit_reviews migrations.',
      done: ['Reproduce', 'Patch migration tree'],
      now: { line: 'Running pytest + migration smoke.', meta: 'in state 45s' },
    },
  ],
  adhoc: [],
};

const arnold: HandView = {
  label: 'arnold', status: 'running',
  initiatives: ['scrapers', 'perf', 'misc'],
  default_initiative: 'scrapers',
  initiative_labels: INITIATIVE_LABELS,
  events_log: [
    { icon: '💬', severity: 'info', title: 'Review push-back from mohamed', detail: 'on ECD-1410', ago: '6m' },
  ],
  routines: {},
  tickets: [
    {
      key: 'ECD-1644', initiative: 'scrapers', epic: 'ECD-1600', stage: 'pre_push',
      summary: 'MAUDE adverse-event ingester rate limit',
      goal: 'Add exponential backoff.', done: ['Repro', 'Backoff impl', 'Acceptance 3/3 green'],
      now: { line: 'Acceptance green; diff small.', meta: 'in state 22m' },
      attention: true, checkpoint: 'pre_push', hint: 'parked · 22m',
      deploy_rec: 'no-deploy',
      deploy_reason: 'all acceptance is unit_test + script (localhost)',
    },
    {
      key: 'ECD-1410', initiative: 'perf', epic: 'ECD-1400', stage: 'review',
      summary: 'Worker spatial pre-parse — index rebuild',
      goal: 'Speed up spatial pre-parse.', done: ['Impl', 'Acceptance 4/4', 'PR opened'],
      now: { line: 'mohamed pushed back 6m ago.', meta: 'PR #1620 · 1 unresolved' },
      attention: true, pr_id: '1620', ci: 'passed', hint: '1 push-back · 6m',
      decide_kind: 'respond_to_review',
      comments_preview: [
        { author: 'mohamed', body: 'Why rebuild on every dispatch? Push back unless I am missing something.' },
      ],
    },
  ],
  adhoc: [],
};

const kesha: HandView = {
  label: 'kesha', status: 'idle',
  initiatives: ['scrapers', 'misc'],
  default_initiative: 'scrapers',
  initiative_labels: INITIATIVE_LABELS,
  events_log: [],
  routines: {},
  tickets: [
    {
      key: 'ECD-1410-kesha', initiative: 'scrapers', epic: 'ECD-1400', stage: 'review',
      summary: 'PubMed callback signing v2',
      goal: 'HMAC instead of static token.', done: ['Implementation', 'Acceptance 5/5', 'PR opened'],
      now: { line: 'PR open for 2 days — no review activity.', meta: 'PR #1580 · 8 files' },
      pr_id: '1580', ci: 'passed', hint: '2d · no activity',
    },
  ],
  adhoc: [],
};

export const FIXTURE_HANDS: Record<string, HandView> = { max, jeffy, arnold, kesha };

export const FIXTURE_HAND_SUMMARIES: HandSummary[] = Object.entries(FIXTURE_HANDS).map(([name, h]) => ({
  name,
  label: h.label,
  status: h.status,
  attention_count: h.tickets.filter((t) => t.attention).length,
  ticket_count: h.tickets.length,
  adhoc_count: h.adhoc.length,
}));
