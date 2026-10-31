import { Database } from "bun:sqlite";
import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";
import type { ToolDeps } from "../tool-registry.js";
import { explicitContactTarget, normalizeContactAlias, TG_CONTACT_ALIAS_TOOLS } from "../tools/tg-contact-aliases.js";

const databases: Database[] = [];
afterEach(() => {
  while (databases.length) databases.pop()?.close();
});

function makeDb(): Database {
  const db = new Database(":memory:");
  databases.push(db);
  return db;
}

function deps(db: Database, userId = "owner", accountId = 0, attachmentId = "attachment-1"): ToolDeps {
  return {
    userId,
    sourceAccountId: accountId,
    sourceAttachmentId: attachmentId,
    telegram: new Proxy(
      {},
      {
        get: () => {
          throw new Error("Alias tool made an unexpected Telegram request");
        },
      },
    ),
    sessions: { getDb: () => db, listAccounts: () => [{ accountId }], getAccountAttachmentId: () => attachmentId },
  } as unknown as ToolDeps;
}

async function call(name: string, args: Record<string, unknown>, context: ToolDeps) {
  const tool = TG_CONTACT_ALIAS_TOOLS.find((item) => item.name === `telegram-${name}`);
  assert.ok(tool);
  const result = await tool.handler(args, context);
  const text = result.content
    .filter((item) => item.type === "text")
    .map((item) => item.text)
    .join("\n");
  return { result, text, json: result.isError ? null : JSON.parse(text) };
}

describe("@tg contact aliases — exact local mappings", () => {
  it("normalizes compatible Unicode, whitespace and case without fuzzy matching", () => {
    assert.deepEqual(normalizeContactAlias("  ＡНДРЕЙ   Разработчик  "), {
      key: "aндрей разработчик",
      display: "AНДРЕЙ Разработчик",
    });
    assert.equal(normalizeContactAlias("Лена").key, "лена");
    assert.notEqual(normalizeContactAlias("Лена").key, normalizeContactAlias("Леня").key);
    for (const alias of ["", " ", "@valid_user", "12345", "me", "self", "Андрей\u202E", "a\u200Bb", "x\nY", "\uD800"]) {
      assert.throws(() => normalizeContactAlias(alias));
    }
  });

  it("only accepts explicit Telegram usernames or numeric IDs", () => {
    assert.equal(explicitContactTarget(" @Alice_Name "), "@alice_name");
    assert.equal(explicitContactTarget("-1001234567890"), "-1001234567890");
    for (const target of [
      "Alice Name",
      "alice_name",
      "me",
      "@me",
      "123abc",
      "0",
      "00123",
      "+79991234567",
      "https://t.me/alice_name",
      "@alice/name",
      "@Kelvin",
      "@ſender",
      "9223372036854775808",
      "-9223372036854775809",
    ]) {
      assert.throws(() => explicitContactTarget(target));
    }
  });

  it("stores and exactly retrieves a contact without any Telegram call", async () => {
    const db = makeDb();
    const context = deps(db);
    const saved = await call("set-contact-alias", { alias: "Андрей разработчик", target: "@Andrew_Dev" }, context);
    assert.equal(saved.json.target, "@andrew_dev");
    assert.equal(saved.json.target_verified, false);
    assert.equal(saved.json.automatic_target_rewriting, false);
    const exact = await call("list-contact-aliases", { alias: "  АНДРЕЙ   разработчик " }, context);
    assert.equal(exact.json.count, 1);
    assert.equal(exact.json.aliases[0].target, "@andrew_dev");
    const similar = await call("list-contact-aliases", { alias: "Андрею разработчику" }, context);
    assert.equal(similar.json.count, 0);
  });

  it("never repoints an existing alias without replace=true", async () => {
    const db = makeDb();
    const context = deps(db);
    await call("set-contact-alias", { alias: "Иван", target: "12345" }, context);
    const conflict = await call("set-contact-alias", { alias: "ИВАН", target: "67890" }, context);
    assert.equal(conflict.result.isError, true);
    assert.match(conflict.text, /replace=true/);
    assert.equal((await call("list-contact-aliases", { alias: "Иван" }, context)).json.aliases[0].target, "12345");
    await call("set-contact-alias", { alias: "ИВАН", target: "67890", replace: true }, context);
    assert.equal((await call("list-contact-aliases", { alias: "Иван" }, context)).json.aliases[0].target, "67890");
  });

  it("isolates the same wording between owners, accounts and reattachments", async () => {
    const db = makeDb();
    const a = deps(db, "owner-a", 0, "a-primary");
    const b = deps(db, "owner-b", 0, "b-primary");
    const work = deps(db, "owner-a", 2, "a-work");
    await call("set-contact-alias", { alias: "Сергей", target: "111" }, a);
    await call("set-contact-alias", { alias: "Сергей", target: "222" }, b);
    await call("set-contact-alias", { alias: "Сергей", target: "333" }, work);
    for (const [context, target] of [
      [a, "111"],
      [b, "222"],
      [work, "333"],
    ] as const) {
      assert.equal((await call("list-contact-aliases", {}, context)).json.aliases[0].target, target);
    }
    assert.equal((await call("list-contact-aliases", {}, deps(db, "owner-a", 0, "a-new-primary"))).json.count, 0);
  });

  it("rejects changed attachment and unowned account without reading aliases", async () => {
    const db = makeDb();
    const changed = deps(db);
    const result = await call("list-contact-aliases", {}, { ...changed, sourceAttachmentId: "stale" });
    assert.equal(result.result.isError, true);
    assert.match(result.text, /attachment changed/);
    const noOwner = await call("list-contact-aliases", {}, { ...changed, userId: undefined });
    assert.equal(noOwner.result.isError, true);
    const unowned = await call("list-contact-aliases", {}, { ...changed, sourceAccountId: 99 });
    assert.equal(unowned.result.isError, true);
  });

  it("parameterizes aliases and deletes only the normalized exact account-local mapping", async () => {
    const db = makeDb();
    const a = deps(db, "owner", 0);
    const b = deps(db, "owner", 2);
    const alias = "Иван'); DROP TABLE tg_contact_aliases; --";
    await call("set-contact-alias", { alias, target: "12345" }, a);
    await call("set-contact-alias", { alias, target: "67890" }, b);
    assert.equal((await call("list-contact-aliases", { alias }, a)).json.count, 1);
    assert.equal((await call("delete-contact-alias", { alias: alias.toUpperCase() }, a)).json.deleted, true);
    assert.equal((await call("delete-contact-alias", { alias }, a)).json.deleted, false);
    assert.equal((await call("list-contact-aliases", { alias }, b)).json.aliases[0].target, "67890");
  });

  it("keeps local mutations non-outbound and leaves read fanout to the shared registry", () => {
    assert.equal(TG_CONTACT_ALIAS_TOOLS.length, 3);
    for (const tool of TG_CONTACT_ALIAS_TOOLS) {
      assert.equal(tool.skipRequireConnection, true);
      assert.equal(tool.annotations.openWorldHint, false);
      assert.equal(tool.annotations.destructiveHint, false);
      assert.equal(tool.annotations.readOnlyHint, tool.name === "telegram-list-contact-aliases");
    }
  });
});
