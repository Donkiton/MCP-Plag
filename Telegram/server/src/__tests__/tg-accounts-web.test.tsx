process.env.ISSUER ??= "https://tg-account-web-test.invalid";
process.env.TELEGRAM_API_ID ??= "12345";
process.env.TELEGRAM_API_HASH ??= "fixture";

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { SessionManager } from "../session-manager.js";

const { config } = await import("../config.js");
const { createTgAccountsRoutes, validateAccountLabel } = await import("../routes/tg-accounts.js");

const cookie = `tg_sid=${"a".repeat(64)}`;
function fixture() {
  const changes: unknown[] = [];
  const sessions = {
    getSavedUserIds: () => ["100"],
    listAccounts: (_owner: string) => [
      { accountId: 0, telegramUserId: "100", label: "Личный", isPrimary: true },
      { accountId: 2, telegramUserId: "200", label: "<script>Рабочий</script>", isPrimary: false },
    ],
    renameAccount: (owner: string, id: number, label: string) => {
      changes.push(["rename", owner, id, label]);
    },
    createAddAccountToken: (owner: string, label: string, ttl: number) => {
      changes.push(["add", owner, label, ttl]);
      return "capability-token";
    },
    disconnectAccount: (owner: string, id: number) => {
      changes.push(["disconnect", owner, id]);
      return true;
    },
  } as unknown as SessionManager;
  const oauth = { getBrowserSessionUser: (token: string | undefined) => (token === "a".repeat(64) ? "100" : null) };
  return { changes, app: createTgAccountsRoutes({ sessions, oauth }) };
}

describe("@tg browser account manager", () => {
  it("requires a verified browser session and escapes display labels", async () => {
    const { app } = fixture();
    assert.equal((await app.request("/")).status, 302);
    const response = await app.request("/", { headers: { Cookie: cookie } });
    assert.equal(response.status, 200);
    const html = await response.text();
    assert.ok(html.includes("&lt;script&gt;Рабочий&lt;/script&gt;"));
    assert.ok(!html.includes("<script>Рабочий</script>"));
    assert.ok(html.includes("/icon-512.png?v=tg-a1"));
  });

  it("binds rename to the cookie owner, including the primary account", async () => {
    const { app, changes } = fixture();
    const response = await app.request("/rename", {
      method: "POST",
      headers: { Cookie: cookie, Origin: config.issuer },
      body: new URLSearchParams({ account_id: "0", label: "  Дом  ", owner: "attacker" }),
    });
    assert.equal(response.status, 303);
    assert.deepEqual(changes, [["rename", "100", 0, "Дом"]]);
  });

  it("rejects cross-origin writes and malformed account ids", async () => {
    const { app, changes } = fixture();
    assert.equal(
      (
        await app.request("/rename", {
          method: "POST",
          headers: { Cookie: cookie, Origin: "https://evil.invalid" },
          body: new URLSearchParams({ account_id: "0", label: "Дом" }),
        })
      ).status,
      403,
    );
    assert.equal(
      (
        await app.request("/rename", {
          method: "POST",
          headers: { Cookie: cookie, Origin: config.issuer },
          body: new URLSearchParams({ account_id: "1e3", label: "Дом" }),
        })
      ).status,
      400,
    );
    assert.deepEqual(changes, []);
  });

  it("creates a bounded add-account capability for this owner", async () => {
    const { app, changes } = fixture();
    const response = await app.request("/add", {
      method: "POST",
      headers: { Cookie: cookie, Origin: config.issuer },
      body: new URLSearchParams({ label: "Общий" }),
    });
    assert.equal(response.status, 303);
    assert.equal(response.headers.get("location"), "/accounts/add/capability-token");
    assert.deepEqual(changes, [["add", "100", "Общий", 600]]);
  });

  it("requires explicit per-account disconnect confirmation", async () => {
    const { app, changes } = fixture();
    const headers = { Cookie: cookie, Origin: config.issuer };
    assert.equal(
      (await app.request("/disconnect", { method: "POST", headers, body: new URLSearchParams({ account_id: "2" }) }))
        .status,
      400,
    );
    assert.deepEqual(changes, []);
    assert.equal(
      (
        await app.request("/disconnect", {
          method: "POST",
          headers,
          body: new URLSearchParams({ account_id: "2", confirm: "yes" }),
        })
      ).status,
      303,
    );
    assert.deepEqual(changes, [["disconnect", "100", 2]]);
  });

  it("rejects empty, overlong or control-bearing names", () => {
    for (const label of [" ", "x".repeat(65), "private\nwork", "all", "@primary", "12", null])
      assert.throws(() => validateAccountLabel(label));
    assert.equal(validateAccountLabel(" Рабочий "), "Рабочий");
  });
});
