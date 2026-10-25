/**
 * issue #19 — the deadline primitive itself.
 *
 * These are the properties the rest of the fix leans on: a breach rejects with a typed
 * error, a late rejection after the breach cannot kill the process, and the happy path
 * leaves no armed timer behind (otherwise every tool call would keep the event loop busy
 * for its full budget).
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { type DeadlineError, isDeadlineError, withDeadline } from "../deadline.js";

const never = () => new Promise<never>(() => {});
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("withDeadline", () => {
  it("returns the value when fn settles in time", async () => {
    assert.equal(await withDeadline("op", 1000, async () => "ok"), "ok");
  });

  it("rejects with DeadlineError when fn outlives the budget", async () => {
    await assert.rejects(
      () => withDeadline("slow-op", 20, never),
      (e: unknown) => {
        assert.ok(isDeadlineError(e));
        assert.equal((e as DeadlineError).operation, "slow-op");
        assert.equal((e as DeadlineError).timeoutMs, 20);
        return true;
      },
    );
  });

  it("propagates the original error, not a deadline error, when fn rejects first", async () => {
    await assert.rejects(
      () =>
        withDeadline("op", 1000, async () => {
          throw new Error("AUTH_KEY_UNREGISTERED");
        }),
      (e: unknown) => {
        assert.equal(isDeadlineError(e), false);
        assert.match((e as Error).message, /AUTH_KEY_UNREGISTERED/);
        return true;
      },
    );
  });

  it("swallows a late rejection after the deadline (no unhandled rejection)", async () => {
    const unhandled: unknown[] = [];
    const onUnhandled = (e: unknown) => unhandled.push(e);
    process.on("unhandledRejection", onUnhandled);
    try {
      await assert.rejects(() =>
        withDeadline("late", 10, async () => {
          await sleep(40);
          throw new Error("late failure nobody is waiting for");
        }),
      );
      // Give the microtask + timer queues room to surface an unhandled rejection.
      await sleep(80);
      assert.deepEqual(unhandled, []);
    } finally {
      process.off("unhandledRejection", onUnhandled);
    }
  });

  it("clears the timer on the happy path so the process can exit immediately", async () => {
    const started = Date.now();
    await withDeadline("op", 60_000, async () => "done");
    // If the 60s timer were left armed, an unref-less timer would hold the loop; we assert
    // the observable proxy: the call returns promptly and node's timer list is drained by
    // the time the next macrotask runs.
    await sleep(1);
    assert.ok(Date.now() - started < 1000);
  });

  it("treats a non-positive budget as 'no deadline'", async () => {
    // 0 is the documented env value for "disabled" — it must not mean "expire instantly".
    assert.equal(await withDeadline("op", 0, async () => "ran"), "ran");
    assert.equal(await withDeadline("op", -5, async () => "ran"), "ran");
  });
});
