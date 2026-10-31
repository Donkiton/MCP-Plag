/**
 * The delivery destination must be visible on every page that can lead to an
 * authorization code.
 *
 * `client_name` is chosen by whoever registers the client (registration is
 * open, RFC 7591), so "Claude" can point at evil.example. The host cannot be
 * faked, and it is the only thing on screen that tells the person what they are
 * actually connecting.
 *
 * Two server-rendered pages exist and BOTH are reachable:
 *   - ConsentPage — shown when the account has no grant for the destination;
 *   - AuthorizePage — the hono fallback QR page, used when the React SSR bundle
 *     is absent (`reactPagesAvailable() === false`, e.g. a source checkout that
 *     never ran `app:build`).
 * The React QR page is covered by app/src/__tests__/qr-pages-render.test.ts.
 *
 * These are direct render tests on purpose: a route-level test picks whichever
 * page the build state happens to enable, so it cannot pin either one. Found
 * the hard way — mutating the hono page left a route-level test green because
 * the React bundle was present in the workspace.
 */
process.env.TELEGRAM_API_ID ??= "1";
process.env.TELEGRAM_API_HASH ??= "test";
process.env.ISSUER ??= "https://authorize-destination-test.invalid";

import assert from "node:assert/strict";
import { describe, it } from "node:test";

const { AuthorizePage } = await import("../pages/AuthorizePage.js");
const { ConsentPage } = await import("../pages/ConsentPage.js");

/** hono/jsx nodes stringify asynchronously — same idiom as observability-page-ssr. */
// The extra String(...) matters: the promise resolves to a boxed String OBJECT,
// which assert.match rejects outright ('argument must be of type string').
const toHtml = async (node: unknown): Promise<string> =>
  String(await (node as { toString(): Promise<string> }).toString());

const base = {
  clientId: "c1",
  clientName: "Claude",
  redirectUri: "https://evil.example/cb?x=1",
  redirectOriginKey: "https://evil.example",
  state: "s",
  codeChallenge: "chal",
  codeChallengeMethod: "S256",
};

describe("AuthorizePage (hono fallback QR page)", () => {
  it("shows the destination host next to the client name", async () => {
    const html = await toHtml(AuthorizePage(base));
    assert.match(html, /Access code will be sent to/i);
    assert.match(html, /<strong>https:\/\/evil\.example<\/strong>/);
    // The name is still shown — it is useful, just not sufficient.
    assert.match(html, /Claude/);
  });

  it("falls back to the full redirect URI when the route supplied no origin", async () => {
    const { redirectOriginKey: _omitted, ...withoutOrigin } = base;
    const html = await toHtml(AuthorizePage(withoutOrigin));
    assert.match(html, /Access code will be sent to/i);
    assert.match(html, /evil\.example/);
  });
});

describe("ConsentPage", () => {
  it("shows the destination host", async () => {
    const html = await toHtml(ConsentPage(base));
    assert.match(html, /The access code will be sent to/i);
    assert.match(html, /<strong>https:\/\/evil\.example<\/strong>/);
  });

  it("submits by POST to the approve route, never by GET", async () => {
    const html = await toHtml(ConsentPage(base));
    assert.match(html, /<form[^>]+method="post"[^>]+action="\/oauth\/authorize\/approve"/);
    // Every OAuth parameter must survive the round-trip, or approval 400s.
    for (const name of ["client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method"]) {
      assert.ok(html.includes(`name="${name}"`), `missing hidden field ${name}`);
    }
  });
});
