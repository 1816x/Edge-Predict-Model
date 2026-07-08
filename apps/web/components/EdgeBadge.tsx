import { formatSignedPts } from "@/lib/format";

/**
 * Minimum edge (in probability points) for a pick to be publishable.
 * MVP default threshold; see docs/05-motor-ev-y-bankroll.md.
 */
const EDGE_THRESHOLD = 0.02;

interface EdgeBadgeProps {
  /** Edge in probability points, 0-1 scale (0.02 = 2%). */
  edge: number;
}

/** Green badge when edge >= 2% (publication threshold), gray otherwise. */
export default function EdgeBadge({ edge }: EdgeBadgeProps) {
  const meetsThreshold = edge >= EDGE_THRESHOLD;
  return (
    <span
      className={`badge ${meetsThreshold ? "badge-green" : "badge-gray"}`}
      title={
        meetsThreshold
          ? "Edge en o por encima del umbral de publicación (2%)"
          : "Edge por debajo del umbral de publicación (2%)"
      }
    >
      {formatSignedPts(edge)}
    </span>
  );
}
