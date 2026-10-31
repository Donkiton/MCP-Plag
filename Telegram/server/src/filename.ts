/**
 * Sanitize a client-supplied file name before it is handed to Telegram.
 *
 * `pending_uploads.original_name` comes straight from the multipart `file.name`
 * field, i.e. it is fully attacker-controlled. We never write it to disk (the
 * bytes keep living under their opaque `upl_<uuid>` path and travel to GramJS
 * wrapped in a CustomFile), so traversal cannot reach the filesystem — but the
 * value still ends up in a DocumentAttributeFilename that other people's
 * clients will render and may use as a download name. Treat it as hostile:
 *
 *  - take the last path segment only (POSIX `/` and Windows `\`), so
 *    `../../etc/passwd` and `C:\Windows\system32\x.dll` degrade to a leaf name;
 *  - drop NUL and every C0/C1 control character (log/terminal injection, and
 *    NUL truncation in anything downstream that is C-based);
 *  - strip bidi/format overrides (U+202A-202E, U+2066-2069, U+200E/F) that flip
 *    the rendered extension — the classic `report<RLO>gnp.exe` spoof;
 *  - refuse names that are only dots/whitespace (`.`, `..`, `   `);
 *  - cap at 255 BYTES in UTF-8, keeping the extension, since that is the
 *    practical file-name ceiling on every target filesystem.
 *
 * Returns `null` when nothing usable survives; callers then fall back to the
 * pre-fix behaviour (GramJS names the document after the temp path) rather than
 * inventing a name.
 */

const MAX_NAME_BYTES = 255;

/** C0 (0x00-0x1F), DEL (0x7F), C1 (0x80-0x9F) — none are legitimate in a name. */
// biome-ignore lint/suspicious/noControlCharactersInRegex: stripping control characters is the point
const CONTROL_CHARS = /[\u0000-\u001F\u007F-\u009F]/g;
/** LRM/RLM, LRE..RLO+PDF, and the isolate family — used to spoof extensions. */
const BIDI_OVERRIDES = /[\u200E\u200F\u202A-\u202E\u2066-\u2069]/g;

/** Truncate to `maxBytes` UTF-8 bytes, preserving the extension when possible. */
function capBytes(name: string, maxBytes: number): string {
  if (Buffer.byteLength(name, "utf8") <= maxBytes) return name;

  const dot = name.lastIndexOf(".");
  // Only treat a trailing `.ext` as an extension when it is short and not the
  // whole name — `.gitignore` (dot === 0) must not be read as "empty stem".
  const ext = dot > 0 && Buffer.byteLength(name.slice(dot), "utf8") <= 16 ? name.slice(dot) : "";
  const stemBudget = maxBytes - Buffer.byteLength(ext, "utf8");
  if (stemBudget <= 0) return sliceToBytes(name, maxBytes);
  return sliceToBytes(name.slice(0, dot > 0 ? dot : name.length), stemBudget) + ext;
}

/** Cut a string to at most `maxBytes` UTF-8 bytes without splitting a code point. */
function sliceToBytes(s: string, maxBytes: number): string {
  let out = "";
  let used = 0;
  // Iterating by code point (not UTF-16 unit) keeps surrogate pairs intact.
  for (const ch of s) {
    const w = Buffer.byteLength(ch, "utf8");
    if (used + w > maxBytes) break;
    out += ch;
    used += w;
  }
  return out;
}

export function sanitizeFileName(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;

  // Last segment only. Split on BOTH separators regardless of host platform:
  // the uploader may run Windows while we run Linux.
  const leaf = raw.split(/[/\\]/).pop() ?? "";

  const cleaned = leaf.replace(CONTROL_CHARS, "").replace(BIDI_OVERRIDES, "").trim();

  if (cleaned.length === 0) return null;
  // `.`, `..`, `...` and friends carry no information and are traversal-adjacent.
  if (/^\.+$/.test(cleaned)) return null;

  const capped = capBytes(cleaned, MAX_NAME_BYTES).trim();
  if (capped.length === 0 || /^\.+$/.test(capped)) return null;
  return capped;
}

/**
 * Best-effort file name for a fetched URL: the last non-empty path segment,
 * sanitized. Query string and fragment are dropped by the URL parser.
 *
 * Deliberately NOT using `Content-Disposition` — the fetcher does not surface
 * response headers, and a remote server should not get to name a file that a
 * third party will see. A URL with no usable segment (`https://host/`) yields
 * `null`, which keeps today's unnamed behaviour.
 */
export function fileNameFromUrl(url: string): string | null {
  let pathname: string;
  try {
    pathname = new URL(url).pathname;
  } catch {
    return null;
  }
  const segment = pathname.split("/").filter(Boolean).pop();
  if (!segment) return null;
  let decoded = segment;
  try {
    decoded = decodeURIComponent(segment);
  } catch {
    // Malformed percent-encoding — keep the raw segment, sanitizer still runs.
  }
  return sanitizeFileName(decoded);
}
