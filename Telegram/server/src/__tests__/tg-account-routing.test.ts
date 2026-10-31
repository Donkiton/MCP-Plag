process.env.ISSUER ??= "https://tg-routing.invalid";
process.env.TELEGRAM_API_ID ??= "1";
process.env.TELEGRAM_API_HASH ??= "stub";
process.env.MCP_TELEGRAM_TELEMETRY ??= "off";

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import type { TelegramService } from "@overpod/mcp-telegram/service";

const { SessionManager } = await import("../session-manager.js");
const { registerAllTools } = await import("../tool-registry.js");
const { ACCOUNTS_TOOLS } = await import("../tools/accounts.js");
const { selectAccountTargets } = await import("../account-routing.js");
const { config } = await import("../config.js");

class StubTelegram {
  identity = "";
  connected = false;
  lastError = "";
  setSessionString(value: string) {
    this.identity = value;
  }
  getSessionString() {
    return this.identity;
  }
  isConnected() {
    return this.connected;
  }
  async connect() {
    this.connected = this.identity !== "offline";
    return this.connected;
  }
  async ensureConnected() {
    return this.connected;
  }
  async disconnect() {
    this.connected = false;
  }
  markUnhealthy() {
    this.connected = false;
  }
}

function setup() {
  const manager = new SessionManager(":memory:", () => new StubTelegram() as unknown as TelegramService);
  manager.saveSessionString("10001", "personal");
  manager.renameAccount("10001", 0, "Личный");
  const work = manager.addAccount("10001", "20002", "work", "Рабочий");
  return { manager, work };
}

type Callback = (args: unknown) => Promise<CallToolResult>;
function client(manager: InstanceType<typeof SessionManager>, owner = "10001") {
  const callbacks = new Map<string, Callback>();
  const definitions = new Map<string, Record<string, unknown>>();
  const server = {
    registerTool(name: string, definition: Record<string, unknown>, callback: Callback) {
      callbacks.set(name, callback);
      definitions.set(name, definition);
    },
  } as unknown as McpServer;
  const effects: string[] = [];
  const readOnly = { readOnlyHint: true, destructiveHint: false, openWorldHint: false };
  const handler = async (_args: unknown, deps: { telegram: TelegramService }) => {
    const identity = (deps.telegram as unknown as StubTelegram).identity;
    await Promise.resolve(); // concurrent switch/rename must not change this call's client
    effects.push(identity);
    return { content: [{ type: "text" as const, text: identity }] };
  };
  registerAllTools(
    server,
    [
      { name: "telegram-probe-read", description: "read", annotations: readOnly, handler },
      {
        name: "telegram-probe-send",
        description: "send",
        annotations: { ...readOnly, readOnlyHint: false, openWorldHint: true },
        handler,
      },
      ...ACCOUNTS_TOOLS,
    ],
    {
      userId: owner,
      sessions: manager,
      getTelegram: () => {
        throw new Error("Global active account lookup must not be used");
      },
      requireConnection: async () => {
        throw new Error("Global active account connection must not be used");
      },
      onSessionRevoked: async () => {
        throw new Error("Global user revocation must not be used");
      },
    },
  );
  return {
    call: (name: string, args: unknown = {}) => {
      const callback = callbacks.get(name);
      assert.ok(callback);
      return callback(args);
    },
    effects,
    definitions,
  };
}
const textOf = (result: CallToolResult) => result.content.map((c) => (c.type === "text" ? c.text : "")).join("\n");

describe("@tg account identity and lifecycle", () => {
  it("renames primary/secondary without changing IDs, rejects label collisions and prefix-parsed IDs", () => {
    const { manager, work } = setup();
    manager.renameAccount("10001", work, "Общий");
    assert.equal(manager.resolveAccount("10001", "личный")?.accountId, 0);
    assert.equal(manager.resolveAccount("10001", "общий")?.accountId, work);
    assert.equal(manager.resolveAccount("10001", `${work}garbage`), null);
    assert.throws(() => manager.renameAccount("10001", work, "ЛИЧНЫЙ"), /already uses/);
    assert.throws(() => manager.renameAccount("10001", work, "all"), /cannot/);
    assert.throws(() => manager.renameAccount("other-owner", work, "Other"), /not found/);
    manager.close();
  });

  it("disconnects primary while preserving owner identity and the secondary's live session", async () => {
    const { manager, work } = setup();
    const secondaryClient = await manager.ensureAccountSession("10001", work);
    assert.equal(manager.disconnectAccount("10001", 0), true);
    assert.deepEqual(manager.getSavedUserIds(), ["10001"]);
    assert.deepEqual(
      manager.listAccounts("10001").map((a) => a.accountId),
      [work],
    );
    assert.equal(await manager.ensureAccountSession("10001", work), secondaryClient);
    assert.equal(manager.getAccountAttachmentId("10001", 0), null);
    await assert.rejects(manager.ensureAccountSession("10001", 0), /disconnected/);
    manager.close();
  });

  it("re-adding the primary does not duplicate it as a secondary", () => {
    const { manager } = setup();
    assert.equal(manager.addAccount("10001", "10001", "fresh-primary", "Домашний"), 0);
    assert.equal(manager.listAccounts("10001").length, 2);
    assert.equal(manager.listAccounts("10001")[0].label, "Домашний");
    manager.close();
  });
});

describe("@tg explicit per-call account routing", () => {
  it("rejects implicit multi-account writes and all-account writes before any handler runs", async () => {
    const { manager } = setup();
    const a = client(manager);
    assert.equal((await a.call("telegram-probe-send")).isError, true);
    assert.equal((await a.call("telegram-probe-send", { account: "all" })).isError, true);
    assert.equal((await a.call("telegram-probe-send", { account: "Личный", account_id: 0 })).isError, true);
    assert.deepEqual(a.effects, []);
    manager.close();
  });

  it("captures the sender independently for simultaneous ChatGPT/Codex writes", async () => {
    const { manager, work } = setup();
    const chatgpt = client(manager);
    const codex = client(manager);
    const [personalResult, workResult] = await Promise.all([
      chatgpt.call("telegram-probe-send", { account: "Личный" }),
      codex.call("telegram-probe-send", { account_id: work }),
    ]);
    assert.match(textOf(personalResult), /personal/);
    assert.match(textOf(workResult), /work/);
    assert.deepEqual(chatgpt.effects, ["personal"]);
    assert.deepEqual(codex.effects, ["work"]);
    manager.close();
  });

  it("makes switch a local read preference without changing another client or implicit writes", async () => {
    const { manager } = setup();
    const a = client(manager);
    const b = client(manager);
    await a.call("telegram-accounts-switch", { identifier: "Рабочий" });
    assert.match(textOf(await a.call("telegram-probe-read")), /work/);
    assert.match(textOf(await b.call("telegram-probe-read")), /personal/);
    assert.equal(manager.getActiveAccountId("10001"), 0);
    assert.equal((await a.call("telegram-probe-send")).isError, true);
    manager.close();
  });

  it("tags fanout output and retains successful accounts when another account is offline", async () => {
    const { manager } = setup();
    manager.addAccount("10001", "30003", "offline", "Недоступный");
    const a = client(manager);
    const result = await a.call("telegram-probe-read", { account: "all" });
    assert.notEqual(result.isError, true);
    assert.match(textOf(result), /Личный/);
    assert.match(textOf(result), /Рабочий/);
    assert.match(textOf(result), /Недоступный/);
    assert.match(textOf(result), /not connected/);
    assert.deepEqual(a.effects, ["personal", "work"]);
    manager.close();
  });

  it("never routes to another owner's accounts or to a removed sender", async () => {
    const { manager, work } = setup();
    const a = client(manager, "other-owner");
    assert.equal((await a.call("telegram-probe-send", { account_id: work })).isError, true);
    manager.disconnectAccount("10001", work);
    assert.throws(() => selectAccountTargets(manager, "10001", { account_id: work }, false), /not found/);
    assert.deepEqual(a.effects, []);
    manager.close();
  });

  it("a secondary connection timeout marks that captured client, leaving the primary healthy", async () => {
    const { manager, work } = setup();
    const primary = await manager.ensureAccountSession("10001", 0);
    const secondary = await manager.ensureAccountSession("10001", work);
    secondary.ensureConnected = () => new Promise<boolean>(() => {});
    const timeout = config.telegramConnectTimeoutMs;
    config.telegramConnectTimeoutMs = 10;
    try {
      const result = await client(manager).call("telegram-probe-send", { account_id: work });
      assert.equal(result.isError, true);
      assert.equal(primary.isConnected(), true);
      assert.equal(secondary.isConnected(), false);
    } finally {
      config.telegramConnectTimeoutMs = timeout;
      manager.close();
    }
  });

  it("advertises custom PNG icons and explicit OAuth metadata on every tool", () => {
    const { manager } = setup();
    const a = client(manager);
    for (const definition of a.definitions.values()) {
      assert.deepEqual(definition.securitySchemes, [{ type: "oauth2", scopes: [] }]);
      const icons = definition.icons as { src: string; mimeType: string }[];
      assert.equal(icons[0].mimeType, "image/png");
      assert.match(icons[0].src, /icon-256\.png/);
    }
    manager.close();
  });
});
