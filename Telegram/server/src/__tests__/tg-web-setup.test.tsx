process.env.ISSUER ??= "https://tg-web-test.invalid";

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, it } from "node:test";
import {
  readTelegramCredentials,
  saveTelegramCredentials,
  telegramCredentialsConfigured,
  validateTelegramCredentials,
  validSetupToken,
} from "../tg-credentials.js";

const { config } = await import("../config.js");
const { createTgSetupRoutes } = await import("../routes/tg-setup.js");
const { handleQrLogin, handleOAuthQrLogin, handleAddAccountQr } = await import("../qr-login.js");
const { LoginPage } = await import("../pages/LoginPage.js");

const keys = ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TG_SETUP_TOKEN", "TG_CREDENTIALS_FILE"] as const;
const before = new Map<string, string | undefined>();
let folder: string;
const token = "test-bootstrap-" + "x".repeat(48);
const hash = "ab".repeat(16);

beforeEach(() => {
  for (const key of keys) before.set(key, process.env[key]);
  delete process.env.TELEGRAM_API_ID;
  delete process.env.TELEGRAM_API_HASH;
  folder = mkdtempSync(join(tmpdir(), "tg-setup-test-"));
  process.env.TG_CREDENTIALS_FILE = join(folder, "private", "telegram-api.json");
  process.env.TG_SETUP_TOKEN = token;
});
afterEach(() => {
  for (const key of keys) {
    const value = before.get(key);
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  rmSync(folder, { recursive: true, force: true });
});

describe("@tg one-time browser setup", () => {
  it("never attempts Telegram calls before setup, including direct QR endpoints", async () => {
    const forbidden = new Proxy(
      {},
      {
        get() {
          throw new Error("No Telegram or OAuth method should run before credentials exist");
        },
      },
    ) as never;
    const signal = new AbortController().signal;
    const streams = [
      await handleQrLogin(forbidden, "self", signal, forbidden),
      await handleOAuthQrLogin(
        forbidden,
        forbidden,
        {
          clientId: "fixture",
          redirectUri: "https://chatgpt.com/callback",
          state: "s",
          codeChallenge: "c",
          codeChallengeMethod: "S256",
        },
        undefined,
        signal,
      ),
      await handleAddAccountQr(forbidden, "owner", "Рабочий", signal),
    ];
    for (const stream of streams) {
      const output = await new Response(stream).text();
      assert.ok(output.includes("event: error_msg"));
      assert.ok(output.includes("/setup"));
    }
    const node = LoginPage({});
    assert.ok(node);
    const html = String(await node.toString());
    assert.ok(html.includes("Нужна первоначальная настройка"));
    assert.ok(!html.includes("new EventSource"));
  });

  it("boots without credentials and resolves newly saved credentials without restart", () => {
    assert.equal(telegramCredentialsConfigured(), false);
    assert.equal(config.telegramApiId, 0);
    assert.equal(config.telegramApiHash, "");
    saveTelegramCredentials({ apiId: 12345, apiHash: hash });
    assert.deepEqual(readTelegramCredentials(), { apiId: 12345, apiHash: hash });
    assert.equal(config.telegramApiId, 12345);
    assert.equal(config.telegramApiHash, hash);
    if (process.platform !== "win32") assert.equal(statSync(process.env.TG_CREDENTIALS_FILE!).mode & 0o777, 0o600);
    assert.throws(() => saveTelegramCredentials({ apiId: 54321, apiHash: "cd".repeat(16) }));
    assert.equal(JSON.parse(readFileSync(process.env.TG_CREDENTIALS_FILE!, "utf8")).apiId, 12345);
  });

  it("rejects malformed IDs/hashes and requires an adequately long setup token", () => {
    for (const id of ["0", "-1", "1e3", "2147483648", "", "3.5"])
      assert.throws(() => validateTelegramCredentials(id, hash));
    for (const value of ["abc", "g".repeat(32), "<script>"])
      assert.throws(() => validateTelegramCredentials("123", value));
    assert.equal(validSetupToken(token), true);
    assert.equal(validSetupToken(token + "x"), false);
    process.env.TG_SETUP_TOKEN = "short";
    assert.equal(validSetupToken("short"), false);
  });

  it("never includes its configured bootstrap token in the public form", async () => {
    const app = createTgSetupRoutes();
    const response = await app.request("/");
    const html = await response.text();
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("cache-control"), "no-store");
    assert.ok(!html.includes(token));
    assert.ok(html.includes("/icon-512.png?v=tg-a1"));
    assert.ok(html.includes("location.hash"));
  });

  it("rejects cross-origin or wrong-token submissions before writing", async () => {
    const app = createTgSetupRoutes();
    const body = new URLSearchParams({ setup_token: token, api_id: "12345", api_hash: hash });
    assert.equal(
      (await app.request("/", { method: "POST", headers: { Origin: "https://evil.invalid" }, body })).status,
      403,
    );
    assert.equal(
      (
        await app.request("/", {
          method: "POST",
          headers: { Origin: config.issuer },
          body: new URLSearchParams({ setup_token: "wrong", api_id: "12345", api_hash: hash }),
        })
      ).status,
      403,
    );
    assert.equal(telegramCredentialsConfigured(), false);
  });

  it("accepts one valid same-origin setup, disables further writes and never echoes the hash", async () => {
    const app = createTgSetupRoutes();
    const body = () => new URLSearchParams({ setup_token: token, api_id: "12345", api_hash: hash });
    const first = await app.request("/", { method: "POST", headers: { Origin: config.issuer }, body: body() });
    assert.equal(first.status, 303);
    assert.equal(
      (await app.request("/", { method: "POST", headers: { Origin: config.issuer }, body: body() })).status,
      409,
    );
    const html = await (await app.request("/")).text();
    assert.ok(html.includes("Доступ Telegram настроен"));
    assert.ok(!html.includes(hash));
    assert.ok(!html.includes(token));
  });

  it("bounds request bodies and does not expose malformed submitted secrets in errors", async () => {
    const app = createTgSetupRoutes();
    const oversized = await app.request("/", {
      method: "POST",
      headers: { Origin: config.issuer, "Content-Type": "application/x-www-form-urlencoded" },
      body: "a=" + "x".repeat(5000),
    });
    assert.equal(oversized.status, 413);
    const bad = await app.request("/", {
      method: "POST",
      headers: { Origin: config.issuer },
      body: new URLSearchParams({ setup_token: token, api_id: "12345", api_hash: "secret-value" }),
    });
    assert.equal(bad.status, 400);
    assert.ok(!(await bad.text()).includes("secret-value"));
  });
});
