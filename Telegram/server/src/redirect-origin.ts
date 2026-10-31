/**
 * Canonical "where does the authorization code actually go" key.
 *
 * WHY ORIGIN AND NOT client_id: every real MCP client re-registers constantly —
 * measured on prod 2026-09-21, one deployment held 727 `Claude` client rows, 98
 * `Cursor`, 66 `Google Antigravity`, all pointing at the same few callback URLs.
 * A consent record keyed by `client_id` would therefore expire on every
 * reconnect and put a confirmation screen in front of every returning user,
 * while a record keyed by the callback destination survives re-registration.
 * It is also the more honest security boundary: the code is delivered to this
 * origin, so this is the party being trusted — the `client_id` is just a label
 * anyone can mint via open RFC 7591 registration.
 *
 * Returns `null` only for input that cannot be parsed as a URL; callers treat
 * that as "never auto-approve".
 */
export function redirectOrigin(redirectUri: string): string | null {
  let url: URL;
  try {
    url = new URL(redirectUri);
  } catch {
    return null;
  }

  // `URL.origin` is the right answer for http/https but returns the literal
  // string "null" for non-special schemes (`cursor://…`, `vscode://…`), which
  // would collapse every custom-scheme client into ONE shared grant key —
  // approving Cursor would then silently approve any other custom-scheme app.
  // Build the key by hand so each scheme+host pair stays distinct.
  const scheme = url.protocol.toLowerCase();
  const host = url.host.toLowerCase();
  if (host) return `${scheme}//${host}`;

  // Host-less custom schemes (`com.example.app:/oauth/cb`): the path is the
  // only distinguishing part, so keep it — minus query and fragment, which are
  // per-request noise, not identity.
  return `${scheme}${url.pathname}`;
}
