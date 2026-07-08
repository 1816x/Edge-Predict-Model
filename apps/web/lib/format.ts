// Display formatting helpers. Conventions (see docs/05-motor-ev-y-bankroll.md):
// - probabilities: percentage with 1 decimal      -> "52.3%"
// - edge / EV: signed percentage points, 1 decimal -> "+2.4%"
// - decimal odds: 2 decimals                       -> "2.18"
// - stake (fraction of bankroll): %, 2 decimals    -> "0.31%"

import type { GameEvent, Market, PickStatus } from "@/lib/types";

export function formatProb(p: number): string {
  return `${(p * 100).toFixed(1)}%`;
}

/** Signed percentage points for edge / EV per unit. */
export function formatSignedPts(x: number): string {
  return `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%`;
}

export function formatOdds(odds: number): string {
  return odds.toFixed(2);
}

/** Stake as % of bankroll. */
export function formatStake(fraction: number): string {
  return `${(fraction * 100).toFixed(2)}%`;
}

export function formatEventLabel(event: Pick<GameEvent, "home_team" | "away_team">): string {
  return `${event.away_team} @ ${event.home_team}`;
}

// UI copy in Spanish (labels are user-facing text, not code identifiers).
export const MARKET_LABELS: Record<Market, string> = {
  ml: "Moneyline",
  f5_ml: "F5 Moneyline",
};

export const STATUS_LABELS: Record<PickStatus, string> = {
  pending: "Pendiente",
  won: "Ganado",
  lost: "Perdido",
  push: "Push",
  void: "Anulado",
};
