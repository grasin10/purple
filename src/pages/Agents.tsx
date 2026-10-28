import { useEffect, useMemo, useState } from 'react';
import {
  Activity, ArrowRight, Bitcoin, Cpu, Fish, FlaskConical, TrendingUp,
} from 'lucide-react';
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { BotPosition, Crypto15mStatus, SignalSource } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { Card, Empty, Page, StatCard } from '../components/common';
import { cls, fmtNum, fmtRelative, fmtUsd, titleCase } from '../utils/format';
import type { PageId } from '../App';

const C15_POLL_MS = 15000;
const DAY_MS = 24 * 3600 * 1000;

type AgentState = 'active' | 'watching' | 'off' | 'error';

interface AgentView {
  id: string;
  name: string;
  role: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  state: AgentState;
  stateNote: string;
  signals24h: number | null;
  traded24h: number | null;
  hitRate: number | null;
  resolved: number;
  openPositions: number;
  realizedPnl: number;
  lastActivity: string | null;
  hourly: { h: string; n: number }[] | null;
  configurePage: PageId;
}

interface AgentsProps {
  onNav: (p: PageId) => void;
}

/** Read-only overview of every trading agent: what it's doing and how it's performing. */
export function AgentsPage({ onNav }: AgentsProps) {
  const { config, backend, scannerStats, signals, positions, strategies } = useApp();
  const [c15, setC15] = useState<Crypto15mStatus | null>(null);

  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const s = await window.krypt.crypto15m.status();
        if (mounted) setC15(s);
      } catch {   }
    };
    void load();
    const i = window.setInterval(load, C15_POLL_MS);
    return () => {
      mounted = false;
      window.clearInterval(i);
    };
  }, []);

  const env = config?.kalshiEnv ?? 'demo';
  const engineUp = backend.status === 'running';
  const tradingOn = !!config?.enableTrading;

  const agents = useMemo<AgentView[]>(() => {
    const envPositions = positions.filter((p) => p.kalshiEnv === env);
    const scanAgent = (
      source: SignalSource,
      name: string,
      role: string,
      icon: AgentView['icon'],
      color: string,
      enabled: boolean,
      stats: { total: number; resolved: number; winRate: number } | undefined,
      lastScan: string | null | undefined,
    ): AgentView => {
      const sig = signals.filter((s) => s.source === source);
      const pos = envPositions.filter((p) => p.signalSource === source);
      return {
        id: source,
        name,
        role,
        icon,
        color,
        ...agentState(engineUp, enabled, tradingOn),
        signals24h: countSince(sig, DAY_MS),
        traded24h: countSince(sig.filter((s) => s.traded), DAY_MS),
        hitRate: stats && stats.resolved > 0 ? stats.winRate : null,
        resolved: stats?.resolved ?? 0,
        openPositions: pos.filter(isOpen).length,
        realizedPnl: realized(pos),
        lastActivity: lastScan ?? sig[0]?.createdAt ?? null,
        hourly: hourlyBuckets(sig),
        configurePage: 'settings',
      };
    };

    const list: AgentView[] = [
      scanAgent(
        'whale', 'Whale Tracker', 'Follows big taker orders', Fish, '#A855F7',
        !!config?.tradeWhales, scannerStats?.whales, scannerStats?.lastWhaleScanAt,
      ),
      scanAgent(
        'momentum', 'Momentum Scanner',
        config?.contrarianOnly ? 'Fades crowd surges (contrarian)' : 'Rides volume & price surges',
        TrendingUp, '#EC4899',
        !!config?.tradeMomentum, scannerStats?.momentum, scannerStats?.lastMomentumScanAt,
      ),
    ];

    const c15Stats = c15?.stats;
    const c15Resolved = (c15Stats?.wins ?? 0) + (c15Stats?.losses ?? 0);
    list.push({
      id: 'crypto15m',
      name: '15m Crypto',
      role: c15?.live ? 'Trading 15-minute crypto markets' : 'Paper-trading 15-minute crypto markets',
      icon: Bitcoin,
      color: '#F59E0B',
      ...(c15 === null
        ? { state: 'error' as const, stateNote: 'status unavailable' }
        : !c15.enabled
          ? { state: 'off' as const, stateNote: 'disabled' }
          : c15.live
            ? { state: 'active' as const, stateNote: 'live orders' }
            : { state: 'watching' as const, stateNote: 'dry-run' }),
      signals24h: null,
      traded24h: c15 ? countSince(c15.recent, DAY_MS) : null,
      hitRate: c15Resolved > 0 ? ((c15Stats?.wins ?? 0) / c15Resolved) * 100 : null,
      resolved: c15Resolved,
      openPositions: c15Stats?.openCount ?? 0,
      realizedPnl: c15Stats?.realizedPnlUsd ?? 0,
      lastActivity: c15?.recent[0]?.createdAt ?? null,
      hourly: c15 ? hourlyBuckets(c15.recent) : null,
      configurePage: 'crypto15m',
    });
    return list;
  }, [config, scannerStats, signals, positions, c15, env, engineUp, tradingOn]);

  const activeCount = agents.filter((a) => a.state === 'active' || a.state === 'watching').length;
  const totalOpen = agents.reduce((s, a) => s + a.openPositions, 0);
  const totalPnl = agents.reduce((s, a) => s + a.realizedPnl, 0);
  const pnlBars = agents.map((a) => ({ name: a.name, pnl: a.realizedPnl, color: a.color }));
  const lab = strategies.filter((s) => !s.secret && !s.comingSoon);

  return (
    <Page title="Agents" subtitle="Every trading agent at a glance — status, activity and performance.">
      <div className="grid gap-4 md:grid-cols-4">
        <StatCard
          label="Engine"
          value={engineUp ? 'Online' : titleCase(backend.status)}
          hint={`${env === 'production' ? 'LIVE' : 'DEMO'} · ${tradingOn ? 'trading on' : 'dry-run / paused'}`}
          accent={engineUp ? 'good' : 'bad'}
        />
        <StatCard
          label="Agents running"
          value={`${activeCount} / ${agents.length}`}
          hint="active or watching"
        />
        <StatCard
          label="Open positions"
          value={fmtNum(totalOpen)}
          hint="across all agents"
        />
        <StatCard
          label="Realized P&L"
          value={fmtUsd(totalPnl, { sign: true })}
          hint="settled trades, all agents"
          accent={totalPnl >= 0 ? 'good' : 'bad'}
        />
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-3">
        {agents.map((a) => (
          <AgentCard key={a.id} agent={a} onNav={onNav} />
        ))}
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <Card header={
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-krypt-purple" />
            <span className="text-sm text-white">Realized P&amp;L by agent</span>
          </div>
        }>
          {pnlBars.every((b) => b.pnl === 0) ? (
            <Empty
              title="No settled trades yet"
              description="Bars appear once each agent's trades resolve."
            />
          ) : (
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={pnlBars} layout="vertical" margin={{ left: 8, right: 16 }}>
                  <XAxis
                    type="number"
                    tickFormatter={(v) => `$${Math.round(v)}`}
                    tick={{ fill: '#71717A', fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    type="category"
                    dataKey="name"
                    width={120}
                    tick={{ fill: '#A1A1AA', fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <Tooltip
                    cursor={{ fill: 'rgba(255,255,255,0.03)' }}
                    contentStyle={{
                      background: '#171722',
                      border: '1px solid rgba(255,255,255,0.1)',
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    formatter={(v: number) => [fmtUsd(v, { sign: true }), 'Realized']}
                  />
                  <Bar dataKey="pnl" radius={[0, 4, 4, 0]}>
                    {pnlBars.map((b) => (
                      <Cell key={b.name} fill={b.pnl >= 0 ? '#22C55E' : '#EF4444'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>

        <Card header={
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <FlaskConical className="h-4 w-4 text-krypt-pink" />
              <span className="text-sm text-white">Strategy lab</span>
            </div>
            <button onClick={() => onNav('strategies')} className="text-xs text-krypt-muted hover:text-white">
              Strategies <ArrowRight className="ml-1 inline h-3 w-3" />
            </button>
          </div>
        }>
          {lab.length === 0 ? (
            <Empty title="No presets loaded" description="Strategy presets load with the app." />
          ) : (
            <table className="w-full text-xs">
              <thead className="text-left text-[10px] uppercase tracking-wider text-krypt-muted">
                <tr>
                  <th className="pb-2 font-normal">Preset</th>
                  <th className="pb-2 text-right font-normal">Net ¢/contract</th>
                  <th className="pb-2 text-right font-normal">t</th>
                  <th className="pb-2 text-right font-normal">n</th>
                  <th className="pb-2 text-right font-normal">Confidence</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-krypt-border">
                {lab.map((s) => (
                  <tr key={s.id}>
                    <td className="py-2 text-white">{s.name}</td>
                    <td className={cls(
                      'py-2 text-right font-mono',
                      (s.backtest?.netCents ?? 0) >= 0 ? 'text-krypt-win' : 'text-krypt-loss',
                    )}>
                      {s.backtest ? `${s.backtest.netCents >= 0 ? '+' : ''}${s.backtest.netCents.toFixed(1)}` : '—'}
                    </td>
                    <td className="py-2 text-right font-mono text-krypt-muted">
                      {s.backtest ? s.backtest.t.toFixed(1) : '—'}
                    </td>
                    <td className="py-2 text-right font-mono text-krypt-muted">
                      {s.backtest ? s.backtest.n : '—'}
                    </td>
                    <td className="py-2 text-right">
                      <ConfidencePill t={s.backtest?.t ?? null} n={s.backtest?.n ?? 0} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="mt-3 text-[11px] leading-relaxed text-krypt-dim">
            In-sample backtests on small samples. |t| under 2 or n under 100 can easily be luck —
            paper-trade before risking real money.
          </p>
        </Card>
      </div>
    </Page>
  );
}

function AgentCard({ agent: a, onNav }: { agent: AgentView; onNav: (p: PageId) => void }) {
  const Icon = a.icon;
  return (
    <Card>
      <div className="flex items-start gap-3">
        <div
          className="grid h-10 w-10 shrink-0 place-items-center rounded-lg"
          style={{ background: `${a.color}22`, color: a.color }}
        >
          <Icon className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-white">{a.name}</div>
          <div className="truncate text-xs text-krypt-muted">{a.role}</div>
        </div>
        <StatePill state={a.state} note={a.stateNote} />
      </div>

      <div className="mt-4 h-14">
        {a.hourly && a.hourly.some((b) => b.n > 0) ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={a.hourly}>
              <Tooltip
                cursor={{ fill: 'rgba(255,255,255,0.03)' }}
                contentStyle={{
                  background: '#171722',
                  border: '1px solid rgba(255,255,255,0.1)',
                  borderRadius: 8,
                  fontSize: 11,
                }}
                formatter={(v: number) => [v, 'events']}
                labelFormatter={(l) => `${l} ago`}
              />
              <Bar dataKey="n" fill={a.color} radius={[2, 2, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="grid h-full place-items-center rounded-md border border-dashed border-krypt-border text-[11px] text-krypt-dim">
            no activity in the last 24h
          </div>
        )}
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <Metric label="Signals 24h" value={a.signals24h === null ? '—' : fmtNum(a.signals24h)} />
        <Metric label="Trades 24h" value={a.traded24h === null ? '—' : fmtNum(a.traded24h)} />
        <Metric
          label="Hit rate"
          value={a.hitRate === null ? '—' : `${a.hitRate.toFixed(0)}%`}
          hint={a.resolved > 0 ? `${a.resolved} resolved` : undefined}
        />
        <Metric label="Open" value={fmtNum(a.openPositions)} />
        <Metric
          label="Realized"
          value={fmtUsd(a.realizedPnl, { sign: true })}
          tone={a.realizedPnl > 0 ? 'good' : a.realizedPnl < 0 ? 'bad' : undefined}
        />
        <Metric label="Last seen" value={fmtRelative(a.lastActivity)} />
      </div>

      <button
        onClick={() => onNav(a.configurePage)}
        className="mt-4 flex w-full items-center justify-center gap-1 rounded-md border border-krypt-border py-1.5 text-xs text-krypt-muted hover:border-krypt-borderHi hover:text-white"
      >
        <Cpu className="h-3 w-3" /> Configure
      </button>
    </Card>
  );
}

function StatePill({ state, note }: { state: AgentState; note: string }) {
  const tone = {
    active: 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win',
    watching: 'border-krypt-indigo/40 bg-krypt-indigo/10 text-krypt-indigo',
    off: 'border-krypt-border bg-white/[0.03] text-krypt-muted',
    error: 'border-krypt-loss/40 bg-krypt-loss/10 text-krypt-loss',
  }[state];
  return (
    <span className={cls('krypt-pill shrink-0', tone)} title={note}>
      <span className={cls(
        'mr-1 inline-block h-1.5 w-1.5 rounded-full bg-current',
        state === 'active' && 'animate-pulse',
      )} />
      {state}
    </span>
  );
}

function ConfidencePill({ t, n }: { t: number | null; n: number }) {
  if (t === null) return <span className="text-krypt-dim">untested</span>;
  const strong = Math.abs(t) >= 2 && n >= 100;
  const okay = Math.abs(t) >= 2;
  return (
    <span className={cls(
      'krypt-pill',
      strong ? 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win'
        : okay ? 'border-krypt-warn/40 bg-krypt-warn/10 text-krypt-warn'
          : 'border-krypt-loss/40 bg-krypt-loss/10 text-krypt-loss',
    )}>
      {strong ? 'solid' : okay ? 'small sample' : 'weak'}
    </span>
  );
}

function Metric({
  label, value, hint, tone,
}: { label: string; value: string; hint?: string; tone?: 'good' | 'bad' }) {
  return (
    <div className="rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1.5" title={hint}>
      <div className="text-[10px] uppercase tracking-wider text-krypt-muted">{label}</div>
      <div className={cls(
        'font-mono text-sm',
        tone === 'good' ? 'text-krypt-win' : tone === 'bad' ? 'text-krypt-loss' : 'text-white',
      )}>
        {value}
      </div>
    </div>
  );
}

function agentState(
  engineUp: boolean, enabled: boolean, tradingOn: boolean,
): { state: AgentState; stateNote: string } {
  if (!engineUp) return { state: 'error', stateNote: 'engine offline' };
  if (!enabled) return { state: 'off', stateNote: 'not trading this source' };
  if (!tradingOn) return { state: 'watching', stateNote: 'scanning, auto-trading paused' };
  return { state: 'active', stateNote: 'scanning and trading' };
}

function isOpen(p: BotPosition): boolean {
  return !p.resolved && (p.status === 'filled' || p.status === 'partial' || p.status === 'submitted');
}

function realized(pos: BotPosition[]): number {
  return pos
    .filter((p) => p.resolved && p.status !== 'dry_run')
    .reduce((s, p) => s + (p.pnlUsd ?? 0), 0);
}

function countSince(rows: { createdAt: string }[], ms: number): number {
  const cutoff = Date.now() - ms;
  return rows.filter((r) => new Date(r.createdAt).getTime() >= cutoff).length;
}

/** 24 hourly buckets, oldest first, labelled by hours ago. */
function hourlyBuckets(rows: { createdAt: string }[]): { h: string; n: number }[] {
  const now = Date.now();
  const buckets = Array.from({ length: 24 }, (_, i) => ({ h: `${23 - i}h`, n: 0 }));
  for (const r of rows) {
    const ago = Math.floor((now - new Date(r.createdAt).getTime()) / 3600000);
    if (ago >= 0 && ago < 24) buckets[23 - ago].n += 1;
  }
  return buckets;
}
