import { useState, useMemo, useEffect } from 'react';
import s from './styles.module.css';
import { FIXTURE_HANDS, FIXTURE_HAND_SUMMARIES } from './fixture';
import type { HandSummary, HandView, Stage, Ticket } from './types';

const STAGES: Array<{ key: Stage; label: string; terminal?: boolean }> = [
  { key: 'triage',    label: 'Triage' },
  { key: 'scope',     label: 'Scope' },
  { key: 'plan',      label: 'Plan' },
  { key: 'code',      label: 'Code' },
  { key: 'verify',    label: 'Verify' },
  { key: 'pre_push',  label: 'Pre-push' },
  { key: 'deploy',    label: 'Deploy' },
  { key: 'pr_open',   label: 'PR open' },
  { key: 'review',    label: 'Review' },
  { key: 'merge',     label: 'Merge', terminal: true },
];

interface Props {
  /** Live mode uses HTTP fetch; fixture mode (P3 default) uses bundled data. */
  fetchHand?: (name: string) => Promise<HandView>;
  fetchHandSummaries?: () => Promise<HandSummary[]>;
}

export function HandsConsoleApp({ fetchHand, fetchHandSummaries }: Props = {}) {
  const [summaries, setSummaries] = useState<HandSummary[]>(FIXTURE_HAND_SUMMARIES);
  const [hands, setHands] = useState<Record<string, HandView>>(FIXTURE_HANDS);
  const [current, setCurrent] = useState<string>(FIXTURE_HAND_SUMMARIES[0]?.name ?? 'max');
  const [currentInitiative, setCurrentInitiative] = useState<Record<string, string>>({});
  const [drilledEpic, setDrilledEpic] = useState<Record<string, string | null>>({});
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [expandedStep, setExpandedStep] = useState<{ key: string; index: number } | null>(null);
  const [activityOpen, setActivityOpen] = useState(false);
  const [now, setNow] = useState(() => new Date());

  // Live-mode bootstrap. In P3 the fetchers are undefined → fixture wins.
  useEffect(() => {
    if (!fetchHandSummaries) return;
    fetchHandSummaries().then(setSummaries).catch(() => {});
  }, [fetchHandSummaries]);

  useEffect(() => {
    if (!fetchHand || !current) return;
    fetchHand(current).then((v) => setHands((h) => ({ ...h, [current]: v }))).catch(() => {});
  }, [fetchHand, current]);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(t);
  }, []);

  const hand = hands[current];
  if (!hand) return <div className={s.root}>No hand data.</div>;

  const initiative = currentInitiative[current] ?? hand.default_initiative ?? hand.initiatives[0] ?? null;
  const epic = drilledEpic[current] ?? null;

  const closePanel = () => {
    setSelectedKey(null);
    setExpandedStep(null);
  };

  const switchHand = (name: string) => {
    setCurrent(name);
    closePanel();
  };

  return (
    <div className={s.root}>
      <Topbar
        summaries={summaries}
        current={current}
        onSwitch={switchHand}
        activityOpen={activityOpen}
        toggleActivity={() => setActivityOpen((v) => !v)}
        hand={hand}
        now={now}
      />
      <ScopeBar
        hand={hand}
        active={initiative}
        epic={epic}
        onSwitch={(k) => {
          setCurrentInitiative({ ...currentInitiative, [current]: k });
          setDrilledEpic({ ...drilledEpic, [current]: null });
          closePanel();
        }}
        onClearEpic={() => setDrilledEpic({ ...drilledEpic, [current]: null })}
      />
      <Kanban
        hand={hand}
        initiative={initiative}
        epic={epic}
        selectedKey={selectedKey}
        onCardClick={(key) => {
          setSelectedKey(key);
          setExpandedStep(null);
        }}
        onEpicClick={(e) => setDrilledEpic({ ...drilledEpic, [current]: e })}
        onBlockedJump={(k) => setSelectedKey(k)}
      />
      <SidePanel
        ticket={findTicket(hand, selectedKey)}
        expandedStep={expandedStep}
        onClose={closePanel}
        onToggleStep={(key, index) => {
          setExpandedStep((prev) =>
            prev && prev.key === key && prev.index === index ? null : { key, index }
          );
        }}
      />
      <div className={s.footerHint}>
        Prototype v4 → React (P3 · fixture-driven). Live data wires in at P4.
      </div>
    </div>
  );
}

function findTicket(hand: HandView, key: string | null): Ticket | null {
  if (!key) return null;
  return [...hand.tickets, ...hand.adhoc].find((t) => t.key === key) ?? null;
}

// ─── Topbar ───────────────────────────────────────────────────────

function Topbar({
  summaries, current, onSwitch, activityOpen, toggleActivity, hand, now,
}: {
  summaries: HandSummary[];
  current: string;
  onSwitch: (name: string) => void;
  activityOpen: boolean;
  toggleActivity: () => void;
  hand: HandView;
  now: Date;
}) {
  return (
    <div className={s.topbar}>
      <div className={s.brand}>
        <span className={s.brandDot} />
        Ranch
      </div>
      <div className={s.tabs}>
        {summaries.map((sm) => (
          <div
            key={sm.name}
            className={`${s.tab} ${sm.name === current ? s.tabActive : ''}`}
            onClick={() => onSwitch(sm.name)}
          >
            <span className={`${s.handStatus} ${sm.status === 'idle' ? s.handStatusIdle : ''}`} />
            <span>{sm.label}</span>
            <span className={`${s.badge} ${sm.attention_count === 0 ? s.badgeZero : ''}`}>
              {sm.attention_count}
            </span>
          </div>
        ))}
      </div>
      <div className={s.topbarRight}>
        <button className={s.activityTrigger} onClick={toggleActivity}>
          <span>⟳</span><span>Activity</span>
        </button>
        <span className={s.meta}>
          {now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </span>
        <span className={s.meta}>● {summaries.length} hand{summaries.length !== 1 ? 's' : ''}</span>
        <ActivityPopout open={activityOpen} hand={hand} now={now} />
      </div>
    </div>
  );
}

// ─── ActivityPopout ──────────────────────────────────────────────

function ActivityPopout({ open, hand, now }: { open: boolean; hand: HandView; now: Date }) {
  return (
    <div className={`${s.activityPopout} ${open ? s.activityPopoutOpen : ''}`}>
      <div className={s.apHeader}>
        <span>⟳ Activity · {hand.label}</span>
        <span style={{ textTransform: 'none', letterSpacing: 0 }}>
          {now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
        </span>
      </div>
      <div className={s.apBody}>
        {hand.events_log.length === 0 ? (
          <div className={s.apEmpty}>No recent events.</div>
        ) : (
          hand.events_log.map((e, i) => (
            <div key={i} className={s.apEvent}>
              <span
                className={`${s.evIcon} ${
                  e.severity === 'good' ? s.evIconGood : e.severity === 'bad' ? s.evIconBad : ''
                }`}
              >
                {e.icon}
              </span>
              <div className={s.evBody}>
                {e.title}
                {e.detail && <div className={s.evDetail}>{e.detail}</div>}
              </div>
              <span className={s.evAgo}>{e.ago}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ─── ScopeBar ────────────────────────────────────────────────────

function ScopeBar({
  hand, active, epic, onSwitch, onClearEpic,
}: {
  hand: HandView; active: string | null; epic: string | null;
  onSwitch: (k: string) => void; onClearEpic: () => void;
}) {
  const all = [...hand.tickets, ...hand.adhoc];
  const stats: Record<string, { count: number; attention: number; blocked: number; merged: number }> = {};
  hand.initiatives.forEach((k) => (stats[k] = { count: 0, attention: 0, blocked: 0, merged: 0 }));
  all.forEach((t) => {
    const key = t.initiative ?? 'misc';
    if (!stats[key]) stats[key] = { count: 0, attention: 0, blocked: 0, merged: 0 };
    stats[key].count += 1;
    if (t.attention) stats[key].attention += 1;
    if (t.blocked_by) stats[key].blocked += 1;
    if (t.stage === 'merge') stats[key].merged += 1;
  });

  const totalBlocked = Object.values(stats).reduce((a, b) => a + b.blocked, 0);

  return (
    <div className={s.scopeBar}>
      <span className={s.scopeLabel}>Initiative</span>
      {hand.initiatives.map((k) => {
        const stat = stats[k] ?? { count: 0, attention: 0, blocked: 0, merged: 0 };
        const pct = stat.count === 0 ? 0 : Math.round((stat.merged / stat.count) * 100);
        return (
          <button
            key={k}
            className={`${s.scopePill} ${active === k ? s.scopePillActive : ''}`}
            onClick={() => onSwitch(k)}
            title={`${stat.merged}/${stat.count} merged · ${stat.attention} attention${stat.blocked ? ` · ${stat.blocked} blocked` : ''}`}
          >
            <span className={s.pillProgress} style={{ width: `${pct}%` }} />
            <span className={s.pillLabel}>{hand.initiative_labels[k] ?? k}</span>
            <span className={s.pillCount}>{stat.count}</span>
            <span className={s.pillFrac}>{stat.merged}/{stat.count}</span>
            {stat.attention > 0 && <span className={s.pillWarn}>{stat.attention}⚠</span>}
          </button>
        );
      })}
      {totalBlocked > 0 && (
        <span className={s.scopeBlocked} title="Tickets blocked by another ticket's decision">
          ⛔ {totalBlocked} blocked
        </span>
      )}
      {epic && (
        <div className={s.breadcrumb}>
          <span>drilled into</span>
          <span className={s.crumb}>{epic}</span>
          <button className={s.crumbClear} onClick={onClearEpic}>clear ✕</button>
        </div>
      )}
    </div>
  );
}

// ─── Kanban ──────────────────────────────────────────────────────

function Kanban({
  hand, initiative, epic, selectedKey, onCardClick, onEpicClick, onBlockedJump,
}: {
  hand: HandView; initiative: string | null; epic: string | null;
  selectedKey: string | null;
  onCardClick: (key: string) => void;
  onEpicClick: (epic: string) => void;
  onBlockedJump: (key: string) => void;
}) {
  const all = [...hand.tickets, ...hand.adhoc];
  const visible = all.filter((t) => {
    const ti = t.initiative ?? 'misc';
    if (initiative && ti !== initiative) return false;
    if (epic && t.epic !== epic) return false;
    return true;
  });

  const byStage = useMemo(() => {
    const map: Record<Stage, Ticket[]> = {
      triage: [], scope: [], plan: [], code: [], verify: [],
      pre_push: [], deploy: [], pr_open: [], review: [], merge: [],
    };
    visible.forEach((t) => map[t.stage].push(t));
    return map;
  }, [visible]);

  return (
    <div className={s.kanbanWrap}>
      <div className={s.kanban}>
        {STAGES.map((stage) => {
          const items = byStage[stage.key];
          const attention = items.filter((t) => t.attention).length;
          return (
            <div
              key={stage.key}
              className={`${s.col} ${stage.terminal ? s.colTerminal : ''} ${attention > 0 ? s.colAttention : ''}`}
            >
              <div className={s.colHeader}>
                <div className={s.colTitle}>{stage.label}</div>
                <div className={s.colCounts}>
                  {attention > 0 && <span className={s.colAttn}>{attention}⚠</span>}
                  <span className={s.colCount}>{items.length}</span>
                </div>
              </div>
              <div className={s.colBody}>
                {items.length === 0 ? (
                  <div className={s.empty}>—</div>
                ) : (
                  items.map((t) => (
                    <TicketCard
                      key={t.key}
                      ticket={t}
                      selected={selectedKey === t.key}
                      onClick={() => onCardClick(t.key)}
                      onEpicClick={onEpicClick}
                      onBlockedClick={onBlockedJump}
                    />
                  ))
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TicketCard({
  ticket, selected, onClick, onEpicClick, onBlockedClick,
}: {
  ticket: Ticket; selected: boolean;
  onClick: () => void;
  onEpicClick: (e: string) => void;
  onBlockedClick: (k: string) => void;
}) {
  return (
    <div
      className={`${s.card} ${ticket.attention ? s.cardAttention : ''} ${selected ? s.cardSelected : ''} ${ticket.blocked_by ? s.cardBlocked : ''}`}
      onClick={onClick}
    >
      <div className={`${s.cardKey} ${ticket.adhoc ? s.cardKeyAdhoc : ''}`}>
        <span>{ticket.key}</span>
        {ticket.epic && (
          <span
            className={s.epicChip}
            title={`Drill into ${ticket.epic}`}
            onClick={(e) => { e.stopPropagation(); onEpicClick(ticket.epic!); }}
          >
            {ticket.epic}
          </span>
        )}
        {ticket.blocked_by && (
          <span
            className={s.blockedChip}
            title={`Blocked by ${ticket.blocked_by} — ${ticket.blocked_reason ?? 'awaits decision'}`}
            onClick={(e) => { e.stopPropagation(); onBlockedClick(ticket.blocked_by!); }}
          >
            ⛔ {ticket.blocked_by}
          </span>
        )}
        {ticket.attention && <span className={s.attentionGlyph}>⚠</span>}
      </div>
      <div className={s.cardSummary}>{ticket.summary}</div>
      {ticket.hint && <div className={s.cardHint}>{ticket.hint}</div>}
    </div>
  );
}

// ─── Side panel ──────────────────────────────────────────────────

function SidePanel({
  ticket, expandedStep, onClose, onToggleStep,
}: {
  ticket: Ticket | null;
  expandedStep: { key: string; index: number } | null;
  onClose: () => void;
  onToggleStep: (key: string, index: number) => void;
}) {
  if (!ticket) {
    return <div className={s.sidePanel} />;
  }
  const isExpanded = expandedStep?.key === ticket.key;
  return (
    <div className={`${s.sidePanel} ${ticket ? s.sidePanelOpen : ''} ${isExpanded ? s.sidePanelWide : ''}`}>
      <div className={s.panelHeader}>
        <div>
          <span className={s.panelKey}>{ticket.key}</span>
          <span className={s.panelSummary}>{ticket.summary}</span>
        </div>
        <button className={s.closeBtn} onClick={onClose}>✕</button>
      </div>
      <div className={`${s.panelBody} ${isExpanded ? s.panelBodyHasExpand : ''}`}>
        <PanelGoal ticket={ticket} />
        <PanelDone ticket={ticket} expandedStep={expandedStep} onToggle={onToggleStep} />
        <PanelNow ticket={ticket} />
        <PanelDecideOrWatching ticket={ticket} />
        {isExpanded && expandedStep && (
          <ExpandPane
            ticket={ticket}
            index={expandedStep.index}
            onClose={() => onToggleStep(expandedStep.key, expandedStep.index)}
          />
        )}
      </div>
    </div>
  );
}

function PanelGoal({ ticket }: { ticket: Ticket }) {
  return (
    <div className={`${s.section} ${s.sectionGoal}`}>
      <div className={s.sectionLabel}>
        <span className={s.sectionNum}>1</span> GOAL
      </div>
      <div className={s.sectionContent}>{ticket.goal || '(no goal recorded)'}</div>
    </div>
  );
}

function PanelDone({
  ticket, expandedStep, onToggle,
}: {
  ticket: Ticket;
  expandedStep: { key: string; index: number } | null;
  onToggle: (key: string, index: number) => void;
}) {
  if (!ticket.done || ticket.done.length === 0) return null;
  const activeIdx = expandedStep?.key === ticket.key ? expandedStep.index : -1;
  return (
    <div className={`${s.section} ${s.sectionDone}`}>
      <div className={s.sectionLabel}>
        <span className={s.sectionNum}>2</span> DONE
      </div>
      <div className={s.sectionContent}>
        <ul className={s.doneList}>
          {ticket.done.map((d, i) => (
            <li
              key={i}
              className={`${s.doneItem} ${i === activeIdx ? s.doneItemActive : ''}`}
              onClick={() => onToggle(ticket.key, i)}
            >
              <span className={s.check}>✓</span>
              <span
                className={`${s.doneText} ${i >= ticket.done.length - 2 ? s.doneTextRecent : ''} ${i === activeIdx ? s.expandHintOpen : s.expandHint}`}
              >
                {d}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function PanelNow({ ticket }: { ticket: Ticket }) {
  if (!ticket.now) return null;
  return (
    <div className={`${s.section} ${s.sectionNow}`}>
      <div className={s.sectionLabel}>
        <span className={s.sectionNum}>2</span> NOW
      </div>
      <div className={s.sectionContent}>
        <div className={s.nowLine}>{ticket.now.line}</div>
        <div className={s.nowMeta}>{ticket.now.meta}</div>
      </div>
    </div>
  );
}

function PanelDecideOrWatching({ ticket }: { ticket: Ticket }) {
  if (ticket.attention) return <Decide ticket={ticket} />;
  if (ticket.next_checkpoint || ticket.next_eta_seconds !== undefined) return <Watching ticket={ticket} />;
  return null;
}

function Decide({ ticket }: { ticket: Ticket }) {
  return (
    <div className={`${s.section} ${s.sectionDecide}`}>
      <div className={s.sectionLabel}>
        <span className={s.sectionNum}>3</span> DECIDE
      </div>
      <div className={s.sectionContent}>
        <div className={s.decidePrompt}>
          <strong>{ticket.checkpoint ?? 'review needed'}</strong> — operator decision required.
        </div>
        {ticket.deploy_rec && (
          <div className={`${s.recommendation} ${ticket.deploy_rec === 'no-deploy' ? s.noDeploy : ''}`}>
            <span className={s.recLabel}>
              {ticket.deploy_rec === 'deploy' ? '↓ DEPLOY recommended' :
               ticket.deploy_rec === 'no-deploy' ? '○ NO DEPLOY needed' : '? NEEDS REVIEW'}
            </span>
            {ticket.deploy_reason}
          </div>
        )}
        {ticket.diff && (
          <div className={s.diffList}>
            {ticket.diff.map((f, i) => (
              <div key={i} className={s.diffFile}>
                <span>{f.file}</span>
                <span>
                  <span className={s.ins}>+{f.ins}</span> <span className={s.del}>-{f.del}</span>
                </span>
              </div>
            ))}
          </div>
        )}
        {ticket.comments_preview && (
          <div className={s.diffList}>
            {ticket.comments_preview.map((c, i) => (
              <div key={i} className={s.diffFile}>
                <span><strong style={{ color: 'var(--accent)' }}>{c.author}:</strong> {c.body}</span>
              </div>
            ))}
          </div>
        )}
        <div className={s.actions}>
          <button className={`${s.btn} ${s.btnPrimary}`}>Approve</button>
          <button className={`${s.btn} ${s.btnDanger}`}>Reject</button>
          <button className={`${s.btn} ${s.btnTakeover}`}>⤵ Drop into chat</button>
        </div>
      </div>
    </div>
  );
}

function Watching({ ticket }: { ticket: Ticket }) {
  const eta = ticket.next_eta_seconds == null
    ? '—'
    : ticket.next_eta_seconds >= 60
      ? `~${Math.round(ticket.next_eta_seconds / 60)}m`
      : `~${ticket.next_eta_seconds}s`;
  return (
    <div className={`${s.section} ${s.sectionWatching}`}>
      <div className={s.sectionLabel}>
        <span className={s.sectionNum}>3</span> WATCHING FOR
      </div>
      <div className={s.sectionContent}>
        Next checkpoint: <strong>{ticket.next_checkpoint ?? 'pending'}</strong>
        <div className={s.nowMeta}>est. {eta} away · no operator action right now</div>
      </div>
    </div>
  );
}

function ExpandPane({
  ticket, index, onClose,
}: {
  ticket: Ticket; index: number; onClose: () => void;
}) {
  const stepLabel = ticket.done[index] ?? '(step)';
  const detail = `(no extra notes for this step yet — once record_state(details=...) is wired in the live pipeline, the multi-paragraph narrative for this step renders here)`;
  return (
    <div className={s.expandPane}>
      <div className={s.epHeader}>
        <span>Step {index + 1} · details</span>
        <button className={s.epClose} onClick={onClose}>collapse ▸</button>
      </div>
      <div className={s.epTitle}>{stepLabel}</div>
      <div className={s.epBody}>{detail}</div>
    </div>
  );
}
