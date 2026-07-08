import Link from "next/link";
import PickCard from "@/components/PickCard";
import { getPick } from "@/lib/api";
import { formatOdds, formatProb, formatSignedPts } from "@/lib/format";
import type { PickDetail } from "@/lib/types";

// Sample detail shown ONLY when the API is unreachable; labeled MOCK in the UI.
function buildMockDetail(id: string): PickDetail {
  return {
    pick_id: id,
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
    audit: {
      odds_at_pick: 2.18,
      model_version: "mlb-ml-0.1.0",
      features: {
        home_sp_xfip_30d: 4.18,
        away_sp_xfip_30d: 3.42,
        away_sp_k_pct_30d: 0.271,
        home_sp_k_pct_30d: 0.198,
        home_bullpen_fip_14d: 4.55,
        away_bullpen_fip_14d: 3.9,
        away_wrc_plus_vs_rhp_30d: 112,
        home_wrc_plus_vs_lhp_30d: 96,
        park_factor_runs: 0.98,
        away_rest_days: 2,
        home_rest_days: 1,
      },
      explanation:
        "El modelo favorece a la visita por la ventaja del abridor en xFIP a 30 días " +
        "(3.42 vs 4.18) y un bullpen local con peor FIP reciente. El precio tomado " +
        "(2.18) implica 45.9%, por debajo de la probabilidad del modelo (47.2%). " +
        "Edge y EV superan los umbrales de publicación; el margen sigue siendo " +
        "estrecho y el resultado individual es incierto.",
      closing_p_fair: null,
      clv: null,
      result: null,
    },
  };
}

interface PickDetailPageProps {
  // Next.js 15: dynamic route params are async.
  params: Promise<{ id: string }>;
}

export default async function PickDetailPage({ params }: PickDetailPageProps) {
  const { id } = await params;

  let pick: PickDetail;
  let isMock = false;

  try {
    pick = await getPick(id);
  } catch {
    pick = buildMockDetail(id);
    isMock = true;
  }

  const { audit } = pick;
  const featureEntries = Object.entries(audit.features);

  return (
    <>
      <Link href="/" className="back-link">
        ← Picks de hoy
      </Link>
      <h1>Detalle del pick</h1>
      <p className="page-subtitle">
        Todo pick es auditable: odds al momento, versión de modelo, snapshot de
        features y, cuando la línea cierra, CLV y resultado.
      </p>

      {isMock && (
        <div className="mock-banner">
          <strong>MOCK</strong>
          <span>
            El API no respondió en <code>NEXT_PUBLIC_API_URL</code>. Este
            detalle es de ejemplo y no corresponde a un partido real.
          </span>
        </div>
      )}

      <div className="detail-section">
        <PickCard pick={pick} linkToDetail={false} />
      </div>

      <section className="detail-section">
        <h2>Audit trail</h2>
        <dl className="kv-list">
          <dt>Odds al momento del pick</dt>
          <dd>
            {formatOdds(audit.odds_at_pick)} ({pick.book})
          </dd>
          <dt>Versión de modelo</dt>
          <dd>
            <code>{audit.model_version}</code>
          </dd>
          <dt>Publicado</dt>
          <dd>{pick.created_at}</dd>
          <dt>Kelly completo</dt>
          <dd>{formatSignedPts(pick.kelly_full)}</dd>
        </dl>
      </section>

      <section className="detail-section">
        <h2>Features clave (snapshot as-of)</h2>
        <p className="note">
          Valores disponibles antes del primer pitch, tal como los recibió el
          modelo. Ver <code>docs/04-features-y-modelos.md</code>.
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Feature</th>
                <th className="num">Valor</th>
              </tr>
            </thead>
            <tbody>
              {featureEntries.map(([name, value]) => (
                <tr key={name}>
                  <td>
                    <code>{name}</code>
                  </td>
                  <td className="num">{String(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="detail-section">
        <h2>Explicación</h2>
        <p>{audit.explanation}</p>
        <p className="note">
          La explicación la redacta la capa LLM a partir de features
          estructuradas. Las probabilidades provienen exclusivamente del modelo
          estadístico calibrado, nunca del LLM.
        </p>
      </section>

      <section className="detail-section">
        <h2>CLV</h2>
        {audit.clv !== null && audit.closing_p_fair !== null ? (
          <dl className="kv-list">
            <dt>Cierre no-vig (Pinnacle)</dt>
            <dd>{formatProb(audit.closing_p_fair)}</dd>
            <dt>CLV</dt>
            <dd>{formatSignedPts(audit.clv)} en probabilidad</dd>
          </dl>
        ) : (
          <p className="note">
            Pendiente: el CLV se calcula contra el cierre no-vig de Pinnacle
            cuando la línea cierra.
          </p>
        )}
      </section>
    </>
  );
}
