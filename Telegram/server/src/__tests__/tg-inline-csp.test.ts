import { describe, expect, test } from "bun:test";
import { Hono } from "hono";
import { contentSecurityPolicy, securityHeaders } from "../middleware/security-headers.js";

describe("@tg self-hosted QR page script policy", () => {
  test("gives inline scripts a matching fresh CSP nonce", async () => {
    const app = new Hono();
    app.use("*", securityHeaders);
    app.get("/", (c) => c.html('<html><body><script>window.qrReady=true</script></body></html>'));
    const first = await app.request("/");
    const html = await first.text();
    const nonce = html.match(/<script nonce="([^"]+)"/)?.[1];
    expect(nonce).toBeTruthy();
    expect(first.headers.get("Content-Security-Policy")).toContain(`'nonce-${nonce}'`);
    expect(first.headers.get("Content-Security-Policy")).not.toContain("script-src 'self' 'unsafe-inline'");
    expect(first.headers.get("Cache-Control")).toBe("private, no-store");
    const second = await app.request("/");
    expect(await second.text()).not.toContain(`nonce="${nonce}"`);
  });

  test("preserves an OAuth client's validated form redirect policy", async () => {
    const app = new Hono();
    app.use("*", securityHeaders);
    app.get("/", (c) => {
      c.header("Content-Security-Policy", contentSecurityPolicy(["https://chatgpt.com"]));
      return c.html('<script type="module">window.ready=true</script>');
    });
    const response = await app.request("/");
    expect(response.headers.get("Content-Security-Policy")).toContain("form-action 'self' https://chatgpt.com");
    expect(response.headers.get("Content-Security-Policy")).toContain("'nonce-");
  });

  test("keeps the ordinary strict policy for JSON and scriptless HTML", async () => {
    const app = new Hono();
    app.use("*", securityHeaders);
    app.get("/health", (c) => c.json({ status: "ok" }));
    app.get("/plain", (c) => c.html("<p>tg</p>"));
    for (const path of ["/health", "/plain"]) {
      const response = await app.request(path);
      expect(response.headers.get("Content-Security-Policy")).toBe(contentSecurityPolicy());
      expect(await response.text()).toContain(path === "/health" ? "ok" : "tg");
    }
  });
});
