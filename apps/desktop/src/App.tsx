import { useCallback, useEffect, useRef, useState } from "react";
import { loadEngineConfig } from "./api/config";
import { EngineError, fetchHealth, type HealthResponse } from "./api/engine";
import "./status.css";

type State =
  | { kind: "checking" }
  | { kind: "ok"; health: HealthResponse }
  | { kind: "error"; message: string };

const POLL_OK_MS = 5000; // engine is healthy: check occasionally
const POLL_RETRY_MS = 1000; // not connected yet: check quickly
const STARTUP_GRACE_ATTEMPTS = 10; // show "Checking" (not an error) while the engine boots

function describeError(err: unknown): string {
  if (err instanceof EngineError) {
    if (err.kind === "unreachable") return "Engine not reachable. It may have stopped or failed to start.";
    if (err.kind === "unauthorized") return "Engine rejected the token.";
    return err.message;
  }
  if (typeof err === "string") return err; // message from the Rust launcher
  return err instanceof Error ? err.message : "Unknown error.";
}

export default function App() {
  const [state, setState] = useState<State>({ kind: "checking" });
  const failures = useRef(0);
  const everConnected = useRef(false);

  /** Returns true if the engine answered healthy. */
  const check = useCallback(async (): Promise<boolean> => {
    try {
      const config = await loadEngineConfig();
      const health = await fetchHealth(config);
      everConnected.current = true;
      failures.current = 0;
      setState({ kind: "ok", health });
      return true;
    } catch (err) {
      failures.current += 1;
      const stillBooting =
        err instanceof EngineError &&
        err.kind === "unreachable" &&
        !everConnected.current &&
        failures.current <= STARTUP_GRACE_ATTEMPTS;
      setState(stillBooting ? { kind: "checking" } : { kind: "error", message: describeError(err) });
      return false;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const loop = async () => {
      const healthy = await check();
      if (!cancelled) timer = setTimeout(loop, healthy ? POLL_OK_MS : POLL_RETRY_MS);
    };
    loop();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [check]);

  const cls = state.kind === "ok" ? "ok" : state.kind === "error" ? "err" : "wait";
  const label =
    state.kind === "ok" ? "Engine connected" : state.kind === "error" ? "Engine offline" : "Starting engine...";

  return (
    <main className="shell">
      <h1>BotFoundry</h1>
      <p className="tag">Your tools. Your bots. Your machine. Real work.</p>
      <section className="card" aria-live="polite">
        <div className={`pill ${cls}`}>
          <span className="dot" /> {label}
        </div>
        {state.kind === "error" && (
          <>
            <p className="msg">{state.message}</p>
            <button onClick={() => void check()}>Retry</button>
          </>
        )}
        {state.kind === "ok" && (
          <>
            <div className="row"><span className="label">Engine version</span><span>{state.health.engine_version}</span></div>
            <div className="row"><span className="label">Python</span><span>{state.health.python_version}</span></div>
            <div className="row">
              <span className="label">Local AI</span>
              <span>{state.health.ai_available ? "Available" : "Not configured (optional)"}</span>
            </div>
          </>
        )}
      </section>
    </main>
  );
}
