import { z } from "zod";
import type { ToolDefinition } from "../tool-registry.js";
import { errorResult, LOCAL_WRITE, READ_ONLY, textResult } from "./helpers.js";

const ADD_TOKEN_TTL_SECONDS = 600;
const identifierSchema = z.string().min(1).describe("Custom label, numeric account_id, Telegram ID, or primary");

export const ACCOUNTS_TOOLS: ToolDefinition[] = [
  {
    name: "telegram-accounts-list",
    description:
      "List your connected Telegram accounts with stable account_id and custom names. Use account or account_id on Telegram data tools; writes require an explicit account when several are connected.",
    inputSchema: {},
    annotations: READ_ONLY,
    skipRequireConnection: true,
    handler: async (_args, { userId, sessions, selectedAccountId }) => {
      if (!userId || !sessions) return errorResult("An authenticated MCP connection is required.");
      const accounts = sessions.listAccounts(userId);
      if (!accounts.length) return textResult("No Telegram accounts connected. Open /my/accounts to connect one.");
      return textResult(
        JSON.stringify({
          accounts: accounts.map((a) => ({
            account_id: a.accountId,
            telegram_id: a.telegramUserId,
            name: a.label,
            primary: a.isPrimary,
            preferred_for_reads: a.accountId === selectedAccountId,
          })),
          note: "Use an explicit account/account_id for writes. account=all is read-only.",
        }),
      );
    },
  },
  {
    name: "telegram-accounts-current",
    description:
      "Show this MCP session's preferred account for reads. This is local to the current connection and never chooses a sender for a multi-account write.",
    inputSchema: {},
    annotations: READ_ONLY,
    skipRequireConnection: true,
    handler: async (_args, { userId, sessions, selectedAccountId }) => {
      if (!userId || !sessions) return errorResult("An authenticated MCP connection is required.");
      const accounts = sessions.listAccounts(userId);
      const account =
        accounts.find((a) => a.accountId === selectedAccountId) ?? accounts.find((a) => a.isPrimary) ?? accounts[0];
      return textResult(
        account
          ? JSON.stringify({
              account_id: account.accountId,
              name: account.label,
              telegram_id: account.telegramUserId,
              note: "Read preference only. Explicit account selection is required for multi-account writes.",
            })
          : "No accounts connected.",
      );
    },
  },
  {
    name: "telegram-accounts-switch",
    description:
      "Set the preferred read account for THIS MCP session only. Other ChatGPT/Codex sessions are unaffected. This does not authorize implicit multi-account sends; pass account/account_id on writes.",
    inputSchema: { identifier: identifierSchema },
    annotations: LOCAL_WRITE,
    skipRequireConnection: true,
    handler: async ({ identifier }, { userId, sessions, selectAccount }) => {
      if (!userId || !sessions || !selectAccount) return errorResult("An authenticated MCP connection is required.");
      const target = sessions.resolveAccount(userId, identifier);
      if (!target) return errorResult("Account not found. Run telegram-accounts-list.");
      selectAccount(target.accountId);
      return textResult(
        `Read preference set to account_id=${target.accountId} (${target.label ?? target.telegramUserId}) for this MCP session. Writes still require an explicit account.`,
      );
    },
  },
  {
    name: "telegram-accounts-add",
    description:
      "Connect another Telegram account through a private QR/2FA page. Returns a one-time link valid for 10 minutes. Existing accounts remain connected.",
    inputSchema: {
      label: z.string().min(1).max(64).optional().describe("Custom account name, e.g. Личный or Рабочий"),
    },
    annotations: LOCAL_WRITE,
    skipRequireConnection: true,
    handler: async ({ label }, { userId, sessions, baseUrl }) => {
      if (!userId || !sessions || !baseUrl) return errorResult("An authenticated MCP connection is required.");
      const token = sessions.createAddAccountToken(userId, label?.trim() || null, ADD_TOKEN_TTL_SECONDS);
      return textResult(
        `Open ${baseUrl}/accounts/add/${token}\nScan with the account you want to add. Link expires in 10 minutes. Never paste a password, login code or session string into chat. Manage names at ${baseUrl}/my/accounts.`,
      );
    },
  },
  {
    name: "telegram-accounts-rename",
    description:
      "Rename a connected Telegram account, including the primary account. The stable account_id and Telegram session do not change. Names must be unique within this connection.",
    inputSchema: { identifier: identifierSchema, label: z.string().min(1).max(64).describe("New unique display name") },
    annotations: LOCAL_WRITE,
    skipRequireConnection: true,
    handler: async ({ identifier, label }, { userId, sessions }) => {
      if (!userId || !sessions) return errorResult("An authenticated MCP connection is required.");
      const target = sessions.resolveAccount(userId, identifier);
      if (!target) return errorResult("Account not found. Run telegram-accounts-list.");
      sessions.renameAccount(userId, target.accountId, label);
      return textResult(`Renamed account_id=${target.accountId} to ${label.trim()}.`);
    },
  },
  {
    name: "telegram-accounts-remove",
    description:
      "Disconnect one Telegram account from @tg by its exact identifier. This does not delete Telegram messages, other accounts, or other MCP client grants. To revoke the Telegram device itself, use Telegram Settings → Devices.",
    inputSchema: { identifier: identifierSchema },
    annotations: LOCAL_WRITE,
    skipRequireConnection: true,
    handler: async ({ identifier }, { userId, sessions }) => {
      if (!userId || !sessions) return errorResult("An authenticated MCP connection is required.");
      const target = sessions.resolveAccount(userId, identifier);
      if (!target) return errorResult("Account not found. Run telegram-accounts-list.");
      const removed = sessions.disconnectAccount(userId, target.accountId);
      return textResult(
        removed
          ? `Disconnected account_id=${target.accountId}. Other accounts and MCP clients are unchanged.`
          : "This account was already disconnected.",
      );
    },
  },
];
