import type { AccountSnapshot, SignalRow, TraderConfig } from '@shared/types';

/** Mirrors trader.py's minimum balance before any order is considered. */
const MIN_BALANCE_USD = 5;
/** Mirrors trader.py: orders sized under $1 are skipped. */
const MIN_ORDER_USD = 1;

export type SkipKind =
  | 'combo' | 'agent-off' | 'confidence' | 'edge' | 'signal-type' | 'category' | 'price'
  | 'gambling' | 'passed';

export interface SkipReason {
  kind: SkipKind;
  text: string;
}

export const SKIP_KIND_LABEL: Record<SkipKind, string> = {
  combo: 'Combo / parlay market',
  'agent-off': 'Agent switched off',
  confidence: 'Confidence too low',
  edge: 'Expected edge too small',
  'signal-type': 'Signal type not used',
  category: 'Category not allowed',
  price: 'Price outside your range',
  gambling: 'Random roll missed',
  passed: 'Passed your rules, but not placed',
};

export const SKIP_KIND_FIX: Record<SkipKind, string> = {
  combo: 'Nothing to change: Kalshi rejects bot orders on combo markets, so they are always skipped.',
  'agent-off': 'Turn this agent on in Settings (trade whales / trade momentum).',
  confidence: 'Lower the minimum confidence in Settings, or wait for stronger signals.',
  edge: 'Lower the minimum edge in Settings, or wait for better-priced signals.',
  'signal-type': 'Allow more momentum signal types in Settings.',
  category: 'Allow more categories in Settings, or pick a different preset.',
  price: 'Widen the min/max entry price in Settings.',
  gambling: 'Secret Strategy only bets on a random 10% of signals.',
  passed: 'It was probably too old by the time the engine checked, or a limit '
    + '(open bets, one bet per event, bet size, cash) blocked it.',
};

/**
 * Why the trading engine would not bet on this signal under the current config.
 * Mirrors should_trade() in python/trader.py; keep the two in sync.
 */
export function skipReason(s: SignalRow, cfg: TraderConfig): SkipReason {
  if (isComboMarket(s.ticker)) return { kind: 'combo', text: 'combo/parlay market' };
  if (cfg.gamblingMode) return { kind: 'gambling', text: 'random roll missed' };

  const conf = s.confidence;
  const edge = s.edgePts;
  if (s.source === 'whale') {
    if (!cfg.tradeWhales) return { kind: 'agent-off', text: 'whale trading is off' };
    if (conf < cfg.minConfidenceWhale) {
      return { kind: 'confidence', text: `confidence ${conf.toFixed(0)}, needs ${cfg.minConfidenceWhale}` };
    }
    if (edge < cfg.minEdgePtsWhale) {
      return { kind: 'edge', text: `edge ${edge.toFixed(1)}, needs ${cfg.minEdgePtsWhale}` };
    }
  } else if (s.source === 'momentum') {
    if (!cfg.tradeMomentum) return { kind: 'agent-off', text: 'momentum trading is off' };
    if (conf < cfg.minConfidenceMomentum) {
      return { kind: 'confidence', text: `confidence ${conf.toFixed(0)}, needs ${cfg.minConfidenceMomentum}` };
    }
    if (edge < cfg.minEdgePtsMomentum) {
      return { kind: 'edge', text: `edge ${edge.toFixed(1)}, needs ${cfg.minEdgePtsMomentum}` };
    }
    const type = s.signalType ?? '';
    if (!cfg.allowedMomentumSignalTypes.includes(type)) {
      return { kind: 'signal-type', text: `type "${type || 'unknown'}" not allowed` };
    }
  }

  const cat = (s.category || '').toLowerCase();
  const catBlock = categoryBlock(cat, cfg.allowedCategories)
    ?? categoryBlock(
      cat,
      s.source === 'momentum' ? cfg.allowedMomentumCategories : cfg.allowedWhaleCategories,
    );
  if (catBlock) return { kind: 'category', text: catBlock };

  if (s.priceCents < cfg.minEntryPriceCents) {
    return { kind: 'price', text: `price ${s.priceCents}¢, min ${cfg.minEntryPriceCents}¢` };
  }
  if (s.priceCents > cfg.maxEntryPriceCents) {
    return { kind: 'price', text: `price ${s.priceCents}¢, max ${cfg.maxEntryPriceCents}¢` };
  }
  return { kind: 'passed', text: 'passed your rules, not placed' };
}

/** Mirrors is_combo_market() in python/categorize.py. */
export function isComboMarket(ticker: string): boolean {
  return (ticker || '').toUpperCase().startsWith('KXMVE');
}

function categoryBlock(cat: string, allowed: string[] | null): string | null {
  if (allowed === null) return null;
  if (allowed.length === 0) return 'no categories enabled';
  if (!allowed.some((c) => c.toLowerCase() === cat)) {
    return `category "${cat || 'none'}" not allowed`;
  }
  return null;
}

/** Account-wide reasons that stop every bet, regardless of the signal. */
export function globalBlockers(cfg: TraderConfig | null, account: AccountSnapshot | null): string[] {
  if (!cfg) return [];
  const out: string[] = [];
  if (!cfg.enableTrading) {
    out.push('Trading is switched off. Turn on the trading switch in the top bar.');
  }
  const cash = account?.cashUsd;
  if (cash !== undefined && cash < MIN_BALANCE_USD) {
    out.push(`Cash is $${cash.toFixed(2)}. The engine needs at least $${MIN_BALANCE_USD} to place any bet.`);
  } else if (cash !== undefined) {
    const fix = 'In Settings, switch bet size to fixed and set it to $1 or more.';
    if (cfg.sizingMode === 'fixed') {
      const size = Math.min(cfg.fixedTradeUsd, cfg.hardMaxPositionUsd);
      if (size < MIN_ORDER_USD) {
        out.push(`Your fixed bet size is $${size.toFixed(2)}, and bets under $${MIN_ORDER_USD} are skipped. ${fix}`);
      }
    } else {
      const smallest = Math.min(cash * cfg.minSizeFraction, cfg.hardMaxPositionUsd);
      const largest = Math.min(cash * cfg.maxSizeFraction, cfg.hardMaxPositionUsd);
      if (largest < MIN_ORDER_USD) {
        out.push(
          `With $${cash.toFixed(2)} cash, every bet works out under $${MIN_ORDER_USD}, so all are skipped. ${fix}`,
        );
      } else if (smallest < MIN_ORDER_USD) {
        out.push(
          `With $${cash.toFixed(2)} cash, bets on weaker signals work out under $${MIN_ORDER_USD} and are skipped. ${fix}`,
        );
      }
    }
  }
  return out;
}
