"use client";

import { useState, type FormEvent } from "react";

// User-configurable Kelly fractions. The engine always computes full Kelly;
// the user chooses how much of it to apply (default 1/8, conservative).
const KELLY_FRACTIONS = [
  { label: "Kelly 1/2 (agresivo)", value: 0.5 },
  { label: "Kelly 1/4", value: 0.25 },
  { label: "Kelly 1/8 (default)", value: 0.125 },
  { label: "Kelly 1/16 (muy conservador)", value: 0.0625 },
];

const DEFAULT_KELLY_FRACTION = 0.125;
const DEFAULT_CAP_PCT = 2; // max % of bankroll per pick

export default function BankrollSettingsPage() {
  const [bankroll, setBankroll] = useState("1000");
  const [kellyFraction, setKellyFraction] = useState(DEFAULT_KELLY_FRACTION);
  const [capPct, setCapPct] = useState(String(DEFAULT_CAP_PCT));
  const [saved, setSaved] = useState(false);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // TODO: persist per-user settings via the API (e.g. PUT /api/v1/settings/bankroll)
    // once auth and multi-tenant user settings exist. Local state only for now:
    // values are lost on page reload.
    setSaved(true);
  }

  function markDirty() {
    setSaved(false);
  }

  return (
    <>
      <h1>Bankroll</h1>
      <p className="page-subtitle">
        Configuración de gestión de riesgo. El motor calcula el Kelly completo
        de cada pick; el stake final es{" "}
        <code>bankroll × min(kelly_full × fracción, cap)</code>.
      </p>

      <form className="form" onSubmit={handleSubmit}>
        <div className="form-field">
          <label htmlFor="bankroll">Bankroll (unidades monetarias)</label>
          <input
            id="bankroll"
            type="number"
            min="0"
            step="any"
            value={bankroll}
            onChange={(e) => {
              setBankroll(e.target.value);
              markDirty();
            }}
            required
          />
          <span className="form-hint">
            Dinero destinado exclusivamente a esto y cuya pérdida total puedes
            asumir.
          </span>
        </div>

        <div className="form-field">
          <label htmlFor="kelly-fraction">Fracción de Kelly</label>
          <select
            id="kelly-fraction"
            value={kellyFraction}
            onChange={(e) => {
              setKellyFraction(Number(e.target.value));
              markDirty();
            }}
          >
            {KELLY_FRACTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <span className="form-hint">
            Default 1/8. Fracciones mayores amplifican tanto el crecimiento
            como el drawdown y el impacto de errores de estimación del modelo.
          </span>
        </div>

        <div className="form-field">
          <label htmlFor="cap">Cap por pick (% del bankroll)</label>
          <input
            id="cap"
            type="number"
            min="0.5"
            max="5"
            step="0.25"
            value={capPct}
            onChange={(e) => {
              setCapPct(e.target.value);
              markDirty();
            }}
            required
          />
          <span className="form-hint">
            Límite duro por pick aunque Kelly sugiera más. Recomendado 1–2%
            (default 2%).
          </span>
        </div>

        <button type="submit">Guardar</button>

        {saved && (
          <p className="saved-note">
            Guardado en el estado local de la página. Aún sin persistencia: los
            valores se pierden al recargar.
          </p>
        )}
      </form>
    </>
  );
}
