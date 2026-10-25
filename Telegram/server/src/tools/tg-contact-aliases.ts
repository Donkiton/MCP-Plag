import type { Database } from "bun:sqlite";
import { z } from "zod";
import type { ToolDefinition, ToolDeps } from "../tool-registry.js";
import { errorResult, LOCAL_WRITE, READ_ONLY, textResult } from "./helpers.js";

/** Fresh local implementation inspired by chigwell's remembered-contact workflow.
 * This module never resolves a peer over Telegram and never rewrites a send target.
 * Every target is an explicit identifier, scoped to the exact attached account.
 */
const MAX_ALIAS_LENGTH = 80;
const MAX_ALIASES_PER_ACCOUNT = 500;

export function normalizeContactAlias(value: string): { key: string; display: string } {
  if (/[\p{Cc}\p{Cf}\p{Cs}]/u.test(value)) {
    throw new Error("Alias must not contain control, invisible-format or invalid Unicode characters.");
  }
  const display = value.normalize("NFKC").trim().replace(/\s+/gu, " ");
  if (!display || display.length > MAX_ALIAS_LENGTH) {
    throw new Error(`Alias must contain 1–${MAX_ALIAS_LENGTH} characters after normalization.`);
  }
  if (/^@|^-?\d+$|^(?:me|self)$/iu.test(display)) {
    throw new Error("Use a descriptive alias, not a Telegram identifier or self-reference.");
  }
  return { key: display.toLowerCase(), display };
}

export function explicitContactTarget(value: string): string {
  const target = value.trim();
  if (/^@[A-Za-z][A-Za-z0-9_]{4,31}$/.test(target)) return target.toLowerCase();
  if (/^-?[1-9]\d{0,19}$/.test(target)) {
    const id = BigInt(target);
    if (id >= -9223372036854775808n && id <= 9223372036854775807n) return target;
  }
  throw new Error(
    "Target must be an explicit @username or nonzero numeric Telegram ID. Names, phone numbers, URLs and other aliases are not accepted.",
  );
}

interface AliasScope {
  userId: string;
  accountId: number;
  attachmentId: string;
}

interface AliasRow {
  alias_key: string;
  display_alias: string;
  target: string;
  created_at: string;
  updated_at: string;
}

function getScope(deps: ToolDeps): { db: Database; scope: AliasScope } {
  const { userId, sessions, sourceAccountId } = deps;
  if (
    !userId ||
    !sessions ||
    sourceAccountId === undefined ||
    !Number.isSafeInteger(sourceAccountId) ||
    sourceAccountId < 0
  ) {
    throw new Error("Contact aliases require an authenticated, explicitly selected Telegram account.");
  }
  if (!sessions.listAccounts(userId).some((account) => account.accountId === sourceAccountId)) {
    throw new Error("The selected Telegram account is not attached to this owner.");
  }
  const currentAttachment = sessions.getAccountAttachmentId(userId, sourceAccountId);
  if (!currentAttachment || (deps.sourceAttachmentId && deps.sourceAttachmentId !== currentAttachment)) {
    throw new Error("The Telegram account attachment changed. Select the account again before editing aliases.");
  }
  const db = sessions.getDb();
  db.exec(`CREATE TABLE IF NOT EXISTS tg_contact_aliases (
    owner_user_id TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    attachment_id TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    display_alias TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, account_id, attachment_id, alias_key)
  )`);
  return { db, scope: { userId, accountId: sourceAccountId, attachmentId: currentAttachment } };
}

function exactAlias(db: Database, scope: AliasScope, key: string): AliasRow | null {
  return db
    .query<AliasRow, [string, number, string, string]>(
      "SELECT alias_key, display_alias, target, created_at, updated_at FROM tg_contact_aliases WHERE owner_user_id=? AND account_id=? AND attachment_id=? AND alias_key=?",
    )
    .get(scope.userId, scope.accountId, scope.attachmentId, key);
}

const localResult = (scope: AliasScope, fields: Record<string, unknown>) =>
  textResult(
    JSON.stringify({
      account_id: scope.accountId,
      storage: "local",
      automatic_target_rewriting: false,
      ...fields,
    }),
  );

const aliasSchema = z
  .string()
  .min(1)
  .max(240)
  .describe(
    "Exact remembered wording, for example Андрей разработчик. Unicode-normalized and case-insensitive; no fuzzy matching.",
  );
const failed = (error: unknown) => errorResult((error as Error).message);

export const TG_CONTACT_ALIAS_TOOLS: ToolDefinition[] = [
  {
    name: "telegram-set-contact-alias",
    description:
      "Remember a local exact contact alias for the selected Telegram account. Target must already be an explicit @username or numeric Telegram ID. Repointing an existing alias requires replace=true. This changes only local bookkeeping; it does not send, modify Telegram contacts, verify the target or automatically rewrite other tools' chatId. List aliases first and pass the returned target explicitly to a Telegram command.",
    inputSchema: {
      alias: aliasSchema,
      target: z
        .string()
        .min(1)
        .max(64)
        .describe("Explicit @username or nonzero numeric Telegram ID, never a person's display name or another alias."),
      replace: z
        .boolean()
        .optional()
        .describe("Required to point an existing alias at a different target; false by default."),
    },
    annotations: LOCAL_WRITE,
    skipRequireConnection: true,
    handler: async ({ alias, target, replace = false }, deps) => {
      try {
        const { db, scope } = getScope(deps);
        const normalized = normalizeContactAlias(alias);
        const explicit = explicitContactTarget(target);
        return db.transaction(() => {
          const existing = exactAlias(db, scope, normalized.key);
          if (existing && existing.target !== explicit && !replace) {
            return errorResult(
              `Alias already points to ${existing.target}. Use replace=true only after confirming the new target.`,
            );
          }
          if (!existing) {
            const count =
              db
                .query<{ total: number }, [string, number, string]>(
                  "SELECT count(*) AS total FROM tg_contact_aliases WHERE owner_user_id=? AND account_id=? AND attachment_id=?",
                )
                .get(scope.userId, scope.accountId, scope.attachmentId)?.total ?? 0;
            if (count >= MAX_ALIASES_PER_ACCOUNT)
              return errorResult("This account already has 500 aliases. Delete an unused alias first.");
          }
          const now = new Date().toISOString();
          db.query(`INSERT INTO tg_contact_aliases
            (owner_user_id, account_id, attachment_id, alias_key, display_alias, target, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_user_id, account_id, attachment_id, alias_key)
            DO UPDATE SET display_alias=excluded.display_alias, target=excluded.target, updated_at=excluded.updated_at`).run(
            scope.userId,
            scope.accountId,
            scope.attachmentId,
            normalized.key,
            normalized.display,
            explicit,
            now,
            now,
          );
          return localResult(scope, {
            saved: true,
            alias: normalized.display,
            key: normalized.key,
            target: explicit,
            target_verified: false,
            note: "Identifier saved exactly; no Telegram request was made. Verify the recipient before sending.",
          });
        })();
      } catch (error) {
        return failed(error);
      }
    },
  },
  {
    name: "telegram-list-contact-aliases",
    description:
      "Read exact local contact aliases for the selected Telegram account. Optional alias filters by normalized exact wording, never by fuzzy similarity. Results are remembered identifiers, not proof of a recipient's current identity. Pass a returned target explicitly to another tool; aliases are not automatically substituted into sends.",
    inputSchema: {
      alias: aliasSchema.optional().describe("Optional exact alias filter. Omit to list this account's saved aliases."),
    },
    annotations: READ_ONLY,
    skipRequireConnection: true,
    handler: async ({ alias }, deps) => {
      try {
        const { db, scope } = getScope(deps);
        const rows =
          alias === undefined
            ? db
                .query<AliasRow, [string, number, string]>(
                  "SELECT alias_key, display_alias, target, created_at, updated_at FROM tg_contact_aliases WHERE owner_user_id=? AND account_id=? AND attachment_id=? ORDER BY alias_key LIMIT 500",
                )
                .all(scope.userId, scope.accountId, scope.attachmentId)
            : [exactAlias(db, scope, normalizeContactAlias(alias).key)].filter((row): row is AliasRow => row !== null);
        return localResult(scope, {
          count: rows.length,
          aliases: rows.map((row) => ({
            alias: row.display_alias,
            key: row.alias_key,
            target: row.target,
            created_at: row.created_at,
            updated_at: row.updated_at,
          })),
        });
      } catch (error) {
        return failed(error);
      }
    },
  },
  {
    name: "telegram-delete-contact-alias",
    description:
      "Forget one exact local contact alias in the selected account. This only deletes a remembered mapping; it does not delete a Telegram contact or message. No fuzzy deletion or automatic target resolution is performed.",
    inputSchema: { alias: aliasSchema },
    annotations: LOCAL_WRITE,
    skipRequireConnection: true,
    handler: async ({ alias }, deps) => {
      try {
        const { db, scope } = getScope(deps);
        const normalized = normalizeContactAlias(alias);
        const result = db
          .query(
            "DELETE FROM tg_contact_aliases WHERE owner_user_id=? AND account_id=? AND attachment_id=? AND alias_key=?",
          )
          .run(scope.userId, scope.accountId, scope.attachmentId, normalized.key);
        return localResult(scope, { deleted: result.changes > 0, key: normalized.key });
      } catch (error) {
        return failed(error);
      }
    },
  },
];
