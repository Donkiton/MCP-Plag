import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import type { AccountSummary, SessionManager } from "./session-manager.js";

export function selectAccountTargets(
  sessions: SessionManager,
  owner: string,
  args: Record<string, unknown>,
  readOnly: boolean,
  preferredAccountId?: number,
): AccountSummary[] {
  if (args.account !== undefined && args.account_id !== undefined) {
    throw new Error("Pass account OR account_id, not both.");
  }
  const accounts = sessions.listAccounts(owner);
  if (accounts.length === 0) throw new Error("No Telegram accounts are connected. Open /my/accounts to connect one.");
  const selector = args.account ?? args.account_id;
  if (selector === undefined) {
    if (!readOnly && accounts.length > 1) {
      throw new Error(
        "Several Telegram accounts are connected. Specify account or account_id explicitly before a write; nothing was sent.",
      );
    }
    return [
      accounts.find((a) => a.accountId === preferredAccountId) ?? accounts.find((a) => a.isPrimary) ?? accounts[0],
    ];
  }
  if (String(selector).trim().toLowerCase() === "all") {
    if (!readOnly)
      throw new Error("account=all is allowed only for read-only tools. Select one account before a write.");
    return accounts;
  }
  const selected = sessions.resolveAccount(owner, String(selector));
  if (!selected)
    throw new Error("Account not found in this connection. Use telegram-accounts-list to choose an account_id.");
  return [selected];
}

export function accountResult(account: AccountSummary, result: CallToolResult): CallToolResult {
  const tag = `[account_id=${account.accountId}; ${account.label ?? account.telegramUserId}]`;
  return { ...result, content: [{ type: "text", text: tag }, ...result.content] };
}

export function fanoutResult(results: CallToolResult[]): CallToolResult {
  return {
    content: results.flatMap((result) => result.content),
    ...(results.every((result) => result.isError) ? { isError: true } : {}),
  };
}
