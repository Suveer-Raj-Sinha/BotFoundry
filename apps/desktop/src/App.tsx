import { useCallback, useEffect, useState } from "react";
import { loadEngineConfig } from "./api/config";
import { EngineError, fetchHealth, type HealthResponse } from "./api/engine";
import "./status.css";

type State =
  | { kind: "checking" }
  | { kind: "ok"; health: HealthResponse }
  | { kind: "error"; message: string };

const POLL_MS = 5000;

function describeError(err: unknown): string {
  if (err instanceof EngineError) {
    if (err.kind === "unreachable") return "Engine not reachable. Is it running on the expected port?";
    if (err.kind === "unauthorized") return "Engine rejected the token. Check VITE_ENGINE_TOKEN.";
    return err.message;
  }
  return err instanceof Error ? err.message : "Unknown error.";
}

export default function App() {
  const [state, setState] = useState<State>({ kind: "checking" });

  const check = useCallback(async () => {
    try {
      const health = await fetchHealth(loadEngineConfig());
      setState({ kind: "ok", health });
    } catch (err) {
      setState({ kind: "error", message: describeError(err) });
    }
  }, []);

  useEffect(() => {
    check();
    const id = setInterval(check, POLL_MS);
    return () => clearInterval(id);
  }, [check]);

  const cls = state.kind === "ok" ? "ok" : state.kind === "error" ? "err" : "wait";
  const label = state.kind === "ok" ? "Engine connected" : state.kind === "error" ? "Engine offline" : "Checking engine...";

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
            <button onClick={check}>Retry</button>
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
