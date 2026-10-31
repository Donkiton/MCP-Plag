/**
 * issue #19 review finding — what counts as proof that a session is dead.
 *
 * This decides whether `tryReconnectSession` DELETEs a user's persisted `session_string`.
 * Both directions are failures a user feels:
 *   too loose  → a network blip logs them out permanently;
 *   too strict → a revoked session is retried forever and never cleaned up.
 *
 * The second one is what the review caught: upstream rewrites the raw Telegram token into
 * the sentence "Session revoked. Run telegram-login to re-authenticate.", so a list of
 * uppercase tokens alone never matched.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { isAuthError, isAuthErrorMessage } from "../auth-errors.js";

describe("isAuthErrorMessage — positive evidence of a dead session", () => {
  it("matches raw Telegram tokens", () => {
    assert.ok(isAuthErrorMessage("RPCError: 401: AUTH_KEY_UNREGISTERED"));
    assert.ok(isAuthErrorMessage("SESSION_REVOKED"));
    assert.ok(isAuthErrorMessage("USER_DEACTIVATED_BAN"));
  });

  it("matches the sentence upstream actually writes into lastError", () => {
    // telegram-client.js: connect() rewrites AUTH_KEY_UNREGISTERED / SESSION_REVOKED /
    // USER_DEACTIVATED into this exact string before we ever see it.
    assert.ok(isAuthErrorMessage("Session revoked. Run telegram-login to re-authenticate."));
  });

  it("does NOT match the other sentences upstream writes", () => {
    // These mean "try again later" — deleting credentials on them is the data-loss bug.
    assert.equal(isAuthErrorMessage("Network error: TIMEOUT. Run telegram-status to retry connection."), false);
    assert.equal(isAuthErrorMessage("Connection error: ECONNREFUSED"), false);
    assert.equal(isAuthErrorMessage("Connection marked unhealthy: ensureConnected exceeded 15000ms"), false);
  });

  it("does NOT match AUTH_KEY_DUPLICATED", () => {
    // A duplicate key means the session is alive and in use elsewhere. Treating it as
    // revoked would turn a recoverable collision into a forced re-login.
    assert.equal(isAuthErrorMessage("RPCError: 406: AUTH_KEY_DUPLICATED"), false);
  });

  it("handles empty and missing input", () => {
    assert.equal(isAuthErrorMessage(""), false);
    assert.equal(isAuthErrorMessage(undefined), false);
    assert.equal(isAuthErrorMessage(null), false);
  });

  it("isAuthError works on thrown values", () => {
    assert.ok(isAuthError(new Error("AUTH_KEY_INVALID")));
    assert.equal(isAuthError(new Error("CHAT_NOT_FOUND")), false);
    assert.equal(isAuthError("FLOOD_WAIT_30"), false);
  });
});
