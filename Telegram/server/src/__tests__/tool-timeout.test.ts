/**
 * issue #19 — the tool path must be bounded, must say so honestly, and must hand the
 * stale session back for a memory-only rebuild.
 *
 * The budgets are read from `config` at call time, so the test sets the env knobs to
 * milliseconds BEFORE importing the modules under test.
 */
process.env.ISSUER ??= "https://tool-timeout-test.invalid";
process.env.TELEGRAM_API_ID ??= "1";
process.env.TELEGRAM_API_HASH ??= "stub";

import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { TelegramService } from "@overpod/mcp-telegram/service";

const { registerAllTools, toolBudgetMs, stageBudgetMs, SLOW_TOOLS } = await import("../tool-registry.js");
const { READ_ONLY, DESTRUCTIVE_PUBLIC: DESTRUCTIVE, textResult } = await import("../tools/helpers.js");
const { config } = await import("../config.js");

/**
 * Budgets are overridden on the config object, NOT through env.
 *
 * `bun test` evaluates every test file in ONE process, so `config.ts` is initialised from
 * whichever file imported it first: setting `TOOL_TIMEOUT_MS` here only worked when this
 * file happened to run first, and silently fell back to the 180s production budget
 * otherwise (which made these tests hang instead of assert). The budget getters read
 * `config` at call time, so mutating it per-test is deterministic — and restoring it keeps
 * the other 700-odd tests in the shared process unaffected.
 */
const ORIGINAL = {
  tool: config.toolTimeoutMs,
  slow: config.toolTimeoutSlowMs,
  connect: config.telegramConnectTimeoutMs,
};

beforeEach(() => {
  config.toolTimeoutMs = 40;
  config.toolTimeoutSlowMs = 400;
  config.telegramConnectTimeoutMs = 30;
});

afterEach(() => {
  config.toolTimeoutMs = ORIGINAL.tool;
  config.toolTimeoutSlowMs = ORIGINAL.slow;
  config.telegramConnectTimeoutMs = ORIGINAL.connect;
});

type Callback = (args: unknown) => Promise<{ content: { type: "text"; text: string }[]; isError?: boolean }>;

const never = () => new Promise<never>(() => {});
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function harness(opts: {
  handler: () => Promise<{ content: { type: "text"; text: string }[]; isError?: boolean }>;
  requireConnection?: () => Promise<string | null>;
  toolName?: string;
  destructive?: boolean;
}) {
  const server = new McpServer({ name: "test", version: "0.0.0" });
  const callbacks = new Map<string, Callback>();
  const original = server.registerTool.bind(server);
  // biome-ignore lint/suspicious/noExplicitAny: SDK signature is overloaded; capture+relay only.
  (server as any).registerTool = (name: string, config: unknown, callback: Callback) => {
    callbacks.set(name, callback);
    return original(name, config as never, callback as never);
  };

  const timeouts: string[] = [];
  const audit: { tool: string; result: string }[] = [];
  registerAllTools(
    server,
    [
      {
        name: opts.toolName ?? "slow-tool",
        description: "test tool",
        annotations: opts.destructive ? DESTRUCTIVE : READ_ONLY,
        handler: opts.handler,
      },
    ],
    {
      getTelegram: () => ({}) as TelegramService,
      requireConnection: opts.requireConnection ?? (async () => null),
      onToolTimeout: (tool: string) => timeouts.push(tool),
      checkDestructive: () => null,
      recordDestructive: (tool: string, _args: unknown, result: "ok" | "error") => audit.push({ tool, result }),
    },
  );

  const name = opts.toolName ?? "slow-tool";
  const callback = callbacks.get(name);
  assert.ok(callback, `tool ${name} was not registered`);
  return { call: () => callback({} as unknown), timeouts, audit };
}

describe("tool deadline (issue #19)", () => {
  it("a handler that never settles returns an error instead of hanging", async () => {
    const { call } = harness({ handler: never });
    const started = Date.now();
    const res = await call();
    const elapsed = Date.now() - started;

    assert.equal(res.isError, true);
    // The whole point: bounded. Without the fix this test would never finish.
    assert.ok(elapsed < 2000, `call took ${elapsed}ms — deadline did not fire`);
  });

  it("the timeout message warns the operation may still have happened", async () => {
    // A deadline does not cancel the Telegram call. If the text implied failure, an agent
    // would happily re-send a message that was already delivered.
    const { call } = harness({ handler: never });
    const text = (await call()).content[0]?.text ?? "";
    assert.match(text, /may still have completed/i);
    assert.match(text, /reconnect/i);
  });

  it("hands the tool name to onToolTimeout so the session can be rebuilt", async () => {
    const { call, timeouts } = harness({ handler: never });
    await call();
    assert.deepEqual(timeouts, ["slow-tool"]);
  });

  it("a hung connection check also times out and triggers the rebuild", async () => {
    const { call, timeouts } = harness({ handler: async () => textResult("unreachable"), requireConnection: never });
    const res = await call();
    assert.equal(res.isError, true);
    assert.deepEqual(timeouts, ["slow-tool"], "connect-stage timeout must mark the session unhealthy too");
  });

  it("a connection check that rejects with a deadline error is not reported as 'not connected'", async () => {
    // mcp-handler rethrows DeadlineError out of requireConnection on purpose; swallowing it
    // would leave the dead client pooled.
    const { call, timeouts } = harness({
      handler: async () => textResult("unreachable"),
      requireConnection: async () => {
        await sleep(5);
        throw Object.assign(new Error("requireConnection exceeded its 40ms deadline"), { name: "DeadlineError" });
      },
    });
    const res = await call();
    assert.equal(res.isError, true);
    assert.deepEqual(timeouts, ["slow-tool"]);
  });

  it("does not fire for a handler that finishes inside its budget", async () => {
    const { call, timeouts } = harness({
      handler: async () => {
        await sleep(5);
        return textResult("done");
      },
    });
    const res = await call();
    assert.equal(res.isError, undefined);
    assert.equal(res.content[0]?.text, "done");
    assert.deepEqual(timeouts, []);
  });

  it("normal handler errors keep their own message and do not count as timeouts", async () => {
    const { call, timeouts } = harness({
      handler: async () => {
        throw new Error("CHAT_NOT_FOUND");
      },
    });
    const res = await call();
    assert.equal(res.isError, true);
    assert.match(res.content[0]?.text ?? "", /CHAT_NOT_FOUND/);
    assert.deepEqual(timeouts, [], "a plain failure must not drop the user's session");
  });

  it("a destructive tool that times out at connect still writes an audit row", async () => {
    // Review finding: `checkDestructive` had already authorised (and rate-charged) the call,
    // but the early return on a connect timeout skipped `recordDestructive` — leaving the
    // audit trail showing an approved destructive action with no outcome.
    const { call, audit } = harness({
      destructive: true,
      handler: async () => textResult("unreachable"),
      requireConnection: never,
    });
    await call();
    assert.deepEqual(audit, [{ tool: "slow-tool", result: "error" }]);
  });

  it("a destructive tool that times out in the handler writes exactly one audit row", async () => {
    const { call, audit } = harness({ destructive: true, handler: never });
    await call();
    assert.deepEqual(audit, [{ tool: "slow-tool", result: "error" }], "no double-counting");
  });

  it("a connect-stage breach falls back to the connect backstop, not the tool budget", async () => {
    // Review finding: `isDeadlineError` also accepts a same-named error from another module
    // instance, which may carry no `timeoutMs`. The fallback used to be the tool budget, so
    // a connect breach was reported as "timed out at connect after 180000ms" and would send
    // an operator to the wrong env knob.
    //
    // The backstop is 3x the connect budget on purpose: `requireConnection` can legitimately
    // spend one budget in ensureActiveSession and another in ensureConnected, and those
    // inner deadlines must win the race so the right account gets marked.
    assert.equal(stageBudgetMs("telegram-read-messages", "connect"), 90, "3 x TELEGRAM_CONNECT_TIMEOUT_MS");
    assert.equal(stageBudgetMs("telegram-read-messages", "handler"), 40);
    assert.equal(stageBudgetMs("telegram-download-media", "connect"), 90, "stage wins over the slow-tool list");
    assert.equal(stageBudgetMs("telegram-download-media", "handler"), 400);
  });

  it("the connect backstop stays disabled when the connect budget is disabled", async () => {
    config.telegramConnectTimeoutMs = 0; // 0 means "no deadline" everywhere else too
    assert.equal(stageBudgetMs("telegram-read-messages", "connect"), 0, "0 must not become 0*3 semantics drift");
  });

  it("byte-moving tools get the long budget, ordinary tools the short one", async () => {
    assert.equal(toolBudgetMs("telegram-read-messages"), 40);
    assert.equal(toolBudgetMs("telegram-download-media"), 400);
    // Guard against the two lists drifting apart silently.
    for (const slow of SLOW_TOOLS) {
      assert.ok(toolBudgetMs(slow) > toolBudgetMs("telegram-read-messages"), `${slow} should use the slow budget`);
    }
  });

  it("uses the slow budget in a real call for a slow tool", async () => {
    // 200ms > TOOL_TIMEOUT_MS (40) but < TOOL_TIMEOUT_SLOW_MS (400): it must survive.
    const { call, timeouts } = harness({
      toolName: "telegram-download-media",
      handler: async () => {
        await sleep(200);
        return textResult("bytes");
      },
    });
    const res = await call();
    assert.equal(res.content[0]?.text, "bytes");
    assert.deepEqual(timeouts, []);
  });
});
