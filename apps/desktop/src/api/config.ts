import type { EngineConfig } from "./engine";

/**
 * Where does the app get the engine address and token?
 *  - Inside the Tauri window: from the Rust launcher (random port + random token each launch).
 *  - In a plain browser (`npm run dev`): from .env.development.local, for manual dev only.
 */
export async function loadEngineConfig(): Promise<EngineConfig> {
  if (typeof window !== "undefined" && "__TAURI_INTERNALS__" in window) {
    const { invoke } = await import("@tauri-apps/api/core");
    // Rejects with a plain string if the launcher could not start the engine.
    return await invoke<EngineConfig>("get_engine_info");
  }

  const baseUrl = import.meta.env.VITE_ENGINE_URL as string | undefined;
  const token = import.meta.env.VITE_ENGINE_TOKEN as string | undefined;
  if (!baseUrl || !token) {
    throw new Error(
      "Not running inside the desktop app, and VITE_ENGINE_URL / VITE_ENGINE_TOKEN are not set.",
    );
  }
  return { baseUrl, token };
}
