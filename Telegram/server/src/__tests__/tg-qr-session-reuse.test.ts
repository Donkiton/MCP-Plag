process.env.ISSUER ??= "https://tg-qr-reuse.invalid";
process.env.TELEGRAM_API_ID ??= "1";
process.env.TELEGRAM_API_HASH ??= "stub";
process.env.MCP_TELEGRAM_TELEMETRY ??= "off";

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { TelegramService } from "@overpod/mcp-telegram/service";

const { retainOrAttachAccount } = await import("../qr-login.js");

type Store = Parameters<typeof retainOrAttachAccount>[0];
function fixture(options: { existing?: boolean; healthy?: boolean; rejectAdd?: boolean } = {}) {
  const events: string[] = [];
  const fresh = {
    async disconnect() {
      events.push("fresh.disconnect");
    },
    async logOut() {
      events.push("fresh.logOut");
      return true;
    },
  } as unknown as TelegramService;
  const store = {
    listAccounts: () => (options.existing ? [{ accountId: 7, telegramUserId: "20002", label: "Рабочий" }] : []),
    async ensureAccountSession() {
      events.push("existing.connect");
      return {
        async ensureConnected() {
          return options.healthy !== false;
        },
      };
    },
    renameAccount(_owner: string, _id: number, label: string) {
      events.push(`rename:${label}`);
    },
    async replaceAccountSession(_owner: string, id: number, key: string) {
      events.push(`replace:${id}:${key}`);
    },
    addAccount(_owner: string, _id: string, key: string) {
      events.push(`add:${key}`);
      if (options.rejectAdd) throw new Error("Invalid name");
      return 9;
    },
  } as unknown as Store;
  return { fresh, store, events };
}

describe("@tg QR linking preserves one stored session per account", () => {
  it("healthy repeated scan keeps the stored device and ends the fresh sign-in-only device", async () => {
    const f = fixture({ existing: true });
    assert.equal(await retainOrAttachAccount(f.store, "10001", "20002", f.fresh, "new-key", "Общий"), 7);
    assert.deepEqual(f.events, ["existing.connect", "rename:Общий", "fresh.logOut", "fresh.disconnect"]);
  });
  it("repairs a dead key without changing account ID and closes the QR connection before publishing its key", async () => {
    const f = fixture({ existing: true, healthy: false });
    assert.equal(await retainOrAttachAccount(f.store, "10001", "20002", f.fresh, "new-key", null), 7);
    assert.deepEqual(f.events, ["existing.connect", "fresh.disconnect", "replace:7:new-key"]);
  });
  it("closes a new account's QR connection before saving its reusable key", async () => {
    const f = fixture();
    assert.equal(await retainOrAttachAccount(f.store, "10001", "30003", f.fresh, "new-key", "Общий"), 9);
    assert.deepEqual(f.events, ["fresh.disconnect", "add:new-key"]);
    assert.ok(!f.events.includes("fresh.logOut"), "the accepted authorization must remain valid");
  });
  it("does not publish the key while temporary disconnect is still pending", async () => {
    const f = fixture();
    let finish!: () => void;
    f.fresh.disconnect = () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      });
    const work = retainOrAttachAccount(f.store, "10001", "30003", f.fresh, "new-key", null);
    await Promise.resolve();
    assert.deepEqual(f.events, []);
    finish();
    await work;
    assert.deepEqual(f.events, ["add:new-key"]);
  });
  it("ends the rejected QR authorization when validation/persistence fails", async () => {
    const f = fixture({ rejectAdd: true });
    await assert.rejects(retainOrAttachAccount(f.store, "10001", "30003", f.fresh, "new-key", "bad"), /Invalid name/);
    assert.deepEqual(f.events, ["fresh.disconnect", "add:new-key", "fresh.logOut", "fresh.disconnect"]);
  });
});
