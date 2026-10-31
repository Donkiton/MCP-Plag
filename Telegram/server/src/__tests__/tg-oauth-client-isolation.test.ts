process.env.ISSUER ??= "https://tg-oauth.invalid";
process.env.TELEGRAM_API_ID ??= "1";
process.env.TELEGRAM_API_HASH ??= "test";
process.env.MCP_TELEGRAM_TELEMETRY ??= "off";

import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { describe, it } from "node:test";

const { SessionManager } = await import("../session-manager.js");
const { OAuthProvider } = await import("../oauth.js");
const { createOAuthRoutes } = await import("../routes/oauth.js");
const { Hono } = await import("hono");
const { hashToken } = await import("../crypto.js");

function setup() {
  const sessions = new SessionManager(":memory:");
  sessions.saveSessionString("10001", "saved-primary-session");
  sessions.renameAccount("10001", 0, "Личный");
  sessions.addAccount("10001", "20002", "saved-work-session", "Рабочий");
  const db = sessions.getDb();
  const oauth = new OAuthProvider({ issuer: "https://tg-oauth.invalid", db });
  const register = (name: string) =>
    String(oauth.registerClient({ redirect_uris: ["http://127.0.0.1/callback"], client_name: name }).client_id);
  const chatgpt = register("ChatGPT"),
    codex = register("Codex");
  const issue = (clientId: string) => {
    const verifier = randomBytes(32).toString("base64url");
    const code = oauth.createAuthCode({
      clientId,
      userId: "10001",
      redirectUri: "http://127.0.0.1/callback",
      codeChallenge: createHash("sha256").update(verifier).digest("base64url"),
      codeChallengeMethod: "S256",
    });
    const pair = oauth.exchangeCode({
      code,
      clientId,
      codeVerifier: verifier,
      redirectUri: "http://127.0.0.1/callback",
    });
    assert.ok(pair);
    return pair;
  };
  return { sessions, db, oauth, chatgpt, codex, issue };
}

describe("@tg OAuth clients are separate from Telegram accounts", () => {
  it("uses short access lifetime and refreshes an expired access grant without Telegram login", () => {
    const { sessions, db, oauth, chatgpt, issue } = setup();
    const pair = issue(chatgpt);
    assert.ok(pair.expires_in >= 300 && pair.expires_in <= 86400);
    db.prepare("UPDATE oauth_tokens SET expires_at = ? WHERE access_token = ?").run(
      Math.floor(Date.now() / 1000) - 1,
      hashToken(pair.access_token),
    );
    assert.equal(oauth.validateToken(pair.access_token), null);
    const next = oauth.refreshAccessToken({ clientId: chatgpt, refreshToken: pair.refresh_token });
    assert.ok(next);
    assert.ok(oauth.validateToken(next.access_token));
    assert.notEqual(next.refresh_token, pair.refresh_token);
    assert.equal(sessions.listAccounts("10001").length, 2);
    sessions.close();
  });

  for (const tokenKind of ["access_token", "refresh_token"] as const) {
    it(`revokes one client's ${tokenKind} and refresh chain while preserving the other client and Telegram sessions`, async () => {
      const { sessions, oauth, chatgpt, codex, issue } = setup();
      const a = issue(chatgpt),
        b = issue(codex);
      let destroyed = false;
      sessions.destroyUserSession = async () => {
        destroyed = true;
        return { loggedOut: true };
      };
      const app = new Hono();
      app.route("/oauth", createOAuthRoutes({ oauth, sessions }));
      const response = await app.request("/oauth/revoke", {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ token: a[tokenKind] }).toString(),
      });
      assert.equal(response.status, 200);
      assert.equal(destroyed, false);
      assert.equal(oauth.validateToken(a.access_token), null);
      assert.equal(oauth.refreshAccessToken({ clientId: chatgpt, refreshToken: a.refresh_token }), null);
      assert.ok(oauth.validateToken(b.access_token));
      assert.ok(oauth.refreshAccessToken({ clientId: codex, refreshToken: b.refresh_token }));
      assert.equal(sessions.listAccounts("10001").length, 2);
      sessions.close();
    });
  }
});
