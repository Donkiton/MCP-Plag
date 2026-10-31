/**
 * The one list of Telegram errors that mean "this session is dead, not just unlucky".
 *
 * WHY IT IS SHARED (issue #19 review finding): `tool-registry.ts` used this list to decide
 * whether to revoke OAuth, while `session-manager.ts` decided whether to DELETE the stored
 * `session_string` from a plain boolean — `TelegramService.connect()` returns `false` for
 * network failures just as it does for a revoked auth key. A transient stall therefore
 * looked exactly like a revoked session and logged the user out permanently, which is a
 * worse outcome than the wedge this change set is fixing.
 *
 * The rule: destroy persisted credentials ONLY on positive evidence from Telegram that they
 * are invalid. Silence, timeouts and network errors are not evidence.
 *
 * Deliberately NOT here: `AUTH_KEY_DUPLICATED`. It means "this key is in use by another
 * connection", i.e. a live, valid session — deleting it would turn a recoverable collision
 * into a forced re-login.
 */
export const AUTH_ERROR_PATTERNS = [
  "AUTH_KEY_UNREGISTERED",
  "AUTH_KEY_INVALID",
  "SESSION_REVOKED",
  "SESSION_EXPIRED",
  "USER_DEACTIVATED",
  "USER_DEACTIVATED_BAN",
] as const;

/**
 * Upstream does not always surface the raw token. `TelegramService.connect()` catches
 * `AUTH_KEY_UNREGISTERED` / `SESSION_REVOKED` / `USER_DEACTIVATED` and rewrites `lastError`
 * into the sentence below (telegram-client.js: "Session revoked. Run telegram-login to
 * re-authenticate.").
 *
 * Matching only the uppercase tokens therefore produced the OPPOSITE failure of the one we
 * were fixing (review finding): a genuinely revoked session was never recognised, its row
 * was kept forever and every later call retried an auth key Telegram had already thrown
 * away. Deliberately narrow so "Network error: …" and "Connection marked unhealthy: …"
 * — the other two sentences upstream writes — keep the credentials.
 */
const REVOCATION_PHRASES = [/session revoked/i, /auth key (unregistered|invalid)/i] as const;

/** True when `message` carries positive evidence that the session is no longer usable. */
export function isAuthErrorMessage(message: string | undefined | null): boolean {
  if (!message) return false;
  if (AUTH_ERROR_PATTERNS.some((p) => message.includes(p))) return true;
  return REVOCATION_PHRASES.some((re) => re.test(message));
}

/** Same test for a thrown value. */
export function isAuthError(error: unknown): boolean {
  return isAuthErrorMessage(error instanceof Error ? error.message : String(error));
}
