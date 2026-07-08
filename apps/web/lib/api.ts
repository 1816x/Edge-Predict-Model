// Typed client for the EDGE API v1 (FastAPI backend in apps/api).
// Base URL comes from NEXT_PUBLIC_API_URL (see .env.example).
//
// API surface v1:
//   POST /api/v1/analyze
//   GET  /api/v1/picks/today
//   GET  /api/v1/picks/{pick_id}
//   GET  /api/v1/performance
//   GET  /health

import type {
  Analysis,
  AnalyzeRequest,
  HealthResponse,
  Performance,
  Pick,
  PickDetail,
} from "@/lib/types";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** Fail fast when the API is unreachable so pages can fall back to mock data. */
const REQUEST_TIMEOUT_MS = 4000;

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json" },
    // Pick data changes intraday: never serve a cached response.
    cache: "no-store",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!res.ok) {
    throw new ApiError(res.status, `API request failed: ${res.status} ${path}`);
  }
  return (await res.json()) as T;
}

/** POST /api/v1/analyze — on-demand analysis of a single game/market. */
export function analyze(body: AnalyzeRequest): Promise<Analysis> {
  return request<Analysis>("/api/v1/analyze", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** GET /api/v1/picks/today — value bets published by the daily slate scan. */
export function getTodayPicks(): Promise<Pick[]> {
  return request<Pick[]>("/api/v1/picks/today");
}

/** GET /api/v1/picks/{pick_id} — single pick with its full audit trail. */
export function getPick(pickId: string): Promise<PickDetail> {
  return request<PickDetail>(`/api/v1/picks/${encodeURIComponent(pickId)}`);
}

/** GET /api/v1/performance — aggregated metrics over registered picks. */
export function getPerformance(): Promise<Performance> {
  return request<Performance>("/api/v1/performance");
}

/** GET /health — API liveness probe. */
export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}
