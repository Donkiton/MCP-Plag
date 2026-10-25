/**
 * Bounded waits for anything that talks to Telegram.
 *
 * WHY THIS EXISTS (issue #19): before v2.59.0 nothing on the tool-call path had a
 * deadline. `requireConnection` → `SessionManager.ensureActiveSession` →
 * `withLock(userId, …)` → GramJS `ensureConnected()` were all unbounded awaits, so a
 * half-open MTProto socket did not merely make one call slow — it pinned the per-user
 * lock chain forever, and every later call by that user queued behind a promise that
 * would never settle. Reinstalling the connector or redoing OAuth could not clear it,
 * because the wedge lives in process memory, not in tokens or SQLite. Measured on
 * production before the fix: 76 tool calls over 7 days ran longer than 60s, the worst
 * finishing after 48.8 minutes.
 *
 * WHAT A DEADLINE IS NOT: JavaScript promises are not cancellable, so `withDeadline`
 * does **not** abort the underlying Telegram work. It only stops *us* waiting on it.
 * A timed-out `telegram-send-message` may still deliver the message. That asymmetry is
 * why the default budget is deliberately generous (minutes, not seconds) — we only cut
 * calls that are already lost to the MCP client anyway, and callers must treat a
 * timeout as "unknown outcome", never as "did not happen".
 */

/** Thrown by {@link withDeadline} when `fn` outlives its budget. */
export class DeadlineError extends Error {
  readonly operation: string;
  readonly timeoutMs: number;

  constructor(operation: string, timeoutMs: number) {
    super(`${operation} exceeded its ${timeoutMs}ms deadline`);
    this.name = "DeadlineError";
    this.operation = operation;
    this.timeoutMs = timeoutMs;
  }
}

/** Narrow an unknown rejection to a deadline breach. Robust across module instances
 *  (bun test isolation can produce two copies of the class) by checking `name`. */
export function isDeadlineError(e: unknown): e is DeadlineError {
  return e instanceof DeadlineError || (e instanceof Error && e.name === "DeadlineError");
}

/**
 * Run `fn` and reject with {@link DeadlineError} if it has not settled within `timeoutMs`.
 *
 * `timeoutMs <= 0` disables the deadline (the env knobs use 0 as "off"), in which case
 * this is a plain await — no timer is armed.
 *
 * The timer is always cleared, including on the happy path, so a long-lived server does
 * not accumulate pending timers; and the losing promise gets a no-op rejection handler so
 * a late failure after the deadline cannot surface as an unhandled rejection and take the
 * process down.
 */
export async function withDeadline<T>(operation: string, timeoutMs: number, fn: () => Promise<T>): Promise<T> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) return fn();

  let timer: ReturnType<typeof setTimeout> | undefined;
  const work = fn();
  // The work promise outlives this function when the deadline wins; without this the
  // eventual rejection would be unhandled.
  work.catch(() => {});

  try {
    return await Promise.race([
      work,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new DeadlineError(operation, timeoutMs)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}
