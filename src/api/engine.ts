export interface EngineConfig {
  baseUrl: string;
  token: string;
}

export interface HealthResponse {
  status: string;
  engine_version: string;
  python_version: string;
  ai_available: boolean;
}

export type EngineErrorKind = "unreachable" | "unauthorized" | "bad_response";

export class EngineError extends Error {
  readonly kind: EngineErrorKind;

  constructor(kind: EngineErrorKind, message: string) {
    super(message);
    this.name = "EngineError";
    this.kind = kind;
  }
}

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.status === "string" &&
    typeof v.engine_version === "string" &&
    typeof v.python_version === "string" &&
    typeof v.ai_available === "boolean"
  );
}

/** Ask the local engine whether it is alive. Never fabricates a result. */
export async function fetchHealth(
  config: EngineConfig,
  fetchImpl: typeof fetch = fetch,
  timeoutMs = 3000,
): Promise<HealthResponse> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetchImpl(`${config.baseUrl}/health`, {
      headers: { "X-BotFoundry-Token": config.token },
      signal: controller.signal,
    });
  } catch {
    throw new EngineError("unreachable", "Cannot reach the local engine.");
  } finally {
    clearTimeout(timer);
  }

  if (response.status === 401) {
    throw new EngineError("unauthorized", "The engine rejected the token.");
  }
  if (!response.ok) {
    throw new EngineError("bad_response", `Engine returned HTTP ${response.status}.`);
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new EngineError("bad_response", "Engine returned invalid JSON.");
  }
  if (!isHealthResponse(body)) {
    throw new EngineError("bad_response", "Engine response has an unexpected shape.");
  }
  return body;
}
