import { getPerformance } from "@/lib/api";
import { formatProb, formatSignedPts } from "@/lib/format";
import type { Performance } from "@/lib/types";

// Sample metrics shown ONLY when the API is unreachable; labeled MOCK in the UI.
const MOCK_PERFORMANCE: Performance = {
  roi: 0.031,
  yield: 0.018,
  clv_beat_rate: 0.54,
  ece: 0.021,
  brier: 0.247,
  drawdown: 4.8,
  units: 3.2,
  n_picks: 87,
};

function formatUnits(x: number): string {
  return `${x >= 0 ? "+" : ""}${x.toFixed(1)} u`;
}

export default async function PerformancePage() {
  let perf: Performance;
  let isMock = false;

  try {
    perf = await getPerformance();
  } catch {
    perf = MOCK_PERFORMANCE;
    isMock = true;
  }

  return (
    <>
      <h1>Performance</h1>
      <p className="page-subtitle">
        Métricas agregadas sobre picks registrados (paper trading y producción).
        Esta página <strong>no</strong> incluye resultados de backtests: el
        backtest se documenta por separado y no se mezcla con el track record.
      </p>

      {isMock && (
        <div className="mock-banner">
          <strong>MOCK</strong>
          <span>
            El API no respondió en <code>NEXT_PUBLIC_API_URL</code>. Métricas de
            ejemplo, no representan resultados reales.
          </span>
        </div>
      )}

      <div className="metrics-grid">
        <div className="metric-card">
          <div className="metric-label">ROI</div>
          <div className="metric-value">{formatSignedPts(perf.roi)}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Yield</div>
          <div className="metric-value">{formatSignedPts(perf.yield)}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">CLV beat-rate</div>
          <div className="metric-value">{formatProb(perf.clv_beat_rate)}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">ECE</div>
          <div className="metric-value">{perf.ece.toFixed(3)}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Brier score</div>
          <div className="metric-value">{perf.brier.toFixed(3)}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Drawdown máximo</div>
          <div className="metric-value">{perf.drawdown.toFixed(1)} u</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Units netas</div>
          <div className="metric-value">{formatUnits(perf.units)}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">n_picks</div>
          <div className="metric-value">{perf.n_picks}</div>
        </div>
      </div>

      <h2>Calibration curve</h2>
      <div className="placeholder-box">
        Placeholder: curva de calibración (probabilidad predicha vs frecuencia
        observada, por bins). Pendiente de implementación.
      </div>

      <h2>Drawdown</h2>
      <div className="placeholder-box">
        Placeholder: curva de equity en units y drawdown acumulado. Pendiente de
        implementación.
      </div>

      <h2>Notas de lectura</h2>
      <p className="note">
        Yield = ganancia neta / total apostado. ROI = ganancia neta / bankroll
        inicial del periodo. No son intercambiables. El winrate no aparece como
        métrica principal a propósito: un modelo puede ganar menos del 50% de
        sus picks y ser rentable (underdogs), o ganar más del 55% y perder
        dinero (precios malos). La calidad del proceso se lee en calibración
        (ECE, Brier) y CLV; la rentabilidad, en yield/ROI con su n_picks al
        lado. Con muestras pequeñas, todas estas métricas tienen alta varianza.
      </p>
    </>
  );
}
