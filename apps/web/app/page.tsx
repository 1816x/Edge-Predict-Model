import Link from "next/link";
import EdgeBadge from "@/components/EdgeBadge";
import { getTodayPicks } from "@/lib/api";
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

// Sample data shown ONLY when the API is unreachable. Clearly labeled as MOCK
// in the UI. Numbers are internally consistent with the canonical formulas
// (edge = p_model - p_fair; EV = p*(odds-1) - (1-p); stake = min(f*/8, 2%)).
const MOCK_PICKS: Pick[] = [
  {
    pick_id: "mock-1",
    event: {
      sport: "mlb",
      home_team: "Boston Red Sox",
      away_team: "New York Yankees",
      start_time: "2026-07-07T23:10:00Z",
    },
    market: "ml",
    selection: "New York Yankees",
    book: "bet365",
    price: 2.18,
    p_model: 0.472,
    p_fair: 0.448,
    edge: 0.024,
    ev_per_unit: 0.029,
    kelly_full: 0.025,
    stake_suggested: 0.0031,
    status: "pending",
    model_version: "mlb-ml-0.1.0",
    created_at: "2026-07-07T14:05:00Z",
  },
  {
    pick_id: "mock-2",
    event: {
      sport: "mlb",
      home_team: "San Diego Padres",
      away_team: "Los Angeles Dodgers",
      start_time: "2026-07-08T02:40:00Z",
    },
    market: "f5_ml",
    selection: "Los Angeles Dodgers",
    book: "caliente",
    price: 1.95,
    p_model: 0.545,
    p_fair: 0.522,
    edge: 0.023,
    ev_per_unit: 0.028,
    kelly_full: 0.066,
    stake_suggested: 0.0083,
    status: "pending",
    model_version: "mlb-f5-0.1.0",
    created_at: "2026-07-07T14:05:00Z",
  },
  {
    pick_id: "mock-3",
    event: {
      sport: "mlb",
      home_team: "Seattle Mariners",
      away_team: "Houston Astros",
      start_time: "2026-07-08T01:40:00Z",
    },
    market: "ml",
    selection: "Seattle Mariners",
    book: "bet365",
    price: 2.35,
    p_model: 0.442,
    p_fair: 0.417,
    edge: 0.025,
    ev_per_unit: 0.039,
    kelly_full: 0.029,
    stake_suggested: 0.0036,
    status: "pending",
    model_version: "mlb-ml-0.1.0",
    created_at: "2026-07-07T14:05:00Z",
  },
];

export default async function TodayPicksPage() {
  let picks: Pick[];
  let isMock = false;

  try {
    picks = await getTodayPicks();
  } catch {
    // API unreachable (e.g. apps/api not running): fall back to labeled mocks.
    picks = MOCK_PICKS;
    isMock = true;
  }

  return (
    <>
      <h1>Picks de hoy</h1>
      <p className="page-subtitle">
        Value bets publicados por el scan diario del slate de MLB. Se publica un
        pick solo si edge ≥ 2%, EV ≥ +2% y el modelo está calibrado (ECE ≤ 0.03
        en ventana rolling de 60 días).
      </p>

      {isMock && (
        <div className="mock-banner">
          <strong>MOCK</strong>
          <span>
            El API no respondió en <code>NEXT_PUBLIC_API_URL</code>. Estos datos
            son de ejemplo y no corresponden a partidos reales.
          </span>
        </div>
      )}

      {picks.length === 0 ? (
        <p className="note">
          Sin picks hoy: ningún mercado del slate superó los umbrales de
          publicación. No publicar también es un resultado correcto.
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Evento</th>
                <th>Mercado</th>
                <th>Book</th>
                <th className="num">Precio</th>
                <th className="num">p_model</th>
                <th className="num">p_fair</th>
                <th className="num">Edge</th>
                <th className="num">EV</th>
                <th className="num">Stake sugerido</th>
                <th>Estado</th>
              </tr>
            </thead>
            <tbody>
              {picks.map((pick) => (
                <tr key={pick.pick_id}>
                  <td>
                    <Link href={`/picks/${pick.pick_id}`}>
                      {formatEventLabel(pick.event)}
                    </Link>
                    <div className="note">{pick.selection}</div>
                  </td>
                  <td>{MARKET_LABELS[pick.market]}</td>
                  <td>{pick.book}</td>
                  <td className="num">{formatOdds(pick.price)}</td>
                  <td className="num">{formatProb(pick.p_model)}</td>
                  <td className="num">{formatProb(pick.p_fair)}</td>
                  <td className="num">
                    <EdgeBadge edge={pick.edge} />
                  </td>
                  <td className="num">{formatSignedPts(pick.ev_per_unit)}</td>
                  <td className="num">{formatStake(pick.stake_suggested)}</td>
                  <td>
                    <span className="status-tag">
                      {STATUS_LABELS[pick.status]}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="note" style={{ marginTop: "0.9rem" }}>
        p_fair es la probabilidad sin vig derivada de la línea de referencia
        (Pinnacle). El stake sugerido usa Kelly fraccional (default 1/8) con cap
        del 2% del bankroll; configúralo en{" "}
        <Link href="/settings/bankroll">Bankroll</Link>.
      </p>
    </>
  );
}
