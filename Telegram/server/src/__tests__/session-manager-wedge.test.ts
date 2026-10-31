/**
 * issue #19 — the permanent wedge.
 *
 * Reported symptom: after some Telegram hiccup, one user's tool calls hang forever;
 * reinstalling the connector, redoing OAuth and waiting past the idle reaper all fail to
 * recover them, and only a container restart helps.
 *
 * Mechanism (pre-v2.59.0): `getOrCreateSessionImpl` / `ensureActiveSession` awaited GramJS
 * `ensureConnected()` INSIDE `withLock(userId, …)`. A half-open socket makes that promise
 * never settle, so the per-user lock chain is pinned and every later call for that user
 * queues behind it forever. Nothing outside process memory is involved — which is exactly
 * why re-auth could not fix it.
 *
 * These tests fail (by hanging into the runner's timeout) without the deadlines.
 */
process.env.ISSUER ??= "https://session-wedge-test.invalid";
process.env.TELEGRAM_API_ID ??= "12345";
process.env.TELEGRAM_API_HASH ??= "test-hash";

import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import type { TelegramService } from "@overpod/mcp-telegram/service";
import type { SessionManager as SessionManagerType } from "../session-manager.js";

const { SessionManager } = (await import("../session-manager.js")) as {
  SessionManager: typeof SessionManagerType;
};
const { config } = await import("../config.js");

// Overridden on the object, not via env: `bun test` shares one process across test files,
// so `config.ts` is initialised by whichever file imports it first and an env var set here
// is silently ignored when another file wins the race. See tool-timeout.test.ts.
const ORIGINAL_CONNECT_MS = config.telegramConnectTimeoutMs;
beforeEach(() => {
  config.telegramConnectTimeoutMs = 50;
});
afterEach(() => {
  config.telegramConnectTimeoutMs = ORIGINAL_CONNECT_MS;
});

const never = () => new Promise<never>(() => {});

/**
 * A client that connects normally and is later wedged by the test, reproducing the real
 * sequence: the session is healthy and pooled, then the MTProto socket goes half-open.
 *
 * `wedge("sticky")` keeps `isConnected()` returning true — the lying flag behind upstream
 * issue #71 — so no reconnect is attempted and the hang lands in the tool handler instead.
 * `wedge("dropped")` reports disconnected, which sends `getOrCreateSession` into
 * `ensureConnected()` — the await that used to pin the per-user lock forever.
 */
class WedgeableTelegramService {
  readonly calls: string[] = [];
  /** Reasons passed to the upstream `markUnhealthy` — the in-place recovery signal. */
  readonly unhealthyReasons: string[] = [];
  lastError = "";
  private connected = false;
  private wedged: "no" | "sticky" | "dropped" = "no";
  private sessionString: string | undefined;

  /** Never resolves connect()/ensureConnected() from construction on (cold-start wedge). */
  constructor(private readonly hangOnConnect = false) {}

  wedge(mode: "sticky" | "dropped"): void {
    this.wedged = mode;
  }

  /** Upstream (core ≥1.42.0) clears its sticky connected flag so the next ensureConnected
   *  destroys the dead sender and rebuilds from the same session string. */
  markUnhealthy(reason: string): void {
    this.unhealthyReasons.push(reason);
    this.connected = false;
  }

  async connect(): Promise<boolean> {
    this.calls.push("connect");
    if (this.hangOnConnect) return never();
    this.connected = true;
    return true;
  }
  isConnected(): boolean {
    if (this.wedged === "sticky") return true;
    if (this.wedged === "dropped") return false;
    return this.connected;
  }
  async ensureConnected(): Promise<boolean> {
    this.calls.push("ensureConnected");
    if (this.wedged !== "no") return never();
    return this.connected;
  }
  async disconnect(): Promise<void> {
    this.calls.push("disconnect");
    this.connected = false;
  }
  setSessionString(s: string): void {
    this.sessionString = s;
  }
  getSessionString(): string | undefined {
    return this.sessionString;
  }
}

/** A healthy client. */
class HealthyTelegramService {
  readonly calls: string[] = [];
  readonly unhealthyReasons: string[] = [];
  lastError = "";
  private connected = false;
  private sessionString: string | undefined;

  markUnhealthy(reason: string): void {
    this.unhealthyReasons.push(reason);
    this.connected = false;
  }

  async connect(): Promise<boolean> {
    this.calls.push("connect");
    this.connected = true;
    return true;
  }
  isConnected(): boolean {
    return this.connected;
  }
  async ensureConnected(): Promise<boolean> {
    this.calls.push("ensureConnected");
    return this.connected;
  }
  async disconnect(): Promise<void> {
    this.calls.push("disconnect");
    this.connected = false;
  }
  setSessionString(s: string): void {
    this.sessionString = s;
  }
  getSessionString(): string | undefined {
    return this.sessionString;
  }
}

/** Factory that yields the queued clients in order, then healthy ones forever. */
function makeManager(queue: object[]) {
  const created: object[] = [];
  const sm = new SessionManager(":memory:", () => {
    const next = queue.shift() ?? new HealthyTelegramService();
    created.push(next);
    return next as unknown as TelegramService;
  });
  return { sm, created };
}

describe("SessionManager — no permanent wedge (issue #19)", () => {
  it("a hung ensureConnected releases the per-user lock instead of pinning it forever", async () => {
    const wedged = new WedgeableTelegramService();
    const healthy = new HealthyTelegramService();
    const { sm } = makeManager([wedged, healthy]);
    sm.saveSessionString("user-A", "session-A");

    // Establish a healthy pooled session first — that is the state the bug starts from.
    assert.equal(await sm.getOrCreateSession("user-A"), wedged as unknown as TelegramService);
    wedged.wedge("dropped");

    const started = Date.now();
    // Now the socket is half-open: ensureConnected hangs and the deadline fires.
    const second = await sm.getOrCreateSession("user-A");
    const elapsed = Date.now() - started;

    assert.ok(elapsed < 5000, `getOrCreateSession took ${elapsed}ms — the lock was pinned`);
    assert.ok(wedged.calls.includes("ensureConnected"), "the reconnect attempt must have been made");
    // Recovery is IN PLACE (review finding): building a replacement here would race the
    // timed-out client for the same auth key. Upstream's markUnhealthy makes the next
    // ensureConnected destroy the dead sender and reconnect on this same instance.
    assert.equal(second, wedged as unknown as TelegramService, "the same client must be reused");
    assert.equal(wedged.unhealthyReasons.length, 1, "the client must be marked for revalidation");
    assert.equal(healthy.calls.length, 0, "no second client may be constructed for this user");
  });

  it("a second call for the same user is not stuck behind the first one", async () => {
    // This is the user-visible half of the bug: not "one slow call" but "every call after
    // it, forever".
    const wedged = new WedgeableTelegramService();
    const { sm } = makeManager([wedged]);
    sm.saveSessionString("user-B", "session-B");
    await sm.getOrCreateSession("user-B");
    wedged.wedge("dropped");

    const started = Date.now();
    const first = sm.getOrCreateSession("user-B");
    const second = sm.getOrCreateSession("user-B");
    await Promise.all([first, second]);

    assert.ok(Date.now() - started < 5000, "the queue behind the wedged call never drained");
  });

  it("recovery never touches the persisted session string (no re-login)", async () => {
    const wedged = new WedgeableTelegramService();
    const { sm } = makeManager([wedged]);
    sm.saveSessionString("user-C", "session-C");
    await sm.getOrCreateSession("user-C");
    wedged.wedge("dropped");

    await sm.getOrCreateSession("user-C");

    // Memory-only recovery: same auth key, nothing revoked, user sees nothing.
    assert.ok(sm.getSavedUserIds().includes("user-C"), "session row must survive recovery");
    assert.equal(wedged.getSessionString(), "session-C", "the session string must stay loaded");
  });

  it("a connect() that never settles does not pin the lock either", async () => {
    const { sm } = makeManager([new WedgeableTelegramService(true)]);
    sm.saveSessionString("user-D", "session-D");
    const started = Date.now();
    // No pooled entry: goes straight to the build-from-SQLite path, whose connect() hangs.
    await sm.getOrCreateSession("user-D");
    assert.ok(Date.now() - started < 5000, "connect deadline did not fire");
    // Connect failed, so nothing broken was cached.
    assert.equal(sm.getSession("user-D"), undefined);
  });
});

describe("SessionManager.markUnhealthy (issue #19)", () => {
  it("marks the pooled client for revalidation without dropping it or the persisted session", async () => {
    const healthy = new HealthyTelegramService();
    const { sm } = makeManager([healthy]);
    sm.saveSessionString("user-E", "session-E");

    await sm.getOrCreateSession("user-E");
    assert.ok(sm.getSession("user-E"), "precondition: session is pooled");

    sm.markUnhealthy("user-E");

    assert.equal(sm.getSession("user-E"), healthy as unknown as TelegramService, "entry stays — one client per user");
    assert.equal(healthy.unhealthyReasons.length, 1, "the client must be told to revalidate");
    assert.ok(sm.getSavedUserIds().includes("user-E"), "session_string must NOT be deleted");
  });

  it("breaks the sticky-connected half-open case: the lying flag is cleared", async () => {
    // isConnected() lies (returns true), so no reconnect is attempted and the hang lands in
    // the tool handler. Before the fix nothing ever cleared that flag, so the user stayed
    // broken indefinitely; markUnhealthy is what makes the next call revalidate.
    const wedged = new WedgeableTelegramService();
    const { sm } = makeManager([wedged]);
    sm.saveSessionString("user-F", "session-F");

    await sm.getOrCreateSession("user-F");
    wedged.wedge("sticky");
    assert.equal(sm.getSession("user-F"), wedged as unknown as TelegramService);
    assert.equal(wedged.isConnected(), true, "precondition: the flag lies");

    sm.markUnhealthy("user-F");

    assert.deepEqual(wedged.unhealthyReasons, ["tool call exceeded its deadline"]);
    assert.equal(sm.getSession("user-F"), wedged as unknown as TelegramService, "still one client for this user");
  });

  it("is a no-op for a user with no pooled session", () => {
    const { sm } = makeManager([]);
    assert.doesNotThrow(() => sm.markUnhealthy("nobody"));
  });

  it("a stale timeout does not touch a session that was already replaced (fencing)", async () => {
    // Review finding: deadlines fire on the caller's clock, so a timeout report can arrive
    // after a concurrent flow replaced the client. Without fencing, the late report would
    // knock a healthy connection offline — and a retrying agent could do it repeatedly.
    const first = new HealthyTelegramService();
    const second = new HealthyTelegramService();
    const { sm } = makeManager([]);
    sm.saveSessionString("user-G", "session-G");

    await sm.adoptSession("user-G", first as unknown as TelegramService);
    await sm.adoptSession("user-G", second as unknown as TelegramService);
    assert.equal(sm.getSession("user-G"), second as unknown as TelegramService, "precondition: pool moved on");

    // The late report names the OLD client — it must be ignored.
    sm.markUnhealthy("user-G", { expected: first as unknown as TelegramService });

    assert.equal(second.unhealthyReasons.length, 0, "the live client must not be knocked offline");
    assert.equal(sm.getSession("user-G"), second as unknown as TelegramService, "healthy session must survive");
  });

  it("still acts when the report names the currently pooled client", async () => {
    const only = new HealthyTelegramService();
    const { sm } = makeManager([only]);
    sm.saveSessionString("user-H", "session-H");
    await sm.getOrCreateSession("user-H");

    sm.markUnhealthy("user-H", { expected: only as unknown as TelegramService });

    assert.equal(only.unhealthyReasons.length, 1);
  });

  it("marks the account that actually timed out, even after the user switched accounts", async () => {
    // Review finding (multi-account): resolving the target from "whichever account is
    // active now" marked the wrong connection when a switch landed between the call
    // starting and its deadline firing — the broken account stayed broken.
    const primary = new HealthyTelegramService();
    const secondary = new HealthyTelegramService();
    const { sm } = makeManager([primary, secondary]);
    sm.saveSessionString("user-M", "session-M");
    await sm.getOrCreateSession("user-M");

    const accountId = sm.addAccount("user-M", "tg-2", "session-M2", "work");
    sm.setActiveAccount("user-M", accountId);
    await sm.ensureActiveSession("user-M"); // materialises the secondary in its own pool slot
    assert.equal(sm.getSession("user-M"), secondary as unknown as TelegramService, "precondition: switched");

    // The timeout belongs to the PRIMARY client, reported after the switch.
    sm.markUnhealthy("user-M", { expected: primary as unknown as TelegramService });

    assert.equal(primary.unhealthyReasons.length, 1, "the client that timed out must be marked");
    assert.equal(secondary.unhealthyReasons.length, 0, "the account in use must be left alone");
  });

  it("connect-stage reports target the account captured at attempt start, not the current one", async () => {
    const primary = new HealthyTelegramService();
    const secondary = new HealthyTelegramService();
    const { sm } = makeManager([primary, secondary]);
    sm.saveSessionString("user-N", "session-N");
    await sm.getOrCreateSession("user-N");

    const accountId = sm.addAccount("user-N", "tg-2", "session-N2", "work");
    sm.setActiveAccount("user-N", accountId);
    await sm.ensureActiveSession("user-N");

    // No handle available (connect-stage): the caller names the account that was active
    // when the attempt began — the primary.
    sm.markUnhealthy("user-N", { accountId: 0 });

    assert.equal(primary.unhealthyReasons.length, 1);
    assert.equal(secondary.unhealthyReasons.length, 0);
  });
});

describe("tryReconnectSession must not confuse 'slow' with 'invalid' (review finding)", () => {
  it("keeps the persisted session string when connect merely times out", async () => {
    // Regression guard: collapsing a deadline breach into the "session invalid" branch
    // DELETEs user_sessions, which logs the user out permanently over a transient stall —
    // strictly worse than the wedge this change set out to fix.
    const { sm } = makeManager([new WedgeableTelegramService(true)]);
    sm.saveSessionString("user-J", "session-J");

    const result = await sm.tryReconnectSession("user-J");

    assert.equal(result, null, "a timed-out reconnect yields no client");
    assert.ok(sm.getSavedUserIds().includes("user-J"), "session_string must survive a timeout");
  });

  it("keeps the persisted session string when connect fails without a revocation reason", async () => {
    // THE review finding: upstream `connect()` returns false for network failures and
    // duplicate auth keys too, not only for revoked sessions. Deleting on a bare `false`
    // turned a transient outage into a forced re-login.
    class FlakyNetworkTelegramService extends HealthyTelegramService {
      override async connect(): Promise<boolean> {
        this.calls.push("connect");
        this.lastError = "Error: connection closed (TIMEOUT)";
        return false;
      }
    }
    const { sm } = makeManager([new FlakyNetworkTelegramService()]);
    sm.saveSessionString("user-K", "session-K");

    const result = await sm.tryReconnectSession("user-K");

    assert.equal(result, null);
    assert.ok(sm.getSavedUserIds().includes("user-K"), "a network failure must not destroy credentials");
  });

  it("still deletes the session string when Telegram names a revocation reason", async () => {
    // The delete path must stay alive: an auth key Telegram refuses is dead weight, and
    // keeping it would make every later call fail the same way.
    class RevokedTelegramService extends HealthyTelegramService {
      override async connect(): Promise<boolean> {
        this.calls.push("connect");
        this.lastError = "RPCError: 401: AUTH_KEY_UNREGISTERED";
        return false;
      }
    }
    const { sm } = makeManager([new RevokedTelegramService()]);
    sm.saveSessionString("user-L", "session-L");

    const result = await sm.tryReconnectSession("user-L");

    assert.equal(result, null);
    assert.ok(sm.getSavedUserIds().includes("user-L"), "the owner record must survive primary session death");
    assert.deepEqual(sm.listAccounts("user-L"), [], "the revoked Telegram session must no longer be routable");
    assert.equal(
      (
        sm.getDb().query("SELECT session_string FROM user_sessions WHERE user_id = ?").get("user-L") as {
          session_string: string;
        }
      ).session_string,
      "",
    );
  });
});

describe("destroyUserSession stays bounded (issue #19 review finding)", () => {
  it("completes even when both logOut and disconnect hang", async () => {
    // The logOut path was bounded, but its FAILURE path awaited `disconnect()` unbounded —
    // inside the per-user lock. A user hitting "Disconnect" on a half-open socket would
    // wedge exactly as before.
    class HangingOnTeardown extends WedgeableTelegramService {
      async logOut(): Promise<boolean> {
        return never();
      }
      override async disconnect(): Promise<void> {
        return never();
      }
    }
    const hanging = new HangingOnTeardown();
    const { sm } = makeManager([hanging]);
    sm.saveSessionString("user-I", "session-I");
    await sm.getOrCreateSession("user-I");

    const started = Date.now();
    const result = await sm.destroyUserSession("user-I");
    assert.ok(Date.now() - started < 5000, "destroyUserSession hung on teardown");
    assert.equal(result.loggedOut, false);

    // Local state must be gone regardless of what Telegram did.
    assert.equal(sm.getSession("user-I"), undefined);
    assert.ok(!sm.getSavedUserIds().includes("user-I"), "destroy must still wipe the persisted session");

    // And the user is not wedged afterwards.
    const afterwards = Date.now();
    await sm.getOrCreateSession("user-I");
    assert.ok(Date.now() - afterwards < 5000, "the per-user lock was pinned by the hung teardown");
  });
});
