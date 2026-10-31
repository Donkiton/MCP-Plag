import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { fileNameFromUrl, sanitizeFileName } from "../filename.js";

describe("sanitizeFileName", () => {
  it("keeps ordinary names untouched", () => {
    assert.equal(sanitizeFileName("review_git-delivery-2026-09-18.md"), "review_git-delivery-2026-09-18.md");
    assert.equal(sanitizeFileName("Отчёт за сентябрь.pdf"), "Отчёт за сентябрь.pdf");
    assert.equal(sanitizeFileName("a b (1).tar.gz"), "a b (1).tar.gz");
    assert.equal(sanitizeFileName(".gitignore"), ".gitignore");
  });

  it("reduces traversal attempts to their leaf name", () => {
    assert.equal(sanitizeFileName("../../etc/passwd"), "passwd");
    assert.equal(sanitizeFileName("/etc/shadow"), "shadow");
    assert.equal(sanitizeFileName("/abs/path.md"), "path.md");
    assert.equal(sanitizeFileName("..\\..\\Windows\\System32\\evil.dll"), "evil.dll");
    assert.equal(sanitizeFileName("C:\\Users\\me\\report.docx"), "report.docx");
  });

  it("never returns a value containing a path separator", () => {
    for (const raw of ["../../etc/passwd", "a/b/c", "x\\y\\z", "/", "//", "\\\\srv\\share\\f.txt"]) {
      const out = sanitizeFileName(raw);
      if (out !== null) {
        assert.ok(!out.includes("/"), `'${raw}' → '${out}' still has /`);
        assert.ok(!out.includes("\\"), `'${raw}' → '${out}' still has \\`);
      }
    }
  });

  it("strips NUL and control characters", () => {
    assert.equal(sanitizeFileName("foo\u0000.md"), "foo.md");
    assert.equal(sanitizeFileName("re\u0007port\u001b[31m.md"), "report[31m.md");
    assert.equal(sanitizeFileName("line\nbreak.txt"), "linebreak.txt");
    assert.equal(sanitizeFileName("\u0000"), null);
  });

  it("strips bidi overrides used to spoof the extension", () => {
    // report<RLO>gnp.exe renders as report exe.png in most clients.
    assert.equal(sanitizeFileName("report\u202Egnp.exe"), "reportgnp.exe");
    assert.equal(sanitizeFileName("\u202Ash\u202Cot.png"), "shot.png");
  });

  it("rejects empty and dots-only names", () => {
    assert.equal(sanitizeFileName(""), null);
    assert.equal(sanitizeFileName("   "), null);
    assert.equal(sanitizeFileName("."), null);
    assert.equal(sanitizeFileName(".."), null);
    assert.equal(sanitizeFileName("...."), null);
    assert.equal(sanitizeFileName("../.."), null);
    assert.equal(sanitizeFileName(null), null);
    assert.equal(sanitizeFileName(undefined), null);
  });

  it("caps at 255 bytes while keeping the extension", () => {
    const long = `${"a".repeat(400)}.md`;
    const out = sanitizeFileName(long);
    assert.ok(out);
    assert.ok(Buffer.byteLength(out, "utf8") <= 255, `got ${Buffer.byteLength(out ?? "", "utf8")} bytes`);
    assert.ok(out.endsWith(".md"), "extension must survive truncation");
  });

  it("counts bytes, not code units, for multi-byte names", () => {
    // 200 Cyrillic chars = 400 bytes — a length-based cap would let this through.
    const out = sanitizeFileName(`${"я".repeat(200)}.pdf`);
    assert.ok(out);
    assert.ok(Buffer.byteLength(out, "utf8") <= 255);
    assert.ok(out.endsWith(".pdf"));
    // No broken surrogate/партial code point at the cut.
    assert.ok(!out.includes("\uFFFD"));
  });

  it("does not split a surrogate pair when truncating", () => {
    const out = sanitizeFileName(`${"😀".repeat(100)}.png`);
    assert.ok(out);
    assert.ok(Buffer.byteLength(out, "utf8") <= 255);
    assert.equal(Buffer.from(out, "utf8").toString("utf8"), out, "must be valid UTF-8 after the cut");
  });

  it("handles a long name with no extension", () => {
    const out = sanitizeFileName("b".repeat(300));
    assert.ok(out);
    assert.equal(Buffer.byteLength(out, "utf8"), 255);
  });
});

describe("fileNameFromUrl", () => {
  it("takes the last path segment and drops query/fragment", () => {
    assert.equal(fileNameFromUrl("https://example.com/files/report.pdf"), "report.pdf");
    assert.equal(fileNameFromUrl("https://example.com/a/b/c.md?token=x#frag"), "c.md");
    assert.equal(fileNameFromUrl("https://example.com/files/"), "files");
  });

  it("decodes percent-encoding", () => {
    assert.equal(fileNameFromUrl("https://example.com/%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf"), "отчёт.pdf");
    // Encoded separators must not reintroduce a path.
    assert.equal(fileNameFromUrl("https://example.com/a%2F..%2Fb.txt"), "b.txt");
  });

  it("returns null when there is no usable segment", () => {
    assert.equal(fileNameFromUrl("https://example.com"), null);
    assert.equal(fileNameFromUrl("https://example.com/"), null);
    assert.equal(fileNameFromUrl("not a url"), null);
  });
});
