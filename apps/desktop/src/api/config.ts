import type { EngineConfig } from "./engine";

/**
 * DEV ONLY (Phase 0b step 2): read the engine address and token from Vite env files.
 * In the next step Tauri will provide a random port and token instead.
 */
export function loadEngineConfig(): EngineConfig {
  const baseUrl = import.meta.env.VITE_ENGINE_URL as string | undefined;
  const token = import.meta.env.VITE_ENGINE_TOKEN as string | undefined;
  if (!baseUrl || !token) {
    throw new Error(
      "Missing VITE_ENGINE_URL or VITE_ENGINE_TOKEN. Create .env.development.local (see README).",
    );
  }
  return { baseUrl, token };
}
