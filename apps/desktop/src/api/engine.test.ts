import { describe, expect, it } from "vitest";
import { EngineError, fetchHealth } from "./engine";

const config = { baseUrl: "http://127.0.0.1:8765", token: "t" };
const okBody = { status: "ok", engine_version: "0.1.0", python_version: "3.11.9", ai_available: false };

function fakeFetch(res: Response | Error): typeof fetch {
  return (async () => {
    if (res instanceof Error) throw res;
    return res;
  }) as unknown as typeof fetch;
}

describe("fetchHealth", () => {
  it("returns the health payload on success", async () => {
    const result = await fetchHealth(config, fakeFetch(Response.json(okBody)));
    expect(result.status).toBe("ok");
    expect(result.ai_available).toBe(false);
  });

  it("sends the token header", async () => {
    let seen: Headers | undefined;
    const spy = (async (_url: unknown, init?: RequestInit) => {
      seen = new Headers(init?.headers);
      return Response.json(okBody);
    }) as unknown as typeof fetch;
    await fetchHealth(config, spy);
    expect(seen?.get("X-BotFoundry-Token")).toBe("t");
  });

  it("maps 401 to unauthorized", async () => {
    await expect(fetchHealth(config, fakeFetch(new Response("{}", { status: 401 })))).rejects.toMatchObject({
      kind: "unauthorized",
    });
  });

  it("maps network failure to unreachable", async () => {
    await expect(fetchHealth(config, fakeFetch(new TypeError("fail")))).rejects.toMatchObject({
      kind: "unreachable",
    });
  });

  it("rejects an unexpected response shape", async () => {
    await expect(fetchHealth(config, fakeFetch(Response.json({ status: "ok" })))).rejects.toBeInstanceOf(EngineError);
  });

  it("rejects non-JSON bodies", async () => {
    await expect(fetchHealth(config, fakeFetch(new Response("nope", { status: 200 })))).rejects.toMatchObject({
      kind: "bad_response",
    });
  });
});
