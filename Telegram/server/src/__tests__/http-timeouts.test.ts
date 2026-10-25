/**
 * Guard for the 10-second cliff (found 2026-09-21).
 *
 * `Bun.serve()` defaults `idleTimeout` to 10s; the MCP SDK's SSE keep-alive
 * defaults to 15s. With both defaults in force, every quiet `/mcp` stream was
 * killed by the socket layer before its first heartbeat and surfaced to clients
 * as a Traefik-synthesized 500 (4617 of them in one week, all with p50 duration
 * 10.34s). Nothing in the behavioural test suite could see it: the handler had
 * already returned 200, and the failure happened below the application.
 *
 * So this guard is deliberately source-level and value-level:
 *  - the two constants must keep a safe ratio (a config-only regression);
 *  - `Bun.serve` must actually be called with the idle timeout, and the MCP
 *    transport with the keep-alive (a wiring-only regression — the constants
 *    could stay perfect while nobody passes them).
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { HTTP_IDLE_TIMEOUT_S, MCP_SSE_KEEP_ALIVE_MS, MIN_IDLE_TO_KEEPALIVE_RATIO } from "../http-timeouts.js";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (f: string) => readFileSync(join(SRC, f), "utf8");

describe("HTTP idle timeout vs SSE keep-alive", () => {
  it("keeps the keep-alive well inside the idle window", () => {
    const idleMs = HTTP_IDLE_TIMEOUT_S * 1000;
    assert.ok(
      idleMs >= MCP_SSE_KEEP_ALIVE_MS * MIN_IDLE_TO_KEEPALIVE_RATIO,
      `idleTimeout ${idleMs}ms must be at least ${MIN_IDLE_TO_KEEPALIVE_RATIO}x the ` +
        `${MCP_SSE_KEEP_ALIVE_MS}ms keep-alive — otherwise Bun reaps live SSE streams ` +
        "and Traefik reports them to clients as 500s",
    );
  });

  it("stays inside Bun's hard ceiling of 255 seconds", () => {
    assert.ok(HTTP_IDLE_TIMEOUT_S > 0 && HTTP_IDLE_TIMEOUT_S <= 255, `got ${HTTP_IDLE_TIMEOUT_S}s`);
    assert.equal(Number.isInteger(HTTP_IDLE_TIMEOUT_S), true, "Bun takes whole seconds");
  });

  it("beats the Bun default that caused the incident", () => {
    // 10s was the silent default. Anything at or below it reopens the cliff.
    assert.ok(HTTP_IDLE_TIMEOUT_S > 10, "idle timeout must exceed Bun's 10s default");
    assert.ok(MCP_SSE_KEEP_ALIVE_MS < 10_000 || HTTP_IDLE_TIMEOUT_S > 15);
  });

  it("is actually wired into Bun.serve", () => {
    const src = read("server.tsx");
    const call = src.match(/Bun\.serve\(\{[^}]*\}\)/s);
    assert.ok(call, "could not find the Bun.serve call in server.tsx");
    assert.match(
      call[0],
      /idleTimeout:\s*HTTP_IDLE_TIMEOUT_S/,
      "Bun.serve must pass idleTimeout: HTTP_IDLE_TIMEOUT_S — the default silently reaps SSE streams",
    );
  });

  it("is actually wired into the MCP transport", () => {
    const src = read("mcp-handler.ts");
    assert.match(
      src,
      /new WebStandardStreamableHTTPServerTransport\(\{[\s\S]*?keepAliveMs:\s*MCP_SSE_KEEP_ALIVE_MS/,
      "the transport must pin keepAliveMs instead of inheriting the SDK default",
    );
  });
});
