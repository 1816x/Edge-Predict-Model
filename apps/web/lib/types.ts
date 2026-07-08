// Shared domain types for the EDGE dashboard.
// They mirror the v1 API response models served by apps/api (FastAPI).
// Field names stay snake_case to match the API JSON payloads 1:1.
// Canonical definitions (edge, EV, Kelly, CLV, yield vs ROI):
// see docs/05-motor-ev-y-bankroll.md.

export type Sport = "mlb";

/** Markets covered by the MVP: MLB Moneyline and First-5-Innings Moneyline. */
export type Market = "ml" | "f5_ml";

export type PickStatus = "pending" | "won" | "lost" | "push" | "void";

export type Recommendation = "bet" | "pass";

/** Event descriptor as returned by the API. */
export interface GameEvent {
  sport: Sport;
  home_team: string;
  away_team: string;
  /** Scheduled first pitch, ISO 8601 UTC. */
  start_time: string;
}

/** Request body for POST /api/v1/analyze. */
export interface AnalyzeRequest {
  sport: Sport;
  market: Market;
  home_team: string;
  away_team: string;
  /** ISO date (YYYY-MM-DD). Defaults to today on the API side. */
  date?: string;
}

/** Response of POST /api/v1/analyze. */
export interface Analysis {
  event: GameEvent;
  market: Market;
  /** Calibrated model probability for the recommended side (0-1). */
  p_model: number;
  /** No-vig fair probability derived from the reference book (Pinnacle), 0-1. */
  p_fair: number;
  /** edge = p_model - p_fair, in probability points (0-1 scale). */
  edge: number;
  /** EV per 1 unit staked: p_model * (odds - 1) - (1 - p_model). */
  ev_per_unit: number;
  /** Full Kelly fraction: f* = (p * (b + 1) - 1) / b, with b = odds - 1. */
  kelly_full: number;
  /** Suggested stake as a fraction of bankroll, after user Kelly fraction and cap. */
  stake_suggested: number;
  recommendation: Recommendation;
  /**
   * Text written by the LLM layer from structured features.
   * The LLM never produces probabilities; they come from the calibrated model.
   */
  explanation: string;
  model_version: string;
}

/** A published value bet (item of GET /api/v1/picks/today). */
export interface Pick {
  pick_id: string;
  event: GameEvent;
  market: Market;
  /** Side the model backs (team name). */
  selection: string;
  /** Book where the quoted price was found (user-facing book, e.g. bet365). */
  book: string;
  /** Decimal odds at pick time. */
  price: number;
  p_model: number;
  p_fair: number;
  edge: number;
  ev_per_unit: number;
  kelly_full: number;
  stake_suggested: number;
  status: PickStatus;
  model_version: string;
  /** ISO 8601 timestamp of when the pick was published. */
  created_at: string;
}

/** Audit trail attached to a pick (GET /api/v1/picks/{pick_id}). */
export interface PickAudit {
  /** Decimal odds captured at the moment the pick was published. */
  odds_at_pick: number;
  model_version: string;
  /** Snapshot of key features fed to the model (as-of, before first pitch). */
  features: Record<string, number | string>;
  explanation: string;
  /** Pinnacle no-vig closing probability; null until the line closes. */
  closing_p_fair: number | null;
  /** CLV in probability points (price taken vs no-vig close); null until close. */
  clv: number | null;
  /** Settled result; null while pending. */
  result: PickStatus | null;
}

export interface PickDetail extends Pick {
  audit: PickAudit;
}

/**
 * Aggregated metrics (GET /api/v1/performance).
 * Computed only over registered picks (paper trading / production), never backtests.
 */
export interface Performance {
  /** Net profit / initial bankroll of the period. */
  roi: number;
  /** Net profit / total staked. Not the same as ROI. */
  yield: number;
  /** Share of settled picks that beat the Pinnacle no-vig close (0-1). */
  clv_beat_rate: number;
  /** Expected calibration error over the rolling window. */
  ece: number;
  /** Brier score over settled picks. */
  brier: number;
  /** Maximum drawdown in units (positive magnitude). */
  drawdown: number;
  /** Net units won/lost. */
  units: number;
  /** Number of settled picks behind these metrics. */
  n_picks: number;
}

export interface HealthResponse {
  status: string;
}
