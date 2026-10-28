import { useEffect, useState } from 'react';
import {
  AlertTriangle, ArrowLeft, Cpu, Eye, HelpCircle, Radio, ScrollText, Trophy, Wallet,
} from 'lucide-react';
import type {
  BotPosition, Crypto15mPosition, Crypto15mSnapshot, Crypto15mStatus, LogEntry, SignalRow,
  TraderConfig,
} from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { Card, Empty, Page } from '../components/common';
import { cls, fmtCents, fmtRelative, fmtTimeShort, fmtUsd } from '../utils/format';
import {
  globalBlockers, SKIP_KIND_FIX, SKIP_KIND_LABEL, SkipKind, skipReason,
} from '../utils/skipReasons';
import type { PageId } from '../App';
import { AgentView, Metric, StatePill, isOpen } from './Agents';

const SNAPSHOT_POLL_MS = 15000;
const FEED_LIMIT = 30;
const LOG_LIMIT = 60;

interface AgentDetailProps {
  agent: AgentView;
  c15: Crypto15mStatus | null;
  onBack: () => void;
  onNav: (p: PageId) => void;
}

/** Drill-down for one agent: what it's doing right now and its latest activity. */
export function AgentDetail({ agent: a, c15, onBack, onNav }: AgentDetailProps) {
  const Icon = a.icon;
  return (
    <Page
      title={a.name}
      subtitle={a.role}
      actions={
        <>
          <button onClick={onBack} className="krypt-btn-default">
            <ArrowLeft className="mr-1 inline h-3.5 w-3.5" /> All agents
          </button>
          <button onClick={() => onNav(a.configurePage)} className="krypt-btn-default">
            <Cpu className="mr-1 inline h-3.5 w-3.5" /> Configure
          </button>
        </>
      }
    >
      <Card>
        <div className="flex items-center gap-3">
          <div
            className="grid h-10 w-10 shrink-0 place-items-center rounded-lg"
            style={{ background: `${a.color}22`, color: a.color }}
          >
            <Icon className="h-5 w-5" />
          </div>
          <div className="flex-1">
            <div className="text-[11px] uppercase tracking-wider text-krypt-muted">Right now</div>
            <div className="text-sm text-white">{doingNow(a)}</div>
          </div>
          <StatePill state={a.state} note={a.stateNote} />
        </div>
        <div className="mt-4 grid grid-cols-3 gap-2 text-xs md:grid-cols-6">
          <Metric label="Signals 24h" value={a.signals24h === null ? '—' : String(a.signals24h)} />
          <Metric label="Trades 24h" value={a.traded24h === null ? '—' : String(a.traded24h)} />
          <Metric label="Hit rate" value={a.hitRate === null ? '—' : `${a.hitRate.toFixed(0)}%`} />
          <Metric label="Open" value={String(a.openPositions)} />
          <Metric
            label="Realized"
            value={fmtUsd(a.realizedPnl, { sign: true })}
            tone={a.realizedPnl > 0 ? 'good' : a.realizedPnl < 0 ? 'bad' : undefined}
          />
          <Metric label="Last seen" value={fmtRelative(a.lastActivity)} />
        </div>
      </Card>

      {a.id === 'crypto15m'
        ? <Crypto15mActivity c15={c15} />
        : <ScannerActivity agent={a} />}
    </Page>
  );
}

function doingNow(a: AgentView): string {
  if (a.state === 'error') return `Not running — ${a.stateNote}.`;
  if (a.state === 'off') return 'Switched off. It is not looking for or placing trades.';
  const last = a.lastActivity ? ` Last activity ${fmtRelative(a.lastActivity)}.` : '';
  if (a.id === 'crypto15m') {
    return a.state === 'active'
      ? `Watching 15-minute crypto markets and placing live orders when its rules trigger.${last}`
      : `Watching 15-minute crypto markets and paper-trading (no real orders).${last}`;
  }
  const what = a.id === 'whale' ? 'big whale orders' : 'sudden trading surges';
  return a.state === 'active'
    ? `Scanning Kalshi for ${what} and placing bets on signals that pass your filters.${last}`
    : `Scanning Kalshi for ${what}. Auto-trading is paused, so it only records signals.${last}`;
}

function ScannerActivity({ agent: a }: { agent: AgentView }) {
  const { signals, positions, logs, config, account } = useApp();
  const env = config?.kalshiEnv ?? 'demo';
  const feed = signals.filter((s) => s.source === a.id).slice(0, FEED_LIMIT);
  const blockers = globalBlockers(config, account);
  const mine = positions.filter((p) => p.kalshiEnv === env && p.signalSource === a.id);
  const open = mine.filter(isOpen);
  const results = mine.filter((p) => p.resolved && p.status !== 'dry_run').slice(0, 15);
  const agentLogs = logs
    .filter((l) => l.source === a.id || (l.source === 'trader' && l.msg.toLowerCase().includes(a.id)))
    .slice(-LOG_LIMIT)
    .reverse();

  return (
    <>
      {config && <WhyNoBets feed={feed} cfg={config} blockers={blockers} />}

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Section icon={Radio} title="Live feed" hint="signals it found">
          {feed.length === 0 ? (
            <Empty title="No signals yet" description="New signals appear here as the agent finds them." />
          ) : (
            <div className="flex max-h-96 flex-col divide-y divide-krypt-border overflow-y-auto">
              {feed.map((s) => <SignalLine key={s.id} s={s} cfg={config} />)}
            </div>
          )}
        </Section>

        <Section icon={Wallet} title="Open bets" hint={`${open.length} open`}>
          {open.length === 0 ? (
            <Empty title="No open bets" description="Bets this agent places show up here until they settle." />
          ) : (
            <div className="flex max-h-96 flex-col divide-y divide-krypt-border overflow-y-auto">
              {open.map((p) => <PositionLine key={p.id} p={p} />)}
            </div>
          )}
        </Section>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Section icon={Trophy} title="Recent results" hint="settled bets">
          {results.length === 0 ? (
            <Empty title="Nothing settled yet" description="Wins and losses appear as markets resolve." />
          ) : (
            <div className="flex flex-col divide-y divide-krypt-border">
              {results.map((p) => <PositionLine key={p.id} p={p} />)}
            </div>
          )}
        </Section>

        <LogSection logs={agentLogs} />
      </div>
    </>
  );
}

function Crypto15mActivity({ c15 }: { c15: Crypto15mStatus | null }) {
  const { logs } = useApp();
  const [snap, setSnap] = useState<Crypto15mSnapshot | null>(null);

  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const s = await window.krypt.crypto15m.snapshot();
        if (mounted) setSnap(s);
      } catch {   }
    };
    void load();
    const i = window.setInterval(load, SNAPSHOT_POLL_MS);
    return () => {
      mounted = false;
      window.clearInterval(i);
    };
  }, []);

  const agentLogs = logs
    .filter((l) => /15m|crypto15m/i.test(l.msg))
    .slice(-LOG_LIMIT)
    .reverse();

  return (
    <>
      <div className="mt-4">
        <Section icon={Eye} title="Watching now" hint={snap ? `updated ${fmtRelative(snap.fetchedAt)}` : 'loading…'}>
          {!snap || snap.assets.length === 0 ? (
            <Empty title="No market data yet" description="Live crypto markets load within a few seconds." />
          ) : (
            <table className="w-full text-xs">
              <thead className="text-left text-[10px] uppercase tracking-wider text-krypt-muted">
                <tr>
                  <th className="pb-2 font-normal">Coin</th>
                  <th className="pb-2 text-right font-normal">Price</th>
                  <th className="pb-2 text-right font-normal">Move (15m)</th>
                  <th className="pb-2 text-right font-normal">Favorite</th>
                  <th className="pb-2 text-right font-normal">Closes in</th>
                  <th className="pb-2 text-right font-normal">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-krypt-border">
                {snap.assets.map((x) => (
                  <tr key={x.asset}>
                    <td className="py-2 font-medium text-white">{x.asset}</td>
                    <td className="py-2 text-right font-mono text-krypt-muted">
                      {x.spotUsd === null ? '—' : fmtUsd(x.spotUsd)}
                    </td>
                    <td className={cls(
                      'py-2 text-right font-mono',
                      (x.deltaPct ?? 0) >= 0 ? 'text-krypt-win' : 'text-krypt-loss',
                    )}>
                      {x.deltaPct == null ? '—' : `${x.deltaPct >= 0 ? '+' : ''}${(x.deltaPct * 100).toFixed(2)}%`}
                    </td>
                    <td className="py-2 text-right font-mono text-white">
                      {x.favorite && x.favoritePrice !== null
                        ? `${x.favorite.toUpperCase()} ${fmtCents(x.favoritePrice * 100)}`
                        : '—'}
                    </td>
                    <td className="py-2 text-right font-mono text-krypt-muted">
                      {x.minsLeft === null ? '—' : `${Math.max(0, Math.round(x.minsLeft))}m`}
                    </td>
                    <td className="py-2 text-right">
                      <span className={cls(
                        'krypt-pill',
                        x.error ? 'border-krypt-loss/40 bg-krypt-loss/10 text-krypt-loss'
                          : x.signal ? 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win'
                            : x.inWindow ? 'border-krypt-warn/40 bg-krypt-warn/10 text-krypt-warn'
                              : 'border-krypt-border bg-white/[0.03] text-krypt-muted',
                      )}>
                        {x.error ? 'error' : x.signal ? 'signal!' : x.inWindow ? 'in window' : 'waiting'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Section icon={Wallet} title="Open bets" hint={`${c15?.open.length ?? 0} open`}>
          {!c15 || c15.open.length === 0 ? (
            <Empty title="No open bets" description="Bets this agent places show up here until they close." />
          ) : (
            <div className="flex flex-col divide-y divide-krypt-border">
              {c15.open.map((p) => <C15Line key={p.id} p={p} />)}
            </div>
          )}
        </Section>

        <Section icon={Trophy} title="Recent trades" hint={c15?.live ? 'live' : 'paper'}>
          {!c15 || c15.recent.length === 0 ? (
            <Empty title="No trades yet" description="Trades appear here as the agent enters markets." />
          ) : (
            <div className="flex max-h-96 flex-col divide-y divide-krypt-border overflow-y-auto">
              {c15.recent.slice(0, FEED_LIMIT).map((p) => <C15Line key={p.id} p={p} />)}
            </div>
          )}
        </Section>
      </div>

      <div className="mt-4">
        <LogSection logs={agentLogs} />
      </div>
    </>
  );
}

function Section({
  icon: Icon, title, hint, children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <Card header={
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-krypt-purple" />
          <span className="text-sm text-white">{title}</span>
        </div>
        {hint && <span className="text-xs text-krypt-muted">{hint}</span>}
      </div>
    }>
      {children}
    </Card>
  );
}

function LogSection({ logs }: { logs: LogEntry[] }) {
  return (
    <Section icon={ScrollText} title="Activity log" hint="newest first">
      {logs.length === 0 ? (
        <Empty title="No log lines yet" description="The agent's own messages show up here while the app runs." />
      ) : (
        <div className="max-h-96 overflow-y-auto font-mono text-[11px] leading-relaxed">
          {logs.map((l, i) => (
            <div key={`${l.ts}-${i}`} className="flex gap-2">
              <span className="shrink-0 text-krypt-dim">{fmtTimeShort(l.ts)}</span>
              <span className={cls(
                'shrink-0',
                l.level === 'ERROR' || l.level === 'CRITICAL' ? 'text-krypt-loss'
                  : l.level === 'WARN' ? 'text-krypt-warn' : 'text-krypt-muted',
              )}>
                {l.level}
              </span>
              <span className="break-all text-white/80">{l.msg}</span>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

/** Plain-language summary of why recent signals did not become bets. */
function WhyNoBets({
  feed, cfg, blockers,
}: { feed: SignalRow[]; cfg: TraderConfig; blockers: string[] }) {
  const skipped = feed.filter((s) => !s.traded);
  if (skipped.length === 0 && blockers.length === 0) return null;

  const counts = new Map<SkipKind, number>();
  for (const s of skipped) {
    const k = skipReason(s, cfg).kind;
    counts.set(k, (counts.get(k) ?? 0) + 1);
  }
  const ranked = Array.from(counts.entries()).sort((x, y) => y[1] - x[1]);

  return (
    <div className="mt-4">
      <Section
        icon={HelpCircle}
        title="Why no bets?"
        hint={`${skipped.length} of the last ${feed.length} signals skipped`}
      >
        {blockers.length > 0 && (
          <div className="mb-3 flex flex-col gap-2">
            {blockers.map((b) => (
              <div
                key={b}
                className="flex items-start gap-2 rounded-lg border border-krypt-loss/30 bg-krypt-loss/5 p-2 text-xs text-krypt-loss"
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{b}</span>
              </div>
            ))}
          </div>
        )}
        {ranked.length > 0 && (
          <div className="flex flex-col divide-y divide-krypt-border text-xs">
            {ranked.map(([kind, n]) => (
              <div key={kind} className="flex items-start gap-3 py-2">
                <span className="w-10 shrink-0 text-right font-mono text-white">{n}×</span>
                <div className="flex-1">
                  <div className="text-white">{SKIP_KIND_LABEL[kind]}</div>
                  <div className="text-krypt-muted">{SKIP_KIND_FIX[kind]}</div>
                </div>
              </div>
            ))}
          </div>
        )}
        <p className="mt-3 text-[11px] leading-relaxed text-krypt-dim">
          Skipping most signals is normal: the rules exist to filter out weak bets. Loosening them means
          more bets, but lower-quality ones. Try changes on Demo first.
        </p>
      </Section>
    </div>
  );
}

function SignalLine({ s, cfg }: { s: SignalRow; cfg: TraderConfig | null }) {
  const why = !s.traded && cfg ? skipReason(s, cfg) : null;
  return (
    <div className="flex items-center gap-3 py-2 text-xs">
      <span className="w-14 shrink-0 text-krypt-dim">{fmtRelative(s.createdAt)}</span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-white">{s.title}</div>
        <div className="font-mono text-[10px] text-krypt-muted">
          {s.ticker} · conf {s.confidence.toFixed(0)} · edge {s.edgePts >= 0 ? '+' : ''}{s.edgePts.toFixed(1)}
        </div>
        {why && <div className="text-[10px] text-krypt-warn">why skipped: {why.text}</div>}
      </div>
      <span className={cls('font-mono', s.direction === 'yes' ? 'text-krypt-win' : 'text-krypt-loss')}>
        {s.direction.toUpperCase()} {s.priceCents}¢
      </span>
      <span className={cls(
        'krypt-pill shrink-0',
        s.traded ? 'border-krypt-indigo/40 bg-krypt-indigo/10 text-krypt-indigo'
          : 'border-krypt-border bg-white/[0.03] text-krypt-muted',
      )}>
        {s.traded ? 'bet placed' : 'skipped'}
      </span>
    </div>
  );
}

function PositionLine({ p }: { p: BotPosition }) {
  const contracts = p.filledContracts || p.targetContracts;
  return (
    <div className="flex items-center gap-3 py-2 text-xs">
      <div className="min-w-0 flex-1">
        <div className="truncate text-white">{p.title}</div>
        <div className="font-mono text-[10px] text-krypt-muted">
          {p.direction.toUpperCase()} · {contracts} × {fmtCents(p.avgFillPriceCents ?? p.limitPriceCents)} · {p.status}
        </div>
      </div>
      {p.resolved ? (
        <span className={cls('font-mono', (p.pnlUsd ?? 0) >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
          {fmtUsd(p.pnlUsd, { sign: true })}
        </span>
      ) : (
        <span className="font-mono text-krypt-muted">{fmtUsd(p.costUsd)} at risk</span>
      )}
    </div>
  );
}

function C15Line({ p }: { p: Crypto15mPosition }) {
  return (
    <div className="flex items-center gap-3 py-2 text-xs">
      <span className="w-14 shrink-0 text-krypt-dim">{fmtRelative(p.createdAt)}</span>
      <div className="min-w-0 flex-1">
        <div className="text-white">{p.asset} {p.side ? p.side.toUpperCase() : ''}</div>
        <div className="font-mono text-[10px] text-krypt-muted">
          {p.filledContracts || p.targetContracts} × {fmtCents(p.avgEntryCents ?? p.entryLimitCents)} · {p.status}
          {p.dryRun ? ' · paper' : ''}
        </div>
      </div>
      {p.pnlUsd !== null ? (
        <span className={cls('font-mono', p.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
          {fmtUsd(p.pnlUsd, { sign: true })}
        </span>
      ) : (
        <span className="font-mono text-krypt-muted">{fmtUsd(p.costUsd)} at risk</span>
      )}
    </div>
  );
}
