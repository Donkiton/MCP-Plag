process.env.ISSUER ??= "https://tools-accounts-test.invalid";
process.env.TELEGRAM_API_ID ??= "1";
process.env.TELEGRAM_API_HASH ??= "stub";
process.env.MCP_TELEGRAM_TELEMETRY ??= "off";

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { ToolDeps } from "../tool-registry.js";

const { SessionManager } = await import("../session-manager.js");
const { ACCOUNTS_TOOLS } = await import("../tools/accounts.js");
const { CLOUD_ONLY } = await import("../parity-config.js");

function setup() {
  const sessions = new SessionManager(":memory:");
  sessions.saveSessionString("10001", "primary-key");
  const work = sessions.addAccount("10001", "20002", "work-key", "Рабочий");
  let selected: number | undefined;
  const deps = {
    sessions,
    userId: "10001",
    baseUrl: "https://tg.example.com",
    get selectedAccountId() {
      return selected;
    },
    selectAccount: (id: number) => {
      selected = id;
    },
  } as ToolDeps;
  const call = (name: string, args: Record<string, unknown> = {}) => {
    const tool = ACCOUNTS_TOOLS.find((tool) => tool.name === name);
    assert.ok(tool);
    return tool.handler(args, deps);
  };
  return { sessions, work, deps, call };
}
const text = (r: Awaited<ReturnType<ReturnType<typeof setup>["call"]>>) =>
  r.content.map((c) => (c.type === "text" ? c.text : "")).join("\n");

describe("@tg account control tools", () => {
  it("declares six connection-management tools with deliberate annotations and no Telegram connection requirement", () => {
    assert.equal(ACCOUNTS_TOOLS.length, 6);
    for (const tool of ACCOUNTS_TOOLS) {
      assert.equal(tool.skipRequireConnection, true);
      assert.ok(CLOUD_ONLY.some((entry) => entry.name === tool.name));
      assert.equal(tool.annotations.openWorldHint, false);
      assert.equal(
        tool.annotations.readOnlyHint,
        ["telegram-accounts-list", "telegram-accounts-current"].includes(tool.name),
      );
    }
  });
  it("lists stable IDs and names", async () => {
    const s = setup();
    const payload = JSON.parse(text(await s.call("telegram-accounts-list")));
    assert.deepEqual(
      payload.accounts.map((a: { account_id: number }) => a.account_id),
      [0, s.work],
    );
    assert.equal(payload.accounts[1].name, "Рабочий");
    s.sessions.close();
  });
  it("switches only the supplied MCP preference, leaving global state unchanged", async () => {
    const s = setup();
    await s.call("telegram-accounts-switch", { identifier: "Рабочий" });
    assert.equal(s.deps.selectedAccountId, s.work);
    assert.equal(s.sessions.getActiveAccountId("10001"), 0);
    const current = JSON.parse(text(await s.call("telegram-accounts-current")));
    assert.equal(current.account_id, s.work);
    s.sessions.close();
  });
  it("generates an owner-bound one-use add link with the requested label", async () => {
    const s = setup();
    const message = text(await s.call("telegram-accounts-add", { label: "Общий" }));
    const token = message.match(/\/accounts\/add\/([a-f0-9]+)/)?.[1];
    assert.ok(token);
    assert.deepEqual(s.sessions.consumeAddAccountToken(token), { ownerUserId: "10001", label: "Общий" });
    assert.equal(s.sessions.consumeAddAccountToken(token), null);
    s.sessions.close();
  });
  it("renames the primary without changing its ID", async () => {
    const s = setup();
    await s.call("telegram-accounts-rename", { identifier: "primary", label: "Личный" });
    assert.equal(s.sessions.resolveAccount("10001", "Личный")?.accountId, 0);
    s.sessions.close();
  });
  it("disconnects a primary account without deleting the secondary", async () => {
    const s = setup();
    const result = await s.call("telegram-accounts-remove", { identifier: "primary" });
    assert.notEqual(result.isError, true);
    assert.deepEqual(
      s.sessions.listAccounts("10001").map((a) => a.accountId),
      [s.work],
    );
    assert.ok(s.sessions.getSavedUserIds().includes("10001"));
    s.sessions.close();
  });
  it("disconnects a secondary without changing the primary", async () => {
    const s = setup();
    await s.call("telegram-accounts-remove", { identifier: "Рабочий" });
    assert.deepEqual(
      s.sessions.listAccounts("10001").map((a) => a.accountId),
      [0],
    );
    s.sessions.close();
  });
  it("rejects unknown accounts and missing authenticated identity", async () => {
    const s = setup();
    assert.equal((await s.call("telegram-accounts-remove", { identifier: "missing" })).isError, true);
    const result = await ACCOUNTS_TOOLS[0].handler({}, {} as ToolDeps);
    assert.equal(result.isError, true);
    s.sessions.close();
  });
});
