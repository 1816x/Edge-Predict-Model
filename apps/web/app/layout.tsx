import type { Metadata } from "next";
import type { ReactNode } from "react";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "EDGE — Dashboard",
  description:
    "Herramienta informativa de análisis cuantitativo para apuestas deportivas (MVP: MLB Moneyline y F5 Moneyline).",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="es">
      <body>
        <header className="site-header">
          <div className="site-header-inner">
            <span className="brand">EDGE</span>
            <nav className="site-nav" aria-label="Navegación principal">
              <Link href="/">Picks de hoy</Link>
              <Link href="/performance">Performance</Link>
              <Link href="/settings/bankroll">Bankroll</Link>
            </nav>
          </div>
        </header>
        <main className="site-main">{children}</main>
        {/* Sticky footer: the disclaimer must stay visible on every page. */}
        <footer className="site-footer">
          <p>
            EDGE es una herramienta informativa y educativa. No constituye
            asesoría financiera ni recomendación de inversión. Ningún modelo,
            edge o EV garantiza resultados: apostar implica riesgo de pérdida.
            Solo para mayores de 18 años. Juega con responsabilidad.
          </p>
        </footer>
      </body>
    </html>
  );
}
