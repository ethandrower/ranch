# Channel Connectors Spec
## Building Slack Bot + Outlook Inbox Connectors for Virtual Employee Architecture

**Purpose:** Deep technical reference for Claude Code to build reusable, production-grade Slack and Outlook channel connectors. Derived from OpenClaw's MIT-licensed source at github.com/openclaw/openclaw.

**Target architecture:** Virtual employees that have "channels" — both reactive (respond to inbound messages) and proactive (reach out based on events, timeouts, or workflow state).

---

## 1. Core Abstractions — Design First

### The Channel Interface

Every connector should implement this interface. Keep it minimal and symmetric — inbound and outbound are separate concerns.

```typescript
// types/channel.ts

export interface ChannelMessage {
  id: string;                    // Platform-native message ID
  conversationId: string;        // Thread/channel/conversation ID
  threadId?: string;             // Parent thread ID (if reply)
  from: ChannelIdentity;
  to?: ChannelIdentity;
  content: string;               // Normalized plain text
  contentHtml?: string;          // Raw HTML if available
  attachments?: ChannelAttachment[];
  timestamp: Date;
  metadata?: Record<string, unknown>; // Platform-specific raw payload
}

export interface ChannelIdentity {
  id: string;                    // Platform user/channel ID
  name?: string;
  email?: string;
  isBot?: boolean;
}

export interface ChannelAttachment {
  id: string;
  name: string;
  contentType: string;
  url?: string;
  content?: Buffer;
  size?: number;
}

export interface OutboundMessage {
  to: string;                    // Channel/user/thread target
  content: string;
  contentHtml?: string;
  threadId?: string;             // Reply to thread
  attachments?: OutboundAttachment[];
  metadata?: Record<string, unknown>;
}

export interface OutboundAttachment {
  name: string;
  content: Buffer;
  contentType: string;
}

export interface SendResult {
  messageId: string;
  conversationId: string;
  threadId?: string;
  timestamp: Date;
}

// The core connector interface every channel must implement
export interface ChannelConnector {
  readonly id: string;           // 'slack' | 'outlook' | etc.
  readonly displayName: string;

  // Lifecycle
  start(opts: ChannelStartOpts): Promise<void>;
  stop(): Promise<void>;
  isRunning(): boolean;

  // Outbound (proactive + reactive)
  send(message: OutboundMessage): Promise<SendResult>;

  // Inbound — register handlers before start()
  onMessage(handler: MessageHandler): void;
  onError(handler: ErrorHandler): void;

  // Optional: proactive capabilities
  listConversations?(): Promise<ChannelConversation[]>;
  search?(query: string): Promise<ChannelMessage[]>;
  getHistory?(conversationId: string, opts?: HistoryOpts): Promise<ChannelMessage[]>;
}

export type MessageHandler = (message: ChannelMessage) => Promise<void>;
export type ErrorHandler = (error: Error, context?: unknown) => void;

export interface ChannelStartOpts {
  credentials: Record<string, string>;
  config?: Record<string, unknown>;
}

export interface ChannelConversation {
  id: string;
  name?: string;
  kind: 'dm' | 'group' | 'channel' | 'email-thread';
  participants?: ChannelIdentity[];
  lastActivity?: Date;
}

export interface HistoryOpts {
  limit?: number;
  before?: Date;
  after?: Date;
  threadId?: string;
}
```

### Token Store (shared by all connectors)

OC stores tokens via `normalizeResolvedSecretInputString` + file-backed secure storage. For your architecture:

```typescript
// token-store.ts
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';

export interface TokenSet {
  accessToken: string;
  refreshToken?: string;
  expiresAt?: number;    // unix ms
  scope?: string[];
  raw?: Record<string, unknown>;
}

export interface TokenStore {
  get(key: string): Promise<TokenSet | null>;
  set(key: string, tokens: TokenSet): Promise<void>;
  clear(key: string): Promise<void>;
}

// File-backed implementation (suitable for single-user agent)
export class FileTokenStore implements TokenStore {
  private dir: string;

  constructor(storeDir: string) {
    this.dir = storeDir;
    mkdirSync(storeDir, { recursive: true });
  }

  private keyPath(key: string): string {
    const hash = createHash('sha256').update(key).digest('hex').slice(0, 16);
    return join(this.dir, `token-${hash}.json`);
  }

  async get(key: string): Promise<TokenSet | null> {
    try {
      const raw = readFileSync(this.keyPath(key), 'utf8');
      return JSON.parse(raw) as TokenSet;
    } catch {
      return null;
    }
  }

  async set(key: string, tokens: TokenSet): Promise<void> {
    writeFileSync(this.keyPath(key), JSON.stringify(tokens, null, 2), { mode: 0o600 });
  }

  async clear(key: string): Promise<void> {
    try {
      const { unlinkSync } = await import('node:fs');
      unlinkSync(this.keyPath(key));
    } catch {
      // noop
    }
  }
}

// Check if token needs refresh (5-min buffer)
export function isTokenExpired(tokens: TokenSet, bufferMs = 5 * 60_000): boolean {
  if (!tokens.expiresAt) return false;
  return Date.now() + bufferMs >= tokens.expiresAt;
}
```

### Reconnect/Backoff (from OC's reconnect-policy.ts)

OC uses this exact backoff config for Slack Socket Mode. Extract it as a shared utility:

```typescript
// utils/backoff.ts

export interface BackoffPolicy {
  initialMs: number;
  maxMs: number;
  factor: number;
  jitter: number;       // 0–1, fraction of computed delay to randomly add/subtract
  maxAttempts: number;
}

// OC's exact Slack policy — battle-tested
export const DEFAULT_BACKOFF: BackoffPolicy = {
  initialMs: 2_000,
  maxMs: 30_000,
  factor: 1.8,
  jitter: 0.25,
  maxAttempts: 12,
};

export function computeBackoff(policy: BackoffPolicy, attempt: number): number {
  const base = Math.min(policy.initialMs * Math.pow(policy.factor, attempt), policy.maxMs);
  const jitter = base * policy.jitter * (Math.random() * 2 - 1);
  return Math.max(0, Math.round(base + jitter));
}

export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new Error('Aborted'));
    }, { once: true });
  });
}

export async function withRetry<T>(
  policy: BackoffPolicy,
  fn: (attempt: number) => Promise<T>,
  isRetryable: (err: unknown) => boolean = () => true,
  signal?: AbortSignal,
): Promise<T> {
  let lastError: unknown;
  for (let attempt = 0; attempt <= policy.maxAttempts; attempt++) {
    try {
      return await fn(attempt);
    } catch (err) {
      lastError = err;
      if (attempt >= policy.maxAttempts || !isRetryable(err)) {
        throw err;
      }
      const delayMs = computeBackoff(policy, attempt);
      await sleep(delayMs, signal);
    }
  }
  throw lastError;
}
```

### Send Queue (from OC's Slack send queuing)

OC serializes Slack sends per conversation to avoid race conditions. This is critical for thread replies:

```typescript
// utils/send-queue.ts

const queues = new Map<string, Promise<void>>();

export async function runQueued<T>(key: string, task: () => Promise<T>): Promise<T> {
  const previous = queues.get(key) ?? Promise.resolve();
  let release!: () => void;
  const slot = new Promise<void>((resolve) => { release = resolve; });
  const queued = previous.catch(() => undefined).then(() => slot);
  queues.set(key, queued);
  await previous.catch(() => undefined);
  try {
    return await task();
  } finally {
    release();
    if (queues.get(key) === queued) {
      queues.delete(key);
    }
  }
}
```

### Text Chunking (from OC reply-chunking)

Both Slack and Outlook need text chunked to fit platform limits:

```typescript
// utils/chunking.ts

// Slack hard limit
export const SLACK_TEXT_LIMIT = 3000;
// Outlook body can be large but proactive messages should be concise
export const OUTLOOK_PREVIEW_LIMIT = 500;

export function chunkText(text: string, maxLen: number): string[] {
  if (text.length <= maxLen) return [text];

  const chunks: string[] = [];
  const paragraphs = text.split(/\n\n+/);
  let current = '';

  for (const para of paragraphs) {
    if (!current) {
      current = para;
      continue;
    }
    const next = `${current}\n\n${para}`;
    if (next.length <= maxLen) {
      current = next;
    } else {
      if (current) chunks.push(current);
      // If single paragraph exceeds limit, hard-split it
      if (para.length > maxLen) {
        for (let i = 0; i < para.length; i += maxLen) {
          chunks.push(para.slice(i, i + maxLen));
        }
        current = '';
      } else {
        current = para;
      }
    }
  }
  if (current) chunks.push(current);
  return chunks.length ? chunks : [''];
}
```

---

## 2. Slack Connector

### How OC Does It

OC's Slack connector uses:
- **`@slack/bolt`** for Socket Mode (persistent WebSocket, no public webhook needed)
- **`@slack/web-api`** `WebClient` for sending
- Socket Mode = app connects OUT to Slack, no inbound HTTP required — perfect for agents behind NAT/VPN

The monitor lifecycle: `startSlackSocketAndWaitForDisconnect` → disconnect event → exponential backoff → reconnect loop.

### Required Slack App Scopes

From OC's `scopes.ts`:

**Bot Token Scopes (minimum for agent):**
```
channels:history     # Read messages in public channels
channels:read        # List channels
chat:write           # Post messages
chat:write.customize # Post as custom name/avatar (nice to have)
files:read           # Download file attachments
files:write          # Upload files
groups:history       # Read private channel messages
groups:read          # List private channels
im:history           # Read DMs
im:read              # List DMs
im:write             # Open DM conversations
mpim:history         # Read group DMs
reactions:read       # Read reactions
reactions:write      # Add reactions
users:read           # Resolve user IDs to profiles
users:read.email     # Look up users by email
```

**App-Level Token Scope (for Socket Mode):**
```
connections:write    # Required for Socket Mode
```

### Implementation

```typescript
// connectors/slack/index.ts
import { App, type MessageEvent } from '@slack/bolt';
import { WebClient } from '@slack/web-api';
import type {
  ChannelConnector, ChannelMessage, ChannelIdentity,
  MessageHandler, ErrorHandler, OutboundMessage, SendResult,
  ChannelStartOpts, ChannelConversation,
} from '../../types/channel.js';
import { runQueued } from '../../utils/send-queue.js';
import { withRetry, DEFAULT_BACKOFF, sleep } from '../../utils/backoff.js';
import { chunkText, SLACK_TEXT_LIMIT } from '../../utils/chunking.js';

// ── DM channel cache (user ID → channel ID) ───────────────────────────────
const dmCache = new Map<string, string>();
const DM_CACHE_MAX = 1024;

function cacheDm(userId: string, channelId: string) {
  if (dmCache.size >= DM_CACHE_MAX) {
    const oldest = dmCache.keys().next().value!;
    dmCache.delete(oldest);
  }
  dmCache.set(userId, channelId);
}

// ── WebClient cache (keyed by token hash) ─────────────────────────────────
import { createHash } from 'node:crypto';
const clientCache = new Map<string, WebClient>();

function getClient(token: string): WebClient {
  const key = createHash('sha256').update(token).digest('base64url').slice(0, 16);
  if (!clientCache.has(key)) {
    clientCache.set(key, new WebClient(token, {
      retryConfig: { retries: 3 },
      rejectRateLimitedCalls: false,
    }));
  }
  return clientCache.get(key)!;
}

// ── DNS retry (from OC's exact implementation) ────────────────────────────
const DNS_ERROR_RE = /EAI_AGAIN|ENOTFOUND|UND_ERR_DNS_RESOLVE_FAILED/i;
const DNS_RETRY_ATTEMPTS = 2;

async function withDnsRetry<T>(label: string, fn: () => Promise<T>): Promise<T> {
  for (let attempt = 0; attempt <= DNS_RETRY_ATTEMPTS; attempt++) {
    try {
      return await fn();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      const isDns = DNS_ERROR_RE.test(msg);
      if (attempt >= DNS_RETRY_ATTEMPTS || !isDns) throw err;
      console.warn(`[slack] DNS retry ${attempt + 1}/${DNS_RETRY_ATTEMPTS} for ${label}`);
      await sleep(250 * (attempt + 1));
    }
  }
  throw new Error('unreachable');
}

// ── Markdown → Slack mrkdwn (simplified, OC has full IR pipeline) ─────────
function markdownToMrkdwn(text: string): string {
  return text
    // Bold: **text** or __text__ → *text*
    .replace(/\*\*(.+?)\*\*/g, '*$1*')
    .replace(/__(.+?)__/g, '*$1*')
    // Italic: *text* or _text_ → _text_
    .replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '_$1_')
    // Code block
    .replace(/```([\s\S]+?)```/g, '```$1```')
    // Inline code
    .replace(/`(.+?)`/g, '`$1`')
    // Escape HTML entities (must come before link handling)
    .replace(/&(?!amp;|lt;|gt;)/g, '&amp;')
    // Links: [text](url) → <url|text>
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<$2|$1>');
}

// ── Main connector class ──────────────────────────────────────────────────
export class SlackConnector implements ChannelConnector {
  readonly id = 'slack';
  readonly displayName = 'Slack';

  private app?: App;
  private client?: WebClient;
  private botToken?: string;
  private botUserId?: string;
  private messageHandlers: MessageHandler[] = [];
  private errorHandlers: ErrorHandler[] = [];
  private running = false;
  private stopController?: AbortController;

  onMessage(handler: MessageHandler) { this.messageHandlers.push(handler); }
  onError(handler: ErrorHandler) { this.errorHandlers.push(handler); }
  isRunning() { return this.running; }

  async start(opts: ChannelStartOpts): Promise<void> {
    const botToken = opts.credentials.botToken;
    const appToken = opts.credentials.appToken; // xapp-... Socket Mode token
    if (!botToken || !appToken) {
      throw new Error('[slack] botToken and appToken required');
    }
    this.botToken = botToken;
    this.client = getClient(botToken);
    this.stopController = new AbortController();

    // Resolve bot user ID for self-message filtering
    const auth = await this.client.auth.test();
    this.botUserId = typeof auth.user_id === 'string' ? auth.user_id : undefined;

    // Build Bolt app in Socket Mode
    this.app = new App({
      token: botToken,
      appToken,
      socketMode: true,
      // Don't start Bolt's built-in server — we own the lifecycle
    });

    this.registerBoltHandlers();
    await this.runWithReconnect(opts);
  }

  private registerBoltHandlers() {
    if (!this.app) return;

    // Direct messages
    this.app.message(async ({ message, say }) => {
      await this.handleInbound(message as MessageEvent);
    });

    // App mentions in channels
    this.app.event('app_mention', async ({ event }) => {
      await this.handleInbound(event as unknown as MessageEvent);
    });

    // Errors
    this.app.error(async (err) => {
      for (const h of this.errorHandlers) {
        h(err instanceof Error ? err : new Error(String(err)));
      }
    });
  }

  private async handleInbound(event: MessageEvent): Promise<void> {
    // Filter bot's own messages
    if ('bot_id' in event || event.user === this.botUserId) return;
    // Filter system subtypes (joins, leaves, etc.)
    if ('subtype' in event && event.subtype && event.subtype !== 'thread_broadcast') return;

    const message = this.normalizeInbound(event);
    for (const handler of this.messageHandlers) {
      try {
        await handler(message);
      } catch (err) {
        for (const h of this.errorHandlers) {
          h(err instanceof Error ? err : new Error(String(err)), { event });
        }
      }
    }
  }

  private normalizeInbound(event: MessageEvent): ChannelMessage {
    const from: ChannelIdentity = {
      id: ('user' in event && typeof event.user === 'string') ? event.user : 'unknown',
    };
    const channel = typeof event.channel === 'string' ? event.channel : '';
    const ts = typeof event.ts === 'string' ? event.ts : '';
    const threadTs = ('thread_ts' in event && typeof event.thread_ts === 'string')
      ? event.thread_ts : undefined;

    return {
      id: ts,
      conversationId: channel,
      threadId: threadTs,
      from,
      content: typeof event.text === 'string' ? event.text : '',
      timestamp: new Date(parseFloat(ts) * 1000),
      metadata: event as unknown as Record<string, unknown>,
    };
  }

  private async runWithReconnect(opts: ChannelStartOpts): Promise<void> {
    const signal = this.stopController!.signal;
    let attempt = 0;

    this.running = true;
    try {
      while (!signal.aborted) {
        try {
          await this.app!.start();
          console.log('[slack] connected');
          attempt = 0;

          // Wait for disconnect
          await new Promise<void>((resolve) => {
            signal.addEventListener('abort', () => resolve(), { once: true });
          });

          if (!signal.aborted) {
            await this.app!.stop();
          }
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          // Non-recoverable auth errors — stop permanently
          if (/account_inactive|invalid_auth|token_revoked|token_expired|not_authed/i.test(msg)) {
            for (const h of this.errorHandlers) {
              h(new Error(`[slack] fatal auth error, stopping: ${msg}`));
            }
            break;
          }
          if (signal.aborted) break;

          attempt++;
          if (attempt > DEFAULT_BACKOFF.maxAttempts) {
            for (const h of this.errorHandlers) {
              h(new Error(`[slack] max reconnect attempts exceeded`));
            }
            break;
          }

          const delayMs = Math.min(
            DEFAULT_BACKOFF.initialMs * Math.pow(DEFAULT_BACKOFF.factor, attempt),
            DEFAULT_BACKOFF.maxMs,
          );
          console.warn(`[slack] disconnected, reconnecting in ${delayMs}ms (attempt ${attempt})`);
          await sleep(delayMs, signal).catch(() => {});
        }
      }
    } finally {
      this.running = false;
    }
  }

  async stop(): Promise<void> {
    this.stopController?.abort();
    if (this.app) {
      try { await this.app.stop(); } catch { /* noop */ }
    }
    this.running = false;
  }

  // ── Outbound ─────────────────────────────────────────────────────────────

  async send(msg: OutboundMessage): Promise<SendResult> {
    if (!this.client || !this.botToken) {
      throw new Error('[slack] not started');
    }

    // Resolve DM channel ID if target is a user
    const channelId = await this.resolveChannelId(msg.to);
    const queueKey = `${channelId}:${msg.threadId ?? ''}`;

    return runQueued(queueKey, async () => {
      const chunks = chunkText(markdownToMrkdwn(msg.content), SLACK_TEXT_LIMIT);
      let lastTs = '';
      let lastChannel = channelId;

      for (const [i, chunk] of chunks.entries()) {
        const response = await withDnsRetry('chat.postMessage', () =>
          this.client!.chat.postMessage({
            channel: channelId,
            text: chunk,
            ...(msg.threadId ? { thread_ts: msg.threadId } : {}),
            unfurl_links: false,
          })
        );
        lastTs = typeof response.ts === 'string' ? response.ts : lastTs;
        lastChannel = typeof response.channel === 'string' ? response.channel : lastChannel;
      }

      return {
        messageId: lastTs,
        conversationId: lastChannel,
        threadId: msg.threadId,
        timestamp: new Date(parseFloat(lastTs) * 1000),
      };
    });
  }

  private async resolveChannelId(to: string): Promise<string> {
    // Already a channel ID (C...) or thread target
    if (/^[CGD][A-Z0-9]+$/i.test(to)) return to;

    // User ID (U...)
    if (/^[UW][A-Z0-9]+$/i.test(to)) {
      const cached = dmCache.get(to);
      if (cached) return cached;
      const resp = await withDnsRetry('conversations.open', () =>
        this.client!.conversations.open({ users: to })
      );
      const channelId = resp.channel?.id;
      if (!channelId) throw new Error(`[slack] failed to open DM with ${to}`);
      cacheDm(to, channelId);
      return channelId;
    }

    return to;
  }

  // ── Proactive: find user by email ─────────────────────────────────────────

  async findUserByEmail(email: string): Promise<ChannelIdentity | null> {
    if (!this.client) throw new Error('[slack] not started');
    try {
      const resp = await this.client.users.lookupByEmail({ email });
      if (!resp.user) return null;
      return {
        id: resp.user.id ?? '',
        name: resp.user.real_name ?? resp.user.name,
        email,
        isBot: resp.user.is_bot,
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (/users_not_found/i.test(msg)) return null;
      throw err;
    }
  }

  async listConversations(): Promise<ChannelConversation[]> {
    if (!this.client) throw new Error('[slack] not started');
    const result: ChannelConversation[] = [];
    let cursor: string | undefined;
    do {
      const resp = await this.client.conversations.list({
        types: 'public_channel,private_channel,mpim,im',
        limit: 200,
        cursor,
      });
      for (const ch of resp.channels ?? []) {
        result.push({
          id: ch.id ?? '',
          name: ch.name,
          kind: ch.is_im ? 'dm' : ch.is_mpim ? 'group' : 'channel',
        });
      }
      cursor = resp.response_metadata?.next_cursor;
    } while (cursor);
    return result;
  }

  async getHistory(
    conversationId: string,
    opts: { limit?: number; before?: Date; threadId?: string } = {}
  ): Promise<ChannelMessage[]> {
    if (!this.client) throw new Error('[slack] not started');

    const params = {
      channel: conversationId,
      limit: opts.limit ?? 50,
      ...(opts.before ? { latest: String(opts.before.getTime() / 1000) } : {}),
    };

    const resp = opts.threadId
      ? await this.client.conversations.replies({ ...params, ts: opts.threadId })
      : await this.client.conversations.history(params);

    return (resp.messages ?? [])
      .filter(m => m.type === 'message' && m.user !== this.botUserId)
      .map(m => ({
        id: m.ts ?? '',
        conversationId,
        threadId: m.thread_ts,
        from: { id: m.user ?? '' },
        content: m.text ?? '',
        timestamp: new Date(parseFloat(m.ts ?? '0') * 1000),
        metadata: m as unknown as Record<string, unknown>,
      }));
  }

  // ── Reactions ─────────────────────────────────────────────────────────────

  async addReaction(channelId: string, ts: string, emoji: string): Promise<void> {
    if (!this.client) throw new Error('[slack] not started');
    await this.client.reactions.add({ channel: channelId, timestamp: ts, name: emoji });
  }
}
```

### Slack Usage Example

```typescript
// examples/slack-agent-channel.ts
import { SlackConnector } from '../connectors/slack/index.js';

const slack = new SlackConnector();

slack.onMessage(async (msg) => {
  // Respond to direct mentions or DMs
  await slack.send({
    to: msg.conversationId,
    content: `Got your message: "${msg.content}"`,
    threadId: msg.threadId ?? msg.id, // reply in thread
  });
});

await slack.start({
  credentials: {
    botToken: process.env.SLACK_BOT_TOKEN!,
    appToken: process.env.SLACK_APP_TOKEN!,
  },
});

// Proactive: send to a user by email
const user = await slack.findUserByEmail('jane@acme.com');
if (user) {
  await slack.send({
    to: user.id,
    content: 'Hey Jane, following up on the proposal — do you have 15 min this week?',
  });
}
```

---

## 3. Outlook / Microsoft Graph Connector

### How OC Does It

OC's MS Teams connector uses:
- **Bot Framework** for receiving messages (bot webhook endpoint)
- **Microsoft Graph API** for reading/sending emails, channel messages, files
- **App-only auth** (client credentials) + optional **delegated auth** (OAuth on behalf of a user)

For Outlook inbox specifically you need **delegated auth** — reading someone's inbox requires their consent. App-only can read mail if the admin grants `Mail.Read` to the app, but it's usually restricted.

**Key Graph endpoints for email:**
```
GET  /me/messages                         # Inbox messages
GET  /me/mailFolders/inbox/messages       # Explicit inbox
GET  /me/messages/{id}                    # Single message
POST /me/sendMail                         # Send email
POST /me/messages/{id}/reply              # Reply to thread
POST /me/messages/{id}/forward            # Forward
PATCH /me/messages/{id}                   # Mark read, move, etc.

# Subscriptions (push notifications)
POST /subscriptions                       # Create webhook subscription
PATCH /subscriptions/{id}                 # Renew (max 3 days for mail)
DELETE /subscriptions/{id}                # Remove
```

### Auth: MSAL + Token Refresh (from OC's token.ts pattern)

```typescript
// connectors/outlook/auth.ts
import { ConfidentialClientApplication, type AuthenticationResult } from '@azure/msal-node';
import { isTokenExpired, type TokenSet, type TokenStore } from '../../token-store.js';

export interface OutlookAuthConfig {
  tenantId: string;
  clientId: string;
  clientSecret: string;
  // For delegated (user) auth — OAuth redirect
  redirectUri?: string;
}

// Scopes required for inbox read/send
export const OUTLOOK_MAIL_SCOPES = [
  'https://graph.microsoft.com/Mail.Read',
  'https://graph.microsoft.com/Mail.Send',
  'https://graph.microsoft.com/Mail.ReadWrite',
  'offline_access',
];

export class OutlookAuth {
  private msal: ConfidentialClientApplication;
  private tokenStore: TokenStore;
  private config: OutlookAuthConfig;
  private tokenKey: string;

  constructor(config: OutlookAuthConfig, tokenStore: TokenStore) {
    this.config = config;
    this.tokenStore = tokenStore;
    this.tokenKey = `outlook:${config.tenantId}:${config.clientId}`;

    this.msal = new ConfidentialClientApplication({
      auth: {
        clientId: config.clientId,
        clientSecret: config.clientSecret,
        authority: `https://login.microsoftonline.com/${config.tenantId}`,
      },
    });
  }

  // Get a valid access token, refreshing if needed
  async getAccessToken(): Promise<string> {
    const stored = await this.tokenStore.get(this.tokenKey);

    if (stored && !isTokenExpired(stored)) {
      return stored.accessToken;
    }

    if (stored?.refreshToken) {
      try {
        const result = await this.msal.acquireTokenByRefreshToken({
          refreshToken: stored.refreshToken,
          scopes: OUTLOOK_MAIL_SCOPES,
        });
        if (result) {
          await this.storeResult(result);
          return result.accessToken;
        }
      } catch (err) {
        console.warn('[outlook] refresh token failed, need re-auth:', err);
      }
    }

    throw new Error(
      '[outlook] No valid token. Run the OAuth flow first. ' +
      'Use OutlookAuth.getAuthUrl() and OutlookAuth.handleCallback().'
    );
  }

  // Step 1: Get auth URL to send to user
  async getAuthUrl(): Promise<string> {
    const result = await this.msal.getAuthCodeUrl({
      scopes: OUTLOOK_MAIL_SCOPES,
      redirectUri: this.config.redirectUri ?? 'http://localhost:3000/auth/callback',
    });
    return result;
  }

  // Step 2: Exchange code for tokens
  async handleCallback(code: string): Promise<void> {
    const result = await this.msal.acquireTokenByCode({
      code,
      scopes: OUTLOOK_MAIL_SCOPES,
      redirectUri: this.config.redirectUri ?? 'http://localhost:3000/auth/callback',
    });
    if (!result) throw new Error('[outlook] token exchange failed');
    await this.storeResult(result);
  }

  private async storeResult(result: AuthenticationResult): Promise<void> {
    await this.tokenStore.set(this.tokenKey, {
      accessToken: result.accessToken,
      refreshToken: result.refreshToken ?? undefined,
      expiresAt: result.expiresOn ? result.expiresOn.getTime() : undefined,
      scope: OUTLOOK_MAIL_SCOPES,
    });
  }
}
```

### Graph API Client (from OC's graph.ts pattern)

```typescript
// connectors/outlook/graph.ts

const GRAPH_ROOT = 'https://graph.microsoft.com/v1.0';
const NULL_STATUS = new Set([204, 205, 304]);

export class GraphClient {
  constructor(private getToken: () => Promise<string>) {}

  async get<T>(path: string, params?: Record<string, string>): Promise<T> {
    const url = new URL(`${GRAPH_ROOT}${path}`);
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        url.searchParams.set(k, v);
      }
    }
    return this.request<T>(url.toString(), { method: 'GET' });
  }

  async post<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(`${GRAPH_ROOT}${path}`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  async patch<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(`${GRAPH_ROOT}${path}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  async delete(path: string): Promise<void> {
    await this.request<void>(`${GRAPH_ROOT}${path}`, { method: 'DELETE' });
  }

  private async request<T>(url: string, init: RequestInit): Promise<T> {
    const token = await this.getToken();
    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    };

    const resp = await fetch(url, { ...init, headers: { ...headers, ...init.headers as Record<string, string> } });

    if (!resp.ok) {
      const body = await resp.text().catch(() => '');
      throw new Error(`[graph] ${init.method} ${url} → ${resp.status}: ${body}`);
    }

    if (NULL_STATUS.has(resp.status)) return undefined as T;
    return resp.json() as Promise<T>;
  }

  // Paginate through all pages of a collection
  async *paginate<T>(path: string, params?: Record<string, string>): AsyncGenerator<T> {
    let url: string | undefined = undefined;
    let firstPage = true;

    while (true) {
      let page: { value?: T[]; '@odata.nextLink'?: string };
      if (firstPage) {
        page = await this.get<typeof page>(path, params);
        firstPage = false;
      } else if (url) {
        const token = await this.getToken();
        const resp = await fetch(url, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!resp.ok) break;
        page = await resp.json();
      } else {
        break;
      }

      for (const item of page.value ?? []) {
        yield item;
      }

      url = page['@odata.nextLink'];
      if (!url) break;
    }
  }
}
```

### Outlook Email Types

```typescript
// connectors/outlook/types.ts

export interface GraphEmail {
  id: string;
  conversationId?: string;
  conversationIndex?: string;
  subject?: string;
  bodyPreview?: string;
  body?: { content?: string; contentType?: 'html' | 'text' };
  from?: { emailAddress?: { address?: string; name?: string } };
  toRecipients?: Array<{ emailAddress?: { address?: string; name?: string } }>;
  ccRecipients?: Array<{ emailAddress?: { address?: string; name?: string } }>;
  receivedDateTime?: string;
  sentDateTime?: string;
  isRead?: boolean;
  isDraft?: boolean;
  importance?: 'low' | 'normal' | 'high';
  hasAttachments?: boolean;
  attachments?: GraphEmailAttachment[];
  webLink?: string;
  parentFolderId?: string;
}

export interface GraphEmailAttachment {
  id: string;
  name?: string;
  contentType?: string;
  size?: number;
  contentBytes?: string; // base64
}

export interface GraphSubscription {
  id: string;
  resource: string;
  expirationDateTime: string;
  notificationUrl: string;
  changeType: string;
  clientState?: string;
}
```

### Outlook Connector

```typescript
// connectors/outlook/index.ts
import Fastify from 'fastify';
import type {
  ChannelConnector, ChannelMessage, ChannelIdentity,
  MessageHandler, ErrorHandler, OutboundMessage, SendResult,
  ChannelStartOpts, ChannelConversation, HistoryOpts,
} from '../../types/channel.js';
import { withRetry, DEFAULT_BACKOFF, sleep } from '../../utils/backoff.js';
import { FileTokenStore } from '../../token-store.js';
import { OutlookAuth } from './auth.js';
import { GraphClient } from './graph.js';
import type { GraphEmail, GraphSubscription } from './types.js';

// Subscription renewal — Graph mail subscriptions expire after max 3 days
const SUBSCRIPTION_TTL_MS = 3 * 24 * 60 * 60_000;
const SUBSCRIPTION_RENEW_BEFORE_MS = 30 * 60_000; // renew 30 min before expiry

export class OutlookConnector implements ChannelConnector {
  readonly id = 'outlook';
  readonly displayName = 'Outlook / Microsoft 365 Mail';

  private auth?: OutlookAuth;
  private graph?: GraphClient;
  private messageHandlers: MessageHandler[] = [];
  private errorHandlers: ErrorHandler[] = [];
  private running = false;
  private subscription?: GraphSubscription;
  private renewTimer?: ReturnType<typeof setTimeout>;
  private server?: ReturnType<typeof Fastify>;
  private pollTimer?: ReturnType<typeof setInterval>;
  private lastSeenIds = new Set<string>();

  onMessage(handler: MessageHandler) { this.messageHandlers.push(handler); }
  onError(handler: ErrorHandler) { this.errorHandlers.push(handler); }
  isRunning() { return this.running; }

  async start(opts: ChannelStartOpts): Promise<void> {
    const { tenantId, clientId, clientSecret, redirectUri, storeDir } = opts.credentials;
    const tokenStore = new FileTokenStore(storeDir ?? './.tokens');

    this.auth = new OutlookAuth({ tenantId, clientId, clientSecret, redirectUri }, tokenStore);
    this.graph = new GraphClient(() => this.auth!.getAccessToken());

    // Try to subscribe (push) first, fall back to poll
    const webhookUrl = opts.config?.webhookUrl as string | undefined;
    if (webhookUrl) {
      await this.startWebhookMode(webhookUrl);
    } else {
      await this.startPollMode(opts.config?.pollIntervalMs as number ?? 60_000);
    }

    this.running = true;
  }

  // ── Mode 1: Graph Subscription (push) ────────────────────────────────────
  // Requires a public HTTPS endpoint. Best for production.

  private async startWebhookMode(webhookUrl: string): Promise<void> {
    await this.createSubscription(webhookUrl);
    await this.startWebhookServer(webhookUrl);
    this.scheduleSubscriptionRenewal();
  }

  private async createSubscription(notificationUrl: string): Promise<void> {
    const expiry = new Date(Date.now() + SUBSCRIPTION_TTL_MS).toISOString();
    this.subscription = await this.graph!.post<GraphSubscription>('/subscriptions', {
      changeType: 'created,updated',
      notificationUrl,
      resource: '/me/mailFolders/inbox/messages',
      expirationDateTime: expiry,
      clientState: 'agent-outlook-' + Math.random().toString(36).slice(2),
    });
    console.log(`[outlook] subscription created, expires ${expiry}`);
  }

  private async startWebhookServer(webhookUrl: string): Promise<void> {
    const url = new URL(webhookUrl);
    const port = parseInt(url.port || '443');
    this.server = Fastify({ logger: false });

    this.server.post('/notify', async (req, reply) => {
      // Graph sends a validation token on first subscription
      const validationToken = (req.query as Record<string, string>).validationToken;
      if (validationToken) {
        return reply.type('text/plain').send(validationToken);
      }

      // Process notification (minimal payload — need to fetch full message)
      const body = req.body as { value?: Array<{ resourceData?: { id?: string } }> };
      for (const notification of body.value ?? []) {
        const msgId = notification.resourceData?.id;
        if (msgId && !this.lastSeenIds.has(msgId)) {
          this.lastSeenIds.add(msgId);
          // Fetch full message and dispatch
          this.fetchAndDispatch(msgId).catch(err => {
            for (const h of this.errorHandlers) h(err);
          });
        }
      }
      reply.code(202).send();
    });

    await this.server.listen({ port, host: '0.0.0.0' });
    console.log(`[outlook] webhook server on port ${port}`);
  }

  private scheduleSubscriptionRenewal(): void {
    if (!this.subscription) return;
    const expiry = new Date(this.subscription.expirationDateTime).getTime();
    const renewAt = expiry - SUBSCRIPTION_RENEW_BEFORE_MS;
    const delay = Math.max(0, renewAt - Date.now());

    this.renewTimer = setTimeout(async () => {
      try {
        const newExpiry = new Date(Date.now() + SUBSCRIPTION_TTL_MS).toISOString();
        await this.graph!.patch(`/subscriptions/${this.subscription!.id}`, {
          expirationDateTime: newExpiry,
        });
        this.subscription!.expirationDateTime = newExpiry;
        console.log(`[outlook] subscription renewed, expires ${newExpiry}`);
        this.scheduleSubscriptionRenewal();
      } catch (err) {
        console.error('[outlook] subscription renewal failed:', err);
        // Try to re-create
        if (this.server) {
          const url = this.subscription?.resource ?? '';
          await this.createSubscription(url).catch(() => {});
        }
      }
    }, delay);
  }

  // ── Mode 2: Poll (no public endpoint needed — great for dev/local agents) ─

  private async startPollMode(intervalMs: number): Promise<void> {
    console.log(`[outlook] polling inbox every ${intervalMs / 1000}s`);
    // Initial poll to seed seen IDs without dispatching (avoid replaying old mail)
    await this.pollInbox(true).catch(() => {});
    this.pollTimer = setInterval(() => {
      this.pollInbox(false).catch(err => {
        for (const h of this.errorHandlers) h(err);
      });
    }, intervalMs);
  }

  private async pollInbox(seedOnly: boolean): Promise<void> {
    // Only fetch unread, newest first, last 20
    const messages: { value?: GraphEmail[] } = await this.graph!.get('/me/mailFolders/inbox/messages', {
      '$filter': 'isRead eq false',
      '$orderby': 'receivedDateTime desc',
      '$top': '20',
      '$select': 'id,subject,from,receivedDateTime,bodyPreview,conversationId,isRead',
    });

    for (const email of messages.value ?? []) {
      if (!email.id) continue;
      if (this.lastSeenIds.has(email.id)) continue;
      this.lastSeenIds.add(email.id);
      if (!seedOnly) {
        await this.fetchAndDispatch(email.id);
      }
    }

    // Prune seen IDs to avoid unbounded growth
    if (this.lastSeenIds.size > 10_000) {
      const arr = [...this.lastSeenIds];
      this.lastSeenIds = new Set(arr.slice(-5000));
    }
  }

  private async fetchAndDispatch(messageId: string): Promise<void> {
    try {
      const email = await this.graph!.get<GraphEmail>(`/me/messages/${messageId}`, {
        '$expand': 'attachments',
      });
      const message = this.normalizeEmail(email);
      for (const handler of this.messageHandlers) {
        await handler(message);
      }
    } catch (err) {
      for (const h of this.errorHandlers) {
        h(err instanceof Error ? err : new Error(String(err)));
      }
    }
  }

  private normalizeEmail(email: GraphEmail): ChannelMessage {
    const from: ChannelIdentity = {
      id: email.from?.emailAddress?.address ?? '',
      name: email.from?.emailAddress?.name,
      email: email.from?.emailAddress?.address,
    };

    return {
      id: email.id,
      conversationId: email.conversationId ?? email.id,
      from,
      content: email.bodyPreview ?? '',
      contentHtml: email.body?.content,
      timestamp: email.receivedDateTime ? new Date(email.receivedDateTime) : new Date(),
      metadata: email as unknown as Record<string, unknown>,
    };
  }

  async stop(): Promise<void> {
    this.running = false;
    if (this.renewTimer) clearTimeout(this.renewTimer);
    if (this.pollTimer) clearInterval(this.pollTimer);
    if (this.subscription && this.graph) {
      await this.graph.delete(`/subscriptions/${this.subscription.id}`).catch(() => {});
    }
    if (this.server) {
      await this.server.close();
    }
  }

  // ── Outbound ──────────────────────────────────────────────────────────────

  async send(msg: OutboundMessage): Promise<SendResult> {
    if (!this.graph) throw new Error('[outlook] not started');

    // If replying to a thread (conversationId or messageId given)
    if (msg.threadId) {
      return this.reply(msg.threadId, msg.content, msg.contentHtml);
    }

    // New email
    return this.sendNew(msg);
  }

  private async sendNew(msg: OutboundMessage): Promise<SendResult> {
    const [toAddress, ...ccAddresses] = Array.isArray(msg.to) ? msg.to : [msg.to];
    const subject = (msg.metadata?.subject as string) ?? 'Message from your agent';

    const payload = {
      message: {
        subject,
        body: {
          contentType: msg.contentHtml ? 'HTML' : 'Text',
          content: msg.contentHtml ?? msg.content,
        },
        toRecipients: [{ emailAddress: { address: toAddress } }],
        ...(ccAddresses.length ? {
          ccRecipients: ccAddresses.map(a => ({ emailAddress: { address: a } }))
        } : {}),
      },
      saveToSentItems: true,
    };

    await this.graph!.post('/me/sendMail', payload);

    return {
      messageId: `sent-${Date.now()}`,
      conversationId: `sent-${Date.now()}`,
      timestamp: new Date(),
    };
  }

  private async reply(messageId: string, text: string, html?: string): Promise<SendResult> {
    await this.graph!.post(`/me/messages/${messageId}/reply`, {
      message: {
        body: {
          contentType: html ? 'HTML' : 'Text',
          content: html ?? text,
        },
      },
    });

    return {
      messageId: `reply-${Date.now()}`,
      conversationId: messageId,
      timestamp: new Date(),
    };
  }

  // ── Proactive: read inbox ─────────────────────────────────────────────────

  async listConversations(): Promise<ChannelConversation[]> {
    if (!this.graph) throw new Error('[outlook] not started');
    const result: ChannelConversation[] = [];
    for await (const email of this.graph.paginate<GraphEmail>('/me/mailFolders/inbox/messages', {
      '$top': '50',
      '$select': 'id,subject,from,receivedDateTime,conversationId',
    })) {
      result.push({
        id: email.conversationId ?? email.id,
        name: email.subject,
        kind: 'email-thread',
        participants: email.from?.emailAddress?.address
          ? [{ id: email.from.emailAddress.address, email: email.from.emailAddress.address }]
          : [],
        lastActivity: email.receivedDateTime ? new Date(email.receivedDateTime) : undefined,
      });
    }
    return result;
  }

  async getHistory(conversationId: string, opts: HistoryOpts = {}): Promise<ChannelMessage[]> {
    if (!this.graph) throw new Error('[outlook] not started');
    const messages: GraphEmail[] = [];
    for await (const email of this.graph.paginate<GraphEmail>('/me/messages', {
      '$filter': `conversationId eq '${conversationId}'`,
      '$orderby': 'receivedDateTime asc',
      '$top': String(opts.limit ?? 50),
    })) {
      messages.push(email);
    }
    return messages.map(e => this.normalizeEmail(e));
  }

  async markRead(messageId: string): Promise<void> {
    await this.graph!.patch(`/me/messages/${messageId}`, { isRead: true });
  }

  // ── OAuth helpers for setup flow ──────────────────────────────────────────

  getAuthUrl(): Promise<string> {
    if (!this.auth) throw new Error('[outlook] not initialized');
    return this.auth.getAuthUrl();
  }

  handleOAuthCallback(code: string): Promise<void> {
    if (!this.auth) throw new Error('[outlook] not initialized');
    return this.auth.handleCallback(code);
  }
}
```

### Outlook Usage Example

```typescript
// examples/outlook-agent-channel.ts
import { OutlookConnector } from '../connectors/outlook/index.js';

const outlook = new OutlookConnector();

outlook.onMessage(async (msg) => {
  const email = msg.metadata as { from?: { emailAddress?: { address?: string } }; subject?: string };
  const sender = msg.from.email ?? 'unknown';
  const subject = email.subject ?? '(no subject)';

  console.log(`New email from ${sender}: ${subject}`);

  // Proactive: auto-reply or flag for agent processing
  // Reply in-thread:
  await outlook.send({
    to: sender,
    content: 'Thanks for your email — I\'ll get back to you shortly.',
    threadId: msg.id,
  });

  // Mark as read after processing
  await outlook.markRead(msg.id);
});

// Poll mode — no public URL needed
await outlook.start({
  credentials: {
    tenantId: process.env.AZURE_TENANT_ID!,
    clientId: process.env.AZURE_CLIENT_ID!,
    clientSecret: process.env.AZURE_CLIENT_SECRET!,
    storeDir: './.tokens',
  },
  config: {
    pollIntervalMs: 60_000,  // Check every minute
  },
});

// OAuth setup (one-time):
// const url = await outlook.getAuthUrl();
// console.log('Visit:', url);
// Then call: await outlook.handleOAuthCallback(code);
```

---

## 4. Channel Manager (orchestration layer)

This is the "available tools" registry for your virtual employees:

```typescript
// channel-manager.ts
import type { ChannelConnector, ChannelMessage, OutboundMessage, SendResult } from './types/channel.js';

export interface EmployeeChannels {
  // Register a connector
  register(connector: ChannelConnector): void;

  // Start all registered connectors
  startAll(credentialsByChannel: Record<string, Record<string, string>>): Promise<void>;

  // Unified send — auto-routes by connector ID
  send(channelId: string, message: OutboundMessage): Promise<SendResult>;

  // Broadcast: send same message across multiple channels
  broadcast(channelIds: string[], message: OutboundMessage): Promise<Record<string, SendResult | Error>>;

  // Get a connector by ID
  get(channelId: string): ChannelConnector | undefined;
}

export class ChannelManager implements EmployeeChannels {
  private connectors = new Map<string, ChannelConnector>();
  private globalMessageHandlers: Array<(channelId: string, msg: ChannelMessage) => Promise<void>> = [];

  register(connector: ChannelConnector): void {
    this.connectors.set(connector.id, connector);
    // Wire global handlers
    connector.onMessage(async (msg) => {
      for (const h of this.globalMessageHandlers) {
        await h(connector.id, msg);
      }
    });
  }

  // Subscribe to all inbound messages across all channels
  onAnyMessage(handler: (channelId: string, msg: ChannelMessage) => Promise<void>): void {
    this.globalMessageHandlers.push(handler);
  }

  async startAll(credentialsByChannel: Record<string, Record<string, string>>): Promise<void> {
    await Promise.allSettled(
      [...this.connectors.entries()].map(([id, connector]) => {
        const creds = credentialsByChannel[id];
        if (!creds) {
          console.warn(`[channel-manager] no credentials for ${id}, skipping`);
          return Promise.resolve();
        }
        return connector.start({ credentials: creds });
      })
    );
  }

  send(channelId: string, message: OutboundMessage): Promise<SendResult> {
    const connector = this.connectors.get(channelId);
    if (!connector) throw new Error(`[channel-manager] unknown channel: ${channelId}`);
    return connector.send(message);
  }

  async broadcast(
    channelIds: string[],
    message: OutboundMessage,
  ): Promise<Record<string, SendResult | Error>> {
    const results = await Promise.allSettled(
      channelIds.map(id => this.send(id, message))
    );
    return Object.fromEntries(
      channelIds.map((id, i) => [
        id,
        results[i]!.status === 'fulfilled'
          ? (results[i] as PromiseFulfilledResult<SendResult>).value
          : (results[i] as PromiseRejectedResult).reason,
      ])
    );
  }

  get(channelId: string): ChannelConnector | undefined {
    return this.connectors.get(channelId);
  }
}

// Virtual employee usage:
// const employee = new ChannelManager();
// employee.register(new SlackConnector());
// employee.register(new OutlookConnector());
//
// employee.onAnyMessage(async (channel, msg) => {
//   // All inbound messages from all channels flow here
//   // Your agent logic decides what to do
// });
//
// await employee.startAll({
//   slack: { botToken: '...', appToken: '...' },
//   outlook: { tenantId: '...', clientId: '...', clientSecret: '...' },
// });
//
// // Proactive outreach:
// await employee.send('slack', { to: 'U12345', content: 'Hey!' });
// await employee.send('outlook', { to: 'jane@acme.com', content: 'Following up...' });
```

---

## 5. Project Structure

```
your-repo/
├── src/
│   ├── types/
│   │   └── channel.ts           # Core interfaces
│   ├── utils/
│   │   ├── backoff.ts           # Retry/reconnect
│   │   ├── send-queue.ts        # Serialized sends per conversation
│   │   └── chunking.ts          # Text splitting
│   ├── token-store.ts           # Token persistence
│   ├── channel-manager.ts       # Orchestration
│   └── connectors/
│       ├── slack/
│       │   └── index.ts
│       └── outlook/
│           ├── auth.ts
│           ├── graph.ts
│           ├── types.ts
│           └── index.ts
├── examples/
│   ├── slack-agent-channel.ts
│   └── outlook-agent-channel.ts
├── package.json
└── tsconfig.json
```

**`package.json` deps:**
```json
{
  "dependencies": {
    "@slack/bolt": "^4.x",
    "@slack/web-api": "^7.x",
    "@azure/msal-node": "^2.x",
    "fastify": "^5.x"
  },
  "devDependencies": {
    "typescript": "^5.x",
    "@types/node": "^22.x"
  }
}
```

---

## 6. Key Lessons from OC Source

### Slack
1. **Socket Mode is the right choice for agents** — no inbound port/webhook to expose, persistent connection, auto-reconnects
2. **Always queue sends per conversation** — race conditions on thread replies are real (OC uses `runQueuedSlackSend` for exactly this)
3. **DNS errors are transient** — OC retries on `EAI_AGAIN`/`ENOTFOUND` with 2 attempts + 250ms delay; replicate this
4. **DM channel ID resolution matters** — user IDs (U-prefix) can't be used directly for file uploads; always call `conversations.open` and cache the resulting channel ID
5. **Filter self-messages** — check `bot_id` field and compare `event.user` to `auth.test()` result
6. **Text limit is 3000 chars per message** — chunk longer content across multiple messages in the same thread
7. **`chat:write.customize`** for custom username/avatar — OC gracefully falls back to plain `chat.postMessage` if scope is missing

### Outlook / Graph
1. **Delegated auth (user consent) is required for inbox** — app-only auth can read mail but only if tenant admin explicitly grants it; easier to do OAuth flow once and store the refresh token
2. **Graph subscriptions expire in 3 days max** — must renew them; OC schedules renewal 30 min before expiry
3. **Polling is fine for dev/local agents** — no public HTTPS endpoint needed; 60s poll interval is low impact
4. **`@odata.nextLink` pagination** — always follow it; Graph pages at 10–50 items by default
5. **OData escaping** — single quotes in filter values must be doubled: `'O''Brien'` (OC has `escapeOData()`)
6. **Mark messages as read** after processing — avoid re-processing on next poll
7. **MSAL handles token refresh** — but you need to persist the refresh token yourself; it's not stored in the MSAL cache between process restarts by default

### General Architecture
1. **Separate inbound from outbound concerns** — OC keeps `monitor.ts` (inbound) and `send.ts` (outbound) as distinct modules
2. **Cache WebClients / HTTP clients per token** — creating a new WebClient per request is expensive; OC keeps a 32-item LRU cache keyed by token hash
3. **Never expose raw tokens in logs** — OC hashes them before using as cache keys: `sha256:base64url`
4. **Normalize to canonical types immediately** — platform-specific types (Slack `MessageEvent`, Graph `Message`) should be normalized to your `ChannelMessage` type at the connector boundary; the rest of your code stays platform-agnostic
