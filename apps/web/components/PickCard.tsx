import Link from "next/link";
import EdgeBadge from "@/components/EdgeBadge";
import {
  MARKET_LABELS,
  STATUS_LABELS,
  formatEventLabel,
  formatOdds,
  formatProb,
  formatSignedPts,
  formatStake,
} from "@/lib/format";
import type { Pick } from "@/lib/types";

interface PickCardProps {
  pick: Pick;
  /** Disable the link when the card is rendered inside the detail page itself. */
  linkToDetail?: boolean;
}

/** Compact summary card for a published pick. */
export default function PickCard({ pick, linkToDetail = true }: PickCardProps) {
  const title = `${pick.selection} — ${formatEventLabel(pick.event)}`;

  return (
    <div className="card">
      <div className="pick-card-header">
        <div>
          <div className="pick-card-title">
            {linkToDetail ? (
              <Link href={`/picks/${pick.pick_id}`}>{title}</Link>
            ) : (
              title
            )}
          </div>
          <div className="pick-card-market">
            {MARKET_LABELS[pick.market]} · {pick.book} ·{" "}
            {STATUS_LABELS[pick.status]}
          </div>
        </div>
        <EdgeBadge edge={pick.edge} />
      </div>
      <div className="pick-card-grid">
        <div>
          <div className="metric-label">Precio (decimal)</div>
          <div className="metric-value">{formatOdds(pick.price)}</div>
        </div>
        <div>
          <div className="metric-label">p_model</div>
          <div className="metric-value">{formatProb(pick.p_model)}</div>
        </div>
        <div>
          <div className="metric-label">p_fair (no-vig)</div>
          <div className="metric-value">{formatProb(pick.p_fair)}</div>
        </div>
        <div>
          <div className="metric-label">EV / unidad</div>
          <div className="metric-value">{formatSignedPts(pick.ev_per_unit)}</div>
        </div>
        <div>
          <div className="metric-label">Stake sugerido</div>
          <div className="metric-value">
            {formatStake(pick.stake_suggested)} del bankroll
          </div>
        </div>
      </div>
    </div>
  );
}
