/**
 * Socket-level timeouts for the HTTP server and the MCP SSE streams that live on it.
 *
 * WHY THIS FILE EXISTS (measured 2026-09-21, one month of production data):
 * `Bun.serve()` defaults `idleTimeout` to 10 SECONDS, while the MCP SDK's
 * streamable-HTTP transport sends its SSE keep-alive comment every 15 seconds
 * (`DEFAULT_SSE_KEEP_ALIVE_MS`). The heartbeat therefore never arrived in time:
 * Bun killed every quiet `/mcp` stream at the 10s mark, Traefik saw the upstream
 * connection drop mid-response and synthesized a 500 "Internal Server Error"
 * (21-byte body) for the client. Evidence: 4617 `GET /mcp` 500s in 7 days with
 * p50 duration 10.34s / max 12.08s, against p50 0.15s for clean 200s — a flat
 * cliff at the default, not a load effect. Clients reconnected, so nothing was
 * user-visible, but ~19% of all notification streams churned and the noise
 * masked real failures.
 *
 * THE INVARIANT: the keep-alive must fire comfortably before the socket is
 * considered idle. Both numbers are pinned here rather than inherited — an SDK
 * bump that changes its default must not be able to reintroduce the cliff, and
 * `src/__tests__/http-timeouts.test.ts` fails if the margin is ever lost.
 */

/**
 * Bun socket idle timeout, in SECONDS (Bun's unit; hard maximum is 255).
 * Chosen well above the keep-alive so a stream only dies when the peer is
 * genuinely gone, not because it had nothing to say for a while.
 */
export const HTTP_IDLE_TIMEOUT_S = 120;

/**
 * Interval between SSE keep-alive comment frames, in MILLISECONDS. Passed
 * explicitly to every MCP transport so the value is ours, not the SDK's default.
 */
export const MCP_SSE_KEEP_ALIVE_MS = 20_000;

/**
 * Minimum ratio between the idle timeout and the keep-alive interval. At 3x a
 * stream survives two consecutive lost/late heartbeats before Bun reaps it —
 * enough slack for event-loop stalls under load, which a 1.5x margin would not
 * survive.
 */
export const MIN_IDLE_TO_KEEPALIVE_RATIO = 3;
