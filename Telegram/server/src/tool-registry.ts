import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { ShapeOutput, ZodRawShapeCompat } from "@modelcontextprotocol/sdk/server/zod-compat.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import type { TelegramService } from "@overpod/mcp-telegram/service";
import { z } from "zod";
import { accountResult, fanoutResult, selectAccountTargets } from "./account-routing.js";
import { isAuthError } from "./auth-errors.js";
// Aliased: `config` is shadowed by a local per-tool registration object below.
import { config as appConfig, iconPng256Url } from "./config.js";
import { isDeadlineError, withDeadline } from "./deadline.js";
import type { DownloadStore } from "./download-store.js";
import { logger, logUser } from "./logger.js";
import type { AccountSummary, SessionManager } from "./session-manager.js";
import { incr, observe, TOOL_CALLS, TOOL_DURATION, TOOL_TIMEOUTS } from "./telemetry/metrics.js";
import { getActiveSpanContext, SpanKind, withSpan } from "./telemetry/tracer.js";
import { deriveTitle, isGuardedDestructive } from "./tools/helpers.js";
import type { UploadStore } from "./upload-store.js";
import type { fetchUrlSafely } from "./url-fetcher.js";

export type RequireConnection = () => Promise<string | null>;
export type OnSessionRevoked = () => Promise<void>;
/**
 * issue #19 — invoked when a tool call or its connection check breached its deadline.
 * The implementation (mcp-handler) drops the user's in-memory TelegramService so the next
 * call rebuilds it from the persisted session string. Non-destructive by contract: it must
 * never log out of Telegram, revoke OAuth, or delete the stored session.
 */
export type OnToolTimeout = (toolName: string, client?: TelegramService) => void;
export type RateLimitCheck = (toolName: string) => string | null;
export type OnToolCall = (toolName: string) => void;
/** Phase 2.1 hook for tools whose annotation has `destructiveHint=true`. Returns
 * a human-readable error to short-circuit, or null to proceed. The hook is
 * responsible for writing its own audit row on deny — the registry only relays. */
export type DestructiveCheck = (toolName: string, args: unknown) => string | null;
/** Phase 2.1 result hook: invoked after a destructive tool finishes (success or
 * thrown). The registry passes a normalized outcome so the guard can record an
 * audit row without re-running its summarizer. */
export type DestructiveRecord = (toolName: string, args: unknown, result: "ok" | "error") => void;

export interface ToolAnnotations {
  readonly readOnlyHint: boolean;
  readonly destructiveHint: boolean;
  readonly openWorldHint: boolean;
}

export interface ToolDeps {
  readonly telegram: TelegramService;
  /** Preference belongs to this MCP transport; mutations still require explicit selectors. */
  readonly selectedAccountId?: number;
  readonly selectAccount?: (accountId: number) => void;
  /** Owner of the active MCP session. Set by mcp-handler.ts; absent in unit-test fixtures
   * that exercise tools not requiring user identity. Phase X upload tools require it. */
  readonly userId?: string;
  /** Phase X: per-user pending uploads. Required for the 6 FS-bound tools
   * (`telegram-send-file/voice/video-note/album/story`, `telegram-set-profile-photo`). */
  readonly uploads?: UploadStore;
  readonly downloads?: DownloadStore;
  /** Account bound to the exact TelegramService captured for this call. */
  readonly sourceAccountId?: number;
  readonly sourceAttachmentId?: string;
  /** Phase X: SSRF-hardened URL fetcher. Required for the URL variant of the 6 FS-bound tools. */
  readonly fetchUrl?: typeof fetchUrlSafely;
  /** v2.32.0 multi-account: session manager handle for the `accounts-*` tools. */
  readonly sessions?: SessionManager;
  /** v2.32.0: public-facing base URL used by `accounts-add` to build the QR-login link. */
  readonly baseUrl?: string;
}

type Args<TShape extends ZodRawShapeCompat> = ShapeOutput<TShape>;

export interface ToolDefinition<TShape extends ZodRawShapeCompat = ZodRawShapeCompat> {
  name: string;
  description: string;
  /**
   * Optional human-readable display title (MCP `tool.title`). The Claude Connectors
   * Directory requires every tool to carry one. When omitted, the registry derives it
   * from `name` via {@link deriveTitle}; set this only to override (e.g. acronyms).
   */
  title?: string;
  inputSchema?: TShape;
  annotations: ToolAnnotations;
  /** Skip requireConnection() — tool handles disconnected state itself (e.g. telegram-status). */
  skipRequireConnection?: boolean;
  /** Local snapshot reads don't need a live MTProto connection. */
  skipConnectionFor?: (args: Args<TShape>) => boolean;
  /**
   * Opt-in env flag: tool is registered only when `process.env[requiresEnv] === "1"`.
   * Mirrors upstream's opt-in pattern (e.g. MCP_TELEGRAM_ENABLE_GROUP_CALLS) so self-hosters
   * can disable categories of tools by overriding the env in their image/compose.
   */
  requiresEnv?: string;
  /** Synchronous pre-handler check, runs after rate-limit but before requireConnection. */
  preValidate?: (args: Args<TShape>) => CallToolResult | null;
  /** Main handler. Errors thrown propagate to handleToolError unless onError handles them. */
  handler: (args: Args<TShape>, deps: ToolDeps) => Promise<CallToolResult>;
  /** Custom error mapper. Return a result to short-circuit, null to fall through to default. */
  onError?: (e: unknown) => CallToolResult | null;
}

/**
 * `tool:env` pairs already reported as env-gated in this process. See the dedupe rationale
 * at the `tool.skipped` call site.
 */
const loggedSkips = new Set<string>();

/** Test-only: clear the per-process `tool.skipped` dedupe set. */
export function resetSkipLogDedupe(): void {
  loggedSkips.clear();
}

const SESSION_REVOKED_MSG =
  "Telegram session was revoked or expired. Please reconnect: Disconnect → Connect again in your app settings.";

/**
 * Tools allowed the long budget (`config.toolTimeoutSlowMs`): they move bytes over the
 * wire or wait on Telegram-side processing, so minutes are normal rather than a symptom.
 * Everything else gets `config.toolTimeoutMs`.
 */
export const SLOW_TOOLS: ReadonlySet<string> = new Set([
  "telegram-send-file",
  "telegram-send-voice",
  "telegram-send-video-note",
  "telegram-send-album",
  "telegram-send-story",
  "telegram-edit-story",
  "telegram-set-profile-photo",
  "telegram-download-media",
  "telegram-transcribe-audio",
  "telegram-get-transcription",
]);

/** Budget for one tool, in ms. Exported for the guard test. */
export function toolBudgetMs(toolName: string): number {
  return SLOW_TOOLS.has(toolName) ? appConfig.toolTimeoutSlowMs : appConfig.toolTimeoutMs;
}

/**
 * Outer backstop for the whole connection check.
 *
 * `requireConnection` can legitimately spend up to one connect budget inside
 * `ensureActiveSession` and another on its own `ensureConnected()`, and both of those mark
 * their own precise target when they expire. This wrapper exists only for the case where
 * neither fires — so it must be comfortably larger than their sum, otherwise it would cut
 * in first, win the race, and throw away the accurate attribution (review finding).
 */
function connectBackstopMs(): number {
  return appConfig.telegramConnectTimeoutMs > 0 ? appConfig.telegramConnectTimeoutMs * 3 : 0;
}

/** Budget that applies at a given stage — used only as a fallback when a deadline error
 *  arrives without its own `timeoutMs` (cross-module-instance case). Exported for the test
 *  that pins the review finding: a connect-stage breach must not report the tool budget. */
export function stageBudgetMs(toolName: string, stage: "connect" | "handler"): number {
  return stage === "connect" ? connectBackstopMs() : toolBudgetMs(toolName);
}

/**
 * Log + count a deadline breach and hand the session to `onToolTimeout` for a
 * memory-only rebuild. Never throws: self-healing is best-effort and must not turn a
 * timeout into a second failure on the response path.
 */
function reportTimeout(
  toolName: string,
  timeoutMs: number,
  stage: "connect" | "handler",
  onToolTimeout: OnToolTimeout | undefined,
  client?: TelegramService,
): void {
  // Fallback must match the STAGE that timed out (review finding): using the tool budget
  // for a connect-stage breach reported "timed out at connect after 180000ms" when the
  // connect budget was 15s, which would send an operator hunting the wrong knob.
  const budget = Number.isFinite(timeoutMs) ? timeoutMs : stageBudgetMs(toolName, stage);
  logger.warn(`Tool ${toolName} timed out at ${stage} after ${budget}ms`, {
    component: "tools",
    event: "tool.timeout",
    tool: toolName,
    reason: stage,
    durationMs: budget,
  });
  incr(TOOL_TIMEOUTS, { tool: toolName, stage });
  try {
    onToolTimeout?.(toolName, client);
  } catch (err) {
    logger.error(`onToolTimeout hook failed for ${toolName}: ${(err as Error).message}`, {
      component: "tools",
      event: "tool.timeout.hook_failed",
      tool: toolName,
    });
  }
}

/**
 * A timeout is NOT proof the action did not happen — promises are not cancellable, so the
 * Telegram call may still land. The wording says so explicitly, because the failure mode
 * we are guarding against is an agent "retrying" a send that already went through.
 */
function timeoutMessage(toolName: string, timeoutMs: number, stage: "connect" | "handler" = "handler"): string {
  // `isDeadlineError` also accepts a same-named error from another module instance, which
  // may not carry our fields — never render `NaNs` into a user-facing string.
  const seconds = Number.isFinite(timeoutMs)
    ? Math.round(timeoutMs / 1000)
    : Math.round(stageBudgetMs(toolName, stage) / 1000);
  return (
    `Telegram did not answer ${toolName} within ${seconds}s, so the call was abandoned. ` +
    "The connection has been reset and the next call will reconnect automatically. " +
    "Note the operation may still have completed on Telegram's side — check before retrying a send or delete."
  );
}

/**
 * Who made the call, in the same form `tool.call` already logs it (hashed user + MCP client).
 *
 * `tool.duration` / `tool.error` used to carry only the tool name, so slow or failing calls
 * could not be told apart as "one heavy user" vs "everyone": a single user whose calls queue
 * behind their own per-user lock routinely pushes 20-35% of all calls over 5s, which made a
 * tool-latency alert impossible. With these fields the alert can count distinct users.
 */
type CallerLogFields = { userId?: string; client?: string };

function callerLogFields(opts: Pick<RegisterAllOptions, "userId" | "clientName">): CallerLogFields {
  return {
    ...(opts.userId !== undefined && { userId: logUser(opts.userId) }),
    ...(opts.clientName !== undefined && { client: opts.clientName }),
  };
}

function handleToolError(
  e: unknown,
  onRevoked: OnSessionRevoked,
  toolName: string,
  caller: CallerLogFields = {},
): CallToolResult {
  const msg = (e as Error).message ?? String(e);
  if (isAuthError(e)) {
    logger.warn(`Auth error in ${toolName}: ${msg}`, {
      component: "tools",
      event: "tool.auth_error",
      tool: toolName,
      ...caller,
    });
    onRevoked().catch(() => {});
    return { content: [{ type: "text", text: SESSION_REVOKED_MSG }], isError: true };
  }
  logger.error(`Tool error in ${toolName}: ${msg}`, {
    component: "tools",
    event: "tool.error",
    tool: toolName,
    error: msg,
    ...caller,
  });
  return { content: [{ type: "text", text: `Error: ${msg}` }], isError: true };
}

export interface RegisterAllOptions {
  getTelegram: () => TelegramService;
  requireConnection: RequireConnection;
  onSessionRevoked?: OnSessionRevoked;
  /** issue #19 — called on a deadline breach so the stale session can be dropped. */
  onToolTimeout?: OnToolTimeout;
  onToolCall?: OnToolCall;
  checkRateLimit?: RateLimitCheck;
  /** Pre-handler check for destructive tools (annotation `destructiveHint=true`).
   * Skipped for non-destructive tools — they never reach the guard.
   * Both `checkDestructive` and `recordDestructive` should be wired together;
   * the registry will not call one without the other being meaningful. */
  checkDestructive?: DestructiveCheck;
  recordDestructive?: DestructiveRecord;
  /** Phase X: piped through to {@link ToolDeps.userId} for upload-backed tools.
   *  Also logged (hashed via logUser) on tool.duration / tool.error / tool.auth_error. */
  userId?: string;
  /** MCP client name ("Claude", "ChatGPT", …), logged on tool.duration / tool.error. */
  clientName?: string;
  /** Phase X: piped through to {@link ToolDeps.uploads}. */
  uploads?: UploadStore;
  downloads?: DownloadStore;
  /** Phase X: piped through to {@link ToolDeps.fetchUrl}. */
  fetchUrl?: typeof fetchUrlSafely;
  /** v2.32.0: piped through to {@link ToolDeps.sessions} for `accounts-*` tools. */
  sessions?: SessionManager;
  /** v2.32.0: piped through to {@link ToolDeps.baseUrl}. */
  baseUrl?: string;
}

/**
 * Wire a list of ToolDefinition entries onto an MCP server, applying common cross-cutting concerns
 * (rate-limit check → onToolCall logging → connection check → handler invocation → duration logging
 * → centralized error mapping).
 */
export function registerAllTools(server: McpServer, tools: readonly ToolDefinition[], opts: RegisterAllOptions): void {
  const onRevoked = opts.onSessionRevoked ?? (async () => {});
  const caller = callerLogFields(opts);
  let selectedAccountId: number | undefined;
  const accountStore = () => {
    if (!opts.sessions || !opts.userId) throw new Error("Account routing requires an authenticated owner.");
    return { sessions: opts.sessions, userId: opts.userId };
  };

  for (const tool of tools) {
    if (tool.requiresEnv && process.env[tool.requiresEnv] !== "1") {
      // Registration runs once per MCP session, but which tools are env-gated is a
      // process-level constant, so re-logging it per session said nothing new and made
      // `tool.skipped` ~45% of all log volume (136k records/week against 10k tool calls).
      // Emit once per process: same diagnostic for self-hosters, no per-session repetition.
      // Keyed by tool AND env var: two definitions gated on different flags must each be
      // reported, so the dedupe can never hide a distinct skip reason.
      const skipKey = `${tool.name}:${tool.requiresEnv}`;
      if (!loggedSkips.has(skipKey)) {
        loggedSkips.add(skipKey);
        logger.info(`Skipping tool ${tool.name}: env ${tool.requiresEnv} not set`, {
          component: "tools",
          event: "tool.skipped",
          tool: tool.name,
          env: tool.requiresEnv,
        });
      }
      continue;
    }

    const config: {
      title: string;
      description: string;
      annotations: ToolAnnotations & { title?: string };
      inputSchema?: ZodRawShapeCompat;
      icons: { src: string; mimeType: string; sizes: string[] }[];
      securitySchemes: { type: string; scopes: string[] }[];
      _meta: Record<string, unknown>;
    } = {
      title: tool.title ?? deriveTitle(tool.name),
      description: tool.description,
      annotations: tool.annotations,
      icons: [{ src: iconPng256Url, mimeType: "image/png", sizes: ["256x256"] }],
      securitySchemes: [{ type: "oauth2", scopes: [] }],
      _meta: { securitySchemes: [{ type: "oauth2", scopes: [] }] },
    };
    // The same title again inside the annotations: the Claude directory portal checks
    // `annotations.title` (Software Directory Policy 1E), not the top-level `tool.title`,
    // and flagged all 174 tools without it. Additive only: the value is identical and no
    // hint changes, so the tool contract under OpenAI review is untouched.
    config.annotations = { ...tool.annotations, title: config.title };
    const accountScoped =
      !tool.name.startsWith("telegram-accounts-") &&
      !!opts.userId &&
      typeof opts.sessions?.listAccounts === "function" &&
      typeof opts.sessions?.ensureAccountSession === "function";
    config.inputSchema = accountScoped
      ? {
          ...(tool.inputSchema ?? {}),
          account: z
            .string()
            .min(1)
            .optional()
            .describe(
              "Telegram account label or account_id; all is read-only. Writes with several accounts require an explicit selector.",
            ),
          account_id: z
            .number()
            .int()
            .nonnegative()
            .optional()
            .describe("Numeric account_id from telegram-accounts-list; alternative to account."),
        }
      : tool.inputSchema;
    if (accountScoped)
      config.description +=
        " Select the Telegram sender explicitly with account or account_id. account=all is supported only for read-only operations.";

    // Gated set, not the raw hint: CONFIRMED_WRITE tools carry destructiveHint for the
    // host's confirmation prompt but must not require the destructive opt-in.
    const isDestructive = isGuardedDestructive(tool.annotations);

    const execute = async (rawArgs: unknown, target?: AccountSummary): Promise<CallToolResult> => {
      let pinnedClient: TelegramService | undefined;
      const bound = target ? accountStore() : undefined;
      const attachmentAtStart =
        target && opts.userId ? opts.sessions?.getAccountAttachmentId(opts.userId, target.accountId) : undefined;
      const callOpts: RegisterAllOptions =
        target && bound
          ? {
              ...opts,
              getTelegram: () => {
                if (!pinnedClient) throw new Error("Telegram account is not connected.");
                return pinnedClient;
              },
              requireConnection: async () => {
                const telegram = await bound.sessions.ensureAccountSession(bound.userId, target.accountId);
                pinnedClient = telegram;
                if (
                  bound.sessions.getAccountAttachmentId(bound.userId, target.accountId) !== attachmentAtStart ||
                  !bound.sessions.listAccounts(bound.userId).some((a) => a.accountId === target.accountId)
                ) {
                  return "Account was disconnected during this request; nothing was redirected to another sender.";
                }
                const connected = await withDeadline(
                  "account.ensureConnected",
                  appConfig.telegramConnectTimeoutMs,
                  () => telegram.ensureConnected(),
                );
                return connected ? null : "Telegram account is not connected. Reconnect that account in /my/accounts.";
              },
              onSessionRevoked: async () => {
                bound.sessions.markUnhealthy(bound.userId, { accountId: target.accountId, expected: pinnedClient });
              },
              onToolTimeout: (_name, timedOutClient) => {
                bound.sessions.markUnhealthy(bound.userId, {
                  accountId: target.accountId,
                  expected: timedOutClient ?? pinnedClient,
                });
              },
            }
          : opts;
      callOpts.onToolCall?.(tool.name);
      const limitErr = callOpts.checkRateLimit?.(tool.name);
      if (limitErr) return { content: [{ type: "text", text: limitErr }] };

      // biome-ignore lint/suspicious/noExplicitAny: SDK passes opaque args; tool handler casts via shape.
      const args = (rawArgs ?? {}) as any;

      const preErr = tool.preValidate?.(args);
      if (preErr) return preErr;

      if (isDestructive && callOpts.checkDestructive) {
        const destErr = callOpts.checkDestructive(tool.name, args);
        if (destErr) return { content: [{ type: "text", text: destErr }], isError: true };
      }

      const skipConnection =
        (tool.skipRequireConnection || tool.skipConnectionFor?.(args) === true) &&
        !(target && tool.name === "telegram-status");
      if (!skipConnection) {
        // Bounded even though requireConnection swallows its own errors: it reaches
        // GramJS `ensureConnected()` through the per-user lock, which is exactly where
        // issue #19's permanent wedge formed.
        let connErr: string | null;
        try {
          connErr = await withDeadline(`connect:${tool.name}`, connectBackstopMs(), callOpts.requireConnection);
        } catch (e) {
          if (!isDeadlineError(e)) throw e;
          // No client handle exists yet at this stage — the hang happened while obtaining
          // one — so the hook falls back to dropping whatever is currently pooled.
          reportTimeout(tool.name, e.timeoutMs, "connect", callOpts.onToolTimeout);
          if (isDestructive) {
            // Review finding: `checkDestructive` already approved (and charged) this call,
            // so returning here without a result row left the audit trail claiming a
            // destructive action was authorised and never resolved.
            callOpts.recordDestructive?.(tool.name, args, "error");
          }
          return {
            content: [{ type: "text", text: timeoutMessage(tool.name, e.timeoutMs, "connect") }],
            isError: true,
          };
        }
        if (connErr) {
          if (isDestructive) callOpts.recordDestructive?.(tool.name, args, "error");
          return { content: [{ type: "text", text: connErr }], isError: true };
        }
      }

      // Wrap handler in a child span. Parent context (HTTP server-span) is
      // picked up via AsyncLocalStorage from the surrounding request — when
      // the tool is invoked outside an HTTP context (rare; SDK direct call),
      // the span becomes a fresh root, which is fine for trace exploration.
      const parent = getActiveSpanContext() ?? null;
      const start = Date.now();
      return withSpan(
        `mcp.tool ${tool.name}`,
        {
          kind: SpanKind.INTERNAL,
          parent,
          attributes: { component: "mcp", "mcp.tool": tool.name },
        },
        async (span) => {
          // Hoisted out of the try so the catch can fence the timeout report to the exact
          // client this call used (review finding): reporting against "whatever is pooled
          // now" can discard a session that a concurrent call already rebuilt.
          let handlerClient: TelegramService | undefined;
          try {
            // For tools that skip requireConnection (e.g. accounts-list runs
            // even when no Telegram session is alive) `getTelegram()` can throw.
            // The handler is opted out via `skipRequireConnection: true` and
            // works through `sessions` instead, so we tolerate the absence.
            let telegram: TelegramService;
            // No await between these lookups: account switches cannot relabel
            // the captured client while a media download is in flight.
            const sourceAccountId =
              target?.accountId ??
              (callOpts.userId !== undefined ? callOpts.sessions?.getActiveAccountId(callOpts.userId) : undefined);
            const sourceAttachmentId =
              callOpts.userId !== undefined && sourceAccountId !== undefined
                ? callOpts.sessions?.getAccountAttachmentId?.(callOpts.userId, sourceAccountId)
                : undefined;
            try {
              telegram = callOpts.getTelegram();
            } catch (err) {
              if (!skipConnection) throw err;
              // SAFETY: reached only when `tool.skipRequireConnection` is true (guarded on
              // the line above). Those handlers are contractually barred from dereferencing
              // `deps.telegram` — they work through `deps.sessions` instead — so the value is
              // never observed. The cast keeps `telegram` non-nullable in ToolDeps for the
              // other ~170 tools rather than forcing a null check into every one of them.
              telegram = undefined as unknown as TelegramService;
            }
            handlerClient = telegram;
            const deps: ToolDeps = {
              telegram,
              selectedAccountId,
              selectAccount: (accountId: number) => {
                selectedAccountId = accountId;
              },
              ...(sourceAccountId !== undefined && { sourceAccountId }),
              ...(sourceAttachmentId != null && { sourceAttachmentId }),
              ...(callOpts.userId !== undefined && { userId: callOpts.userId }),
              ...(callOpts.uploads !== undefined && { uploads: callOpts.uploads }),
              ...(callOpts.downloads !== undefined && { downloads: callOpts.downloads }),
              ...(callOpts.fetchUrl !== undefined && { fetchUrl: callOpts.fetchUrl }),
              ...(callOpts.sessions !== undefined && { sessions: callOpts.sessions }),
              ...(callOpts.baseUrl !== undefined && { baseUrl: callOpts.baseUrl }),
            };
            const result = await withDeadline(`tool:${tool.name}`, toolBudgetMs(tool.name), () =>
              tool.handler(args, deps),
            );
            const duration = Date.now() - start;
            const outcome = result.isError === true ? "error" : "ok";
            span.setAttribute("mcp.outcome", outcome);
            incr(TOOL_CALLS, { tool: tool.name, outcome });
            observe(TOOL_DURATION, duration, { tool: tool.name, outcome });
            logger.info(`Tool ${tool.name} completed in ${duration}ms`, {
              component: "tools",
              event: "tool.duration",
              tool: tool.name,
              durationMs: duration,
              ...caller,
            });
            if (isDestructive) {
              // `result.isError === true` is the convention for handler-returned faults;
              // record those as 'error' so the audit page distinguishes denied/error/ok.
              callOpts.recordDestructive?.(tool.name, args, outcome);
            }
            return result;
          } catch (e) {
            const duration = Date.now() - start;
            const timedOut = isDeadlineError(e);
            const outcome = timedOut ? "timeout" : "error";
            span.setAttribute("mcp.outcome", outcome);
            incr(TOOL_CALLS, { tool: tool.name, outcome });
            observe(TOOL_DURATION, duration, { tool: tool.name, outcome });
            // Until v2.59.0 `tool.duration` was logged only on the success path, so the
            // duration of everything that failed was invisible in SigNoz — which is part of
            // why issue #19 took guesswork to confirm. Failures now carry it too.
            logger.info(`Tool ${tool.name} failed after ${duration}ms`, {
              component: "tools",
              event: "tool.duration",
              tool: tool.name,
              durationMs: duration,
              outcome,
              ...caller,
            });
            if (isDestructive) {
              callOpts.recordDestructive?.(tool.name, args, "error");
            }
            if (timedOut) {
              reportTimeout(tool.name, e.timeoutMs, "handler", callOpts.onToolTimeout, handlerClient);
              return {
                content: [{ type: "text", text: timeoutMessage(tool.name, e.timeoutMs, "handler") }],
                isError: true,
              };
            }
            const custom = tool.onError?.(e);
            if (custom) return custom;
            return handleToolError(e, callOpts.onSessionRevoked ?? onRevoked, tool.name, caller);
          }
        },
      );
    };
    server.registerTool(tool.name, config, async (rawArgs: unknown): Promise<CallToolResult> => {
      if (!accountScoped) return execute(rawArgs);
      try {
        const owner = accountStore();
        const targets = selectAccountTargets(
          owner.sessions,
          owner.userId,
          (rawArgs ?? {}) as Record<string, unknown>,
          tool.annotations.readOnlyHint,
          selectedAccountId,
        );
        const results: CallToolResult[] = [];
        for (const target of targets) {
          try {
            results.push(accountResult(target, await execute(rawArgs, target)));
          } catch (error) {
            results.push(
              accountResult(target, { content: [{ type: "text", text: (error as Error).message }], isError: true }),
            );
          }
        }
        return results.length === 1 ? results[0] : fanoutResult(results);
      } catch (error) {
        return { content: [{ type: "text", text: (error as Error).message }], isError: true };
      }
    });
  }
}
