/** Every served branding slot must use the user's approved round @tg artwork. */
process.env.ISSUER ??= "https://brand-icons-test.invalid";
process.env.TELEGRAM_API_ID ??= "1";
process.env.TELEGRAM_API_HASH ??= "stub";

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { createHash } from "node:crypto";

const { createStaticRoutes } = await import("../routes/static.js");

const app = createStaticRoutes({ sessions: {} as never });

/** Width and height from a PNG's IHDR chunk. */
function pngSize(buf: Uint8Array): { w: number; h: number } {
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  assert.deepEqual([...buf.slice(1, 4)], [0x50, 0x4e, 0x47], "not a PNG");
  return { w: view.getUint32(16), h: view.getUint32(20) };
}

describe("brand icons", () => {
  for (const [path, size] of [
    ["/icon.png", 128],
    ["/icon-256.png", 256],
    ["/icon-512.png", 512],
    ["/login-icon.png", 512],
    ["/login-icon-dark.png", 512],
    ["/tool-icon.png", 64],
    ["/apple-touch-icon.png", 180],
  ] as const) {
    it(`${path} is a ${size}×${size} PNG`, async () => {
      const res = await app.request(path);
      assert.equal(res.status, 200);
      assert.equal(res.headers.get("content-type"), "image/png");
      assert.deepEqual(pngSize(new Uint8Array(await res.arrayBuffer())), { w: size, h: size });
    });
  }

  it("legacy SVG URL redirects to the approved custom PNG", async () => {
    const res = await app.request("/icon.svg");
    assert.equal(res.status, 302);
    assert.equal(res.headers.get("location"), "/icon-512.png?v=tg-a1");
  });

  it("favicon is a real multi-size ICO", async () => {
    const res = await app.request("/favicon.ico");
    assert.equal(res.headers.get("content-type"), "image/x-icon");
    const bytes = new Uint8Array(await res.arrayBuffer());
    assert.deepEqual([...bytes.slice(0, 4)], [0, 0, 1, 0]);
    assert.ok(bytes[4] >= 3);
  });

  it("App upload bytes match approved ImageGen variant A exactly", async () => {
    const res = await app.request("/icon-256.png");
    const bytes = new Uint8Array(await res.arrayBuffer());
    assert.ok(bytes.length < 10_000);
    assert.equal(createHash("sha256").update(bytes).digest("hex"),
      "7d66ada09d56189818d2ea9e902777b1a14351c6f4544561b5ab704b9323f809");
  });
});
