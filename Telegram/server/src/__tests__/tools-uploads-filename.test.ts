// `src/config.ts` validates env at import time; tools.ts pulls it in transitively.
process.env.ISSUER ??= "https://tools-uploads-filename-test.invalid";
process.env.TELEGRAM_API_ID ??= "1";
process.env.TELEGRAM_API_HASH ??= "stub";

import assert from "node:assert/strict";
import { describe, it } from "node:test";

const { TOOLS } = await import("../tools.js");

const sendFileTool = TOOLS.find((t) => t.name === "telegram-send-file");
const sendAlbumTool = TOOLS.find((t) => t.name === "telegram-send-album");
const sendVoiceTool = TOOLS.find((t) => t.name === "telegram-send-voice");

const TMP_PATH = "/tmp/mcp-telegram-cloud-uploads/upl_9b071789c52b45b4a2cf034b1d9a81a1";

type SendFileCall = [string, string, string | undefined, { fileName?: string } | undefined];
type SendVoiceCall = [string, string, Record<string, unknown>];
type AlbumItem = { filePath: string; caption?: string; fileName?: string };

interface Harness {
  deps: never;
  sendFileCalls: SendFileCall[];
  sendVoiceCalls: SendVoiceCall[];
  albumCalls: AlbumItem[][];
  consumed: string[];
  unlinked: string[];
}

/**
 * Deps double. `originalName` is whatever the multipart upload stored — i.e.
 * attacker-controlled — so tests can feed hostile values straight in.
 */
function makeHarness(
  opts: {
    originalName?: string | null;
    mime?: string;
    throwOnSend?: Error;
    rows?: Record<string, { original_name: string | null; tmp_path: string }>;
  } = {},
): Harness {
  const sendFileCalls: SendFileCall[] = [];
  const sendVoiceCalls: SendVoiceCall[] = [];
  const albumCalls: AlbumItem[][] = [];
  const consumed: string[] = [];
  const unlinked: string[] = [];

  const telegram = {
    sendFile: async (chatId: string, path: string, caption?: string, o?: { fileName?: string }) => {
      sendFileCalls.push([chatId, path, caption, o]);
      if (opts.throwOnSend) throw opts.throwOnSend;
    },
    sendVoice: async (chatId: string, path: string, o: Record<string, unknown>) => {
      sendVoiceCalls.push([chatId, path, o]);
      return { id: 7 };
    },
    sendAlbum: async (_chatId: string, items: AlbumItem[]) => {
      albumCalls.push(items);
      return { ids: [1, 2] };
    },
  };

  const uploads = {
    resolve: (_userId: string, uploadId: string) => {
      const row = opts.rows?.[uploadId];
      if (opts.rows && !row) return undefined;
      return {
        id: uploadId,
        user_id: "u1",
        tmp_path: row?.tmp_path ?? TMP_PATH,
        mime: opts.mime ?? "text/markdown",
        size: 16767,
        original_name: row ? row.original_name : (opts.originalName ?? null),
        created_at: "2026-09-21T00:00:00Z",
        expires_at: "2026-09-21T00:15:00Z",
      };
    },
    consume: async (_userId: string, uploadId: string) => {
      consumed.push(uploadId);
    },
    quotaBytesCap: 100_000_000,
    fileMaxBytesCap: 50_000_000,
    pendingBytesForUser: () => 0,
  };

  const fetchUrl = async () => ({ ok: true as const, bytes: Buffer.from("payload"), mime: "application/pdf" });

  return {
    deps: { telegram, uploads, fetchUrl, userId: "u1" } as never,
    sendFileCalls,
    sendVoiceCalls,
    albumCalls,
    consumed,
    unlinked,
  };
}

describe("telegram-send-file — original name reaches Telegram", () => {
  it("forwards the stored upload name as the fileName option", async () => {
    assert.ok(sendFileTool);
    if (!sendFileTool) return;
    const h = makeHarness({ originalName: "review_git-delivery-2026-09-18_2026-09-21.md" });

    const res = (await sendFileTool.handler(
      { chatId: "5589006450", source: { kind: "upload", uploadId: "upl_x" }, caption: "отчёт" },
      h.deps,
    )) as { content: Array<{ text: string }> };

    assert.equal(h.sendFileCalls.length, 1);
    const [chatId, path, caption, o] = h.sendFileCalls[0] as SendFileCall;
    assert.equal(chatId, "5589006450");
    assert.equal(path, TMP_PATH, "bytes still stream from the opaque temp path");
    assert.equal(caption, "отчёт");
    assert.deepEqual(o, { fileName: "review_git-delivery-2026-09-18_2026-09-21.md" });
    // The tool must say which name it used, or the next defect hides just as long.
    assert.match(res.content[0]?.text ?? "", /review_git-delivery-2026-09-18_2026-09-21\.md/);
  });

  it("sanitizes a hostile upload name instead of passing it through", async () => {
    assert.ok(sendFileTool);
    if (!sendFileTool) return;
    const h = makeHarness({ originalName: "../../etc/passwd" });

    await sendFileTool.handler({ chatId: "@me", source: { kind: "upload", uploadId: "upl_x" } }, h.deps);

    const [, , , o] = h.sendFileCalls[0] as SendFileCall;
    assert.equal(o?.fileName, "passwd");
  });

  it("falls back to upstream naming when nothing usable was stored", async () => {
    assert.ok(sendFileTool);
    if (!sendFileTool) return;
    for (const name of [null, "", "..", "\u0000"]) {
      const h = makeHarness({ originalName: name });
      const res = (await sendFileTool.handler(
        { chatId: "@me", source: { kind: "upload", uploadId: "upl_x" } },
        h.deps,
      )) as { content: Array<{ text: string }> };
      const [, , , o] = h.sendFileCalls[0] as SendFileCall;
      assert.deepEqual(o, {}, `'${name}' should yield no fileName option`);
      assert.equal(res.content[0]?.text, "Sent file to @me");
    }
  });

  it("names URL-sourced files after the last path segment", async () => {
    assert.ok(sendFileTool);
    if (!sendFileTool) return;
    const h = makeHarness();

    await sendFileTool.handler(
      { chatId: "@me", source: { kind: "url", url: "https://example.com/docs/spec%20v2.pdf?sig=abc" } },
      h.deps,
    );

    const [, , , o] = h.sendFileCalls[0] as SendFileCall;
    assert.equal(o?.fileName, "spec v2.pdf");
  });

  it("consumes the upload on success AND when the send throws", async () => {
    assert.ok(sendFileTool);
    if (!sendFileTool) return;

    const okRun = makeHarness({ originalName: "a.md" });
    await sendFileTool.handler({ chatId: "@me", source: { kind: "upload", uploadId: "upl_ok" } }, okRun.deps);
    assert.deepEqual(okRun.consumed, ["upl_ok"]);

    const boom = makeHarness({ originalName: "a.md", throwOnSend: new Error("FILE_PART_MISSING") });
    await assert.rejects(
      () => sendFileTool.handler({ chatId: "@me", source: { kind: "upload", uploadId: "upl_boom" } }, boom.deps),
      /FILE_PART_MISSING/,
      "handler must not swallow the upstream failure",
    );
    assert.deepEqual(boom.consumed, ["upl_boom"], "cleanup must run on the failure path too");
  });
});

describe("telegram-send-album / telegram-send-voice — same resolver, same fix", () => {
  it("passes a per-item fileName for each album member", async () => {
    assert.ok(sendAlbumTool);
    if (!sendAlbumTool) return;
    const h = makeHarness({
      rows: {
        upl_a: { original_name: "first.pdf", tmp_path: "/tmp/upl_a" },
        upl_b: { original_name: "/evil/../second.pdf", tmp_path: "/tmp/upl_b" },
      },
    });

    await sendAlbumTool.handler(
      {
        chatId: "@me",
        items: [
          { source: { kind: "upload", uploadId: "upl_a" }, caption: "one" },
          { source: { kind: "upload", uploadId: "upl_b" } },
        ],
      },
      h.deps,
    );

    assert.deepEqual(h.albumCalls[0], [
      { filePath: "/tmp/upl_a", caption: "one", fileName: "first.pdf" },
      { filePath: "/tmp/upl_b", fileName: "second.pdf" },
    ]);
  });

  it("passes the name to sendVoice so the codec is detectable", async () => {
    assert.ok(sendVoiceTool);
    if (!sendVoiceTool) return;
    const h = makeHarness({ originalName: "note.ogg", mime: "audio/ogg" });

    await sendVoiceTool.handler({ chatId: "@me", source: { kind: "upload", uploadId: "upl_v" } }, h.deps);

    const [, , o] = h.sendVoiceCalls[0] as SendVoiceCall;
    assert.equal(o.fileName, "note.ogg");
  });
});
