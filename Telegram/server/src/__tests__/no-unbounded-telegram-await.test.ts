/**
 * issue #19 guard — every await that reaches GramJS must be bounded.
 *
 * The behavioural tests prove today's paths are bounded. This one is about tomorrow: the
 * bug was not a wrong line of code, it was an easy-to-add one. A new `await
 * telegram.ensureConnected()` inside `SessionManager` re-pins the per-user lock and
 * resurrects the exact wedge users reported, while every existing test stays green.
 *
 * Source-level on purpose: there is no runtime seam that can observe "someone awaited the
 * network without a deadline". Renaming the import does not defeat it, because it matches
 * the Telegram call sites, not the deadline helper.
 *
 * REVIEW FINDING (Copilot, first pass): the first version of this guard could pass
 * vacuously — a comment containing the word `withDeadline` above an unbounded call was
 * accepted as proof of boundedness, and a call split across lines was not seen at all.
 * Comments and strings are therefore stripped before scanning, the call regex tolerates
 * line breaks, and the scanner itself is unit-tested against synthetic offenders below.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");

/** Files that own a Telegram connection lifecycle. */
const GUARDED_FILES = ["session-manager.ts", "mcp-handler.ts", "tool-registry.ts"];

/**
 * Remove block comments, line comments and string/template literals, replacing each with
 * blank space so line numbers survive. Without this, prose and log messages mentioning
 * `withDeadline` count as evidence that a call is bounded.
 */
function scrub(source: string, alsoStrings: boolean): string {
  let out = "";
  let i = 0;
  const blank = (s: string) => s.replace(/[^\n]/g, " ");

  while (i < source.length) {
    const rest = source.slice(i);
    const block = /^\/\*[\s\S]*?\*\//.exec(rest);
    if (block) {
      out += blank(block[0]);
      i += block[0].length;
      continue;
    }
    const line = /^\/\/[^\n]*/.exec(rest);
    if (line) {
      out += blank(line[0]);
      i += line[0].length;
      continue;
    }
    const str = /^(["'`])(?:\\.|(?!\1)[\s\S])*\1/.exec(rest);
    if (str) {
      out += alsoStrings ? blank(str[0]) : str[0];
      i += str[0].length;
      continue;
    }
    out += source[i];
    i += 1;
  }
  return out;
}

/** Comments AND string/template literals blanked. Used for direct call detection and for
 *  the "is it wrapped" window, where a log message mentioning `withDeadline(` must not
 *  count as evidence. Both passes keep line numbers aligned. */
export function stripNonCode(source: string): string {
  return scrub(source, true);
}

/** Comments blanked, strings KEPT: bracket dispatch (`telegram["connect"]()`) lives inside
 *  a string literal, so blanking strings would hide the very form we want to catch. */
export function stripComments(source: string): string {
  return scrub(source, false);
}

/** Methods that put us on the wire and can therefore hang on a half-open socket. */
const WIRE_METHODS = "ensureConnected|connect|logOut|disconnect";

/**
 * Direct call form: `x.ensureConnected()`. `[\s\n]*` tolerates `telegram\n  .ensureConnected()`.
 *
 * REVIEW FINDING (second pass): indirect forms defeat this — `telegram["ensureConnected"]()`,
 * `const f = telegram.ensureConnected.bind(telegram); await f()`, `Reflect.apply(...)`. The
 * bracket and `.bind` forms are matched below. Dynamic dispatch through a variable is NOT
 * detectable by a text scanner and is accepted as out of scope: this guard is a tripwire
 * for the ordinary way someone reintroduces the bug, not a proof of boundedness. The
 * behavioural tests in session-manager-wedge.test.ts are what actually prove the property.
 */
const WIRE_CALL = new RegExp(`\\.[\\s\\n]*(${WIRE_METHODS})[\\s\\n]*\\([\\s\\n]*\\)`, "g");

/** Indirect forms that reach the same methods and would otherwise slip past. */
const INDIRECT_CALL = new RegExp(
  `(\\[[\\s\\n]*["'\`](${WIRE_METHODS})["'\`][\\s\\n]*\\]|\\.(${WIRE_METHODS})\\.bind\\()`,
  "g",
);

/** Markers that make a call site bounded — matched as calls, not as bare words. */
const BOUNDED = /(withDeadline\s*\(|connectBounded\s*\()/;

/**
 * Report every wire call that is not lexically inside a deadline wrapper.
 * Exported shape kept simple (string offenders) so the self-tests below can assert on it.
 */
export function findUnboundedCalls(source: string, label = "src"): string[] {
  const code = stripNonCode(source);
  const lines = code.split("\n");
  const offenders: string[] = [];

  const withStrings = stripComments(source);
  for (const match of [...code.matchAll(WIRE_CALL), ...withStrings.matchAll(INDIRECT_CALL)]) {
    const index = match.index ?? 0;
    const lineNo = code.slice(0, index).split("\n").length;
    // A wrapper either opens on this line or within the few lines above:
    //   await withDeadline("ensureConnected", budget, () =>
    //     pooled.telegram.ensureConnected(),
    //   );
    const window = lines.slice(Math.max(0, lineNo - 5), lineNo).join("\n");
    if (BOUNDED.test(window)) continue;
    // `void x.disconnect().catch(...)` and `x.disconnect().catch(...)` are fire-and-forget:
    // nobody awaits them, so they cannot pin a caller or a lock.
    const tail = code.slice(index, index + 200);
    if (/^\.[\s\n]*disconnect[\s\n]*\([\s\n]*\)[\s\n]*\.[\s\n]*catch/.test(tail)) continue;
    offenders.push(`${label}:${lineNo}: ${lines[lineNo - 1]?.trim() ?? ""}`);
  }
  return offenders;
}

describe("no unbounded Telegram await (issue #19 guard)", () => {
  for (const file of GUARDED_FILES) {
    it(`${file}: every wire call sits inside a deadline`, () => {
      const offenders = findUnboundedCalls(readFileSync(join(SRC, file), "utf8"), file);
      assert.deepEqual(
        offenders,
        [],
        "Unbounded Telegram await(s) found. Wrap them in withDeadline(…, config.telegramConnectTimeoutMs, …) " +
          "or route them through SessionManager.connectBounded — an unbounded one pins the per-user lock " +
          `forever (issue #19):\n${offenders.join("\n")}`,
      );
    });
  }

  it("the guard actually matches real call sites (it is not a no-op regex)", () => {
    // If a refactor renamed these methods, the loop above would pass vacuously.
    const code = stripNonCode(readFileSync(join(SRC, "session-manager.ts"), "utf8"));
    const hits = [...code.matchAll(WIRE_CALL)];
    assert.ok(hits.length >= 5, `expected the guard to see several wire calls, saw ${hits.length}`);
  });

  it("tool handlers are invoked through a deadline in the registry", () => {
    const raw = readFileSync(join(SRC, "tool-registry.ts"), "utf8");
    // Call-site count is taken from stripped code (a mention in a comment is not a call),
    // but the wrapper assertion must run on the RAW text: `stripNonCode` blanks the
    // `tool:${tool.name}` template literal that identifies the wrapper.
    const handlerCalls = stripNonCode(raw)
      .split("\n")
      .filter((l) => /tool\.handler\(/.test(l));
    assert.equal(handlerCalls.length, 1, "expected exactly one tool.handler call site in the registry");
    assert.match(
      raw,
      /withDeadline\(`tool:\$\{tool\.name\}`[\s\S]{0,120}tool\.handler\(/,
      "the single tool.handler call site must be wrapped in withDeadline",
    );
  });
});

describe("guard self-tests (it must not pass vacuously)", () => {
  it("catches a plain unbounded call", () => {
    assert.deepEqual(findUnboundedCalls("await telegram.ensureConnected();").length, 1);
  });

  it("is NOT fooled by a comment that merely mentions withDeadline", () => {
    // This is the exact false negative the review found.
    const source = ["// withDeadline is used elsewhere in this file", "await telegram.ensureConnected();"].join("\n");
    assert.equal(findUnboundedCalls(source).length, 1, "a comment must never count as a deadline");
  });

  it("is NOT fooled by a log string containing withDeadline", () => {
    const source = ['console.log("withDeadline(x)");', "await telegram.ensureConnected();"].join("\n");
    assert.equal(findUnboundedCalls(source).length, 1);
  });

  it("sees a call split across lines", () => {
    assert.equal(findUnboundedCalls(["await telegram", "  .ensureConnected();"].join("\n")).length, 1);
  });

  it("accepts a genuine deadline wrapper spanning lines", () => {
    const source = [
      'await withDeadline("ensureConnected", 15000, () =>',
      "  pooled.telegram.ensureConnected(),",
      ");",
    ].join("\n");
    assert.deepEqual(findUnboundedCalls(source), []);
  });

  it("accepts fire-and-forget disconnect", () => {
    assert.deepEqual(findUnboundedCalls("pooled.telegram.disconnect().catch(() => {});"), []);
  });

  it("flags an awaited disconnect, which can pin a lock", () => {
    assert.equal(findUnboundedCalls("await session.telegram.disconnect();").length, 1);
  });

  it("flags an unbounded logOut", () => {
    assert.equal(findUnboundedCalls("const ok = await session.telegram.logOut();").length, 1);
  });

  it("flags bracket-notation dispatch", () => {
    // Review finding: `telegram["ensureConnected"]()` used to slip through entirely.
    assert.equal(findUnboundedCalls('await telegram["ensureConnected"]();').length, 1);
    assert.equal(findUnboundedCalls("await telegram['connect']();").length, 1);
  });

  it("flags a bound-method reference", () => {
    assert.equal(findUnboundedCalls("const f = telegram.ensureConnected.bind(telegram);").length, 1);
  });

  it("documents the known blind spot: fully dynamic dispatch", () => {
    // Not detectable from text. Recorded as a test so the limitation is explicit rather
    // than discovered later by someone trusting the guard more than it deserves.
    const source = ['const m = "ensure" + "Connected";', "await telegram[m]();"].join("\n");
    assert.deepEqual(findUnboundedCalls(source), [], "known gap — behavioural tests cover this property");
  });
});
