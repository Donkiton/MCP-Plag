import { type Context, Hono } from "hono";
import { bodyLimit } from "hono/body-limit";
import { config } from "../config.js";
import { readBrowserSessionToken } from "../cookie-handler.js";
import type { OAuthProvider } from "../oauth.js";
import { TgPanel } from "../pages/TgPanel.js";
import type { SessionManager } from "../session-manager.js";

export interface TgAccountsRoutesDeps {
  sessions: SessionManager;
  oauth: Pick<OAuthProvider, "getBrowserSessionUser">;
}

const accountsError = (c: Context, status: 400 | 403 | 404 | 413, message: string) =>
  c.html(
    <TgPanel title="Мои аккаунты">
      <h1>Действие не выполнено</h1>
      <p role="alert">{message}</p>
      <a href="/my/accounts">Мои аккаунты</a>
    </TgPanel>,
    status,
  );

function accountId(value: unknown): number | null {
  if (typeof value !== "string" || !/^(0|[1-9]\d*)$/.test(value)) return null;
  const id = Number(value);
  return Number.isSafeInteger(id) ? id : null;
}

export function validateAccountLabel(value: unknown, required = true): string | null {
  if (typeof value !== "string") {
    if (!required) return null;
    throw new Error("Укажите название аккаунта.");
  }
  const name = value.trim().normalize("NFC");
  const hasControl = Array.from(name).some((character) => {
    const code = character.codePointAt(0)!;
    return code < 32 || code === 127;
  });
  if ((!name && required) || name.length > 64 || hasControl)
    throw new Error("Название должно содержать от 1 до 64 символов без управляющих знаков.");
  const key = name.replace(/^@/, "").toLowerCase();
  if (key === "all" || key === "primary" || /^\d+$/.test(key))
    throw new Error("Выберите название вместо служебного слова all, primary или числового ID.");
  return name || null;
}

export function createTgAccountsRoutes({ sessions, oauth }: TgAccountsRoutesDeps): Hono {
  const app = new Hono({ strict: false });
  const owner = (c: Context): string | null => {
    const id = oauth.getBrowserSessionUser(readBrowserSessionToken(c.req.header("cookie")));
    return id && sessions.getSavedUserIds().includes(id) ? id : null;
  };
  app.use("*", async (c, next) => {
    c.header("Cache-Control", "private, no-store");
    await next();
  });
  app.use("*", bodyLimit({ maxSize: 4096, onError: (c) => accountsError(c, 413, "Форма слишком большая.") }));
  app.use("*", async (c, next) => {
    if (!owner(c)) return c.redirect("/login", 302);
    if (c.req.method === "POST" && c.req.header("origin") !== config.issuer)
      return accountsError(c, 403, "Отправьте форму со страницы своих аккаунтов.");
    await next();
  });
  app.get("/", (c) => {
    const accounts = sessions.listAccounts(owner(c)!);
    return c.html(
      <TgPanel title="Мои аккаунты">
        <h1>Мои Telegram-аккаунты</h1>
        <p class="tg-note">
          Назовите аккаунты, например «Личный» и «Рабочий». При отправке указывайте аккаунт: действия из ChatGPT и Codex
          не переключают отправителя друг другу.
        </p>
        {accounts.map((account) => (
          <section class="tg-account">
            <h2>{account.label || (account.isPrimary ? "Основной аккаунт" : `Аккаунт ${account.accountId}`)}</h2>
            <p class="tg-note">
              Telegram ID: {account.telegramUserId} · ID в @tg: {account.accountId}
            </p>
            <form method="post" action="/my/accounts/rename">
              <input type="hidden" name="account_id" value={account.accountId} />
              <label for={`label-${account.accountId}`}>Название</label>
              <input
                id={`label-${account.accountId}`}
                name="label"
                value={account.label || ""}
                placeholder={account.isPrimary ? "Личный" : "Рабочий"}
                maxlength={64}
                required
              />
              <button type="submit">Сохранить название</button>
            </form>
            <details>
              <summary>Отключить этот аккаунт</summary>
              <form method="post" action="/my/accounts/disconnect">
                <input type="hidden" name="account_id" value={account.accountId} />
                <p class="tg-note">Аккаунт будет убран из @tg. Остальные аккаунты и подключённые клиенты сохранятся.</p>
                <label class="tg-confirm">
                  <input type="checkbox" name="confirm" value="yes" required />
                  Подтверждаю отключение этого аккаунта
                </label>
                <button class="tg-danger" type="submit">
                  Отключить аккаунт
                </button>
              </form>
            </details>
          </section>
        ))}
        {accounts.length === 0 && (
          <p>
            Подключённых аккаунтов пока нет. <a href="/login">Подключить основной аккаунт</a>
          </p>
        )}
        <section class="tg-account">
          <h2>Добавить ещё один аккаунт</h2>
          <form method="post" action="/my/accounts/add">
            <label for="new-label">Название нового аккаунта</label>
            <input id="new-label" name="label" placeholder="Рабочий" maxlength={64} required />
            <button type="submit">Подключить другой аккаунт</button>
          </form>
        </section>
        <p>
          <a href="/my/settings">Настройки действий</a>
        </p>
      </TgPanel>,
    );
  });
  app.post("/rename", async (c) => {
    const body = await c.req.parseBody();
    const id = accountId(body.account_id);
    if (id === null) return accountsError(c, 400, "Некорректный ID аккаунта.");
    try {
      const label = validateAccountLabel(body.label)!;
      sessions.renameAccount(owner(c)!, id, label);
      return c.redirect("/my/accounts", 303);
    } catch {
      return c.html(
        <TgPanel title="Название аккаунта">
          <h1>Название не сохранено</h1>
          <p role="alert">Проверьте название и выберите уникальное имя длиной до 64 символов.</p>
          <a href="/my/accounts">Мои аккаунты</a>
        </TgPanel>,
        400,
      );
    }
  });
  app.post("/add", async (c) => {
    const body = await c.req.parseBody();
    try {
      const label = validateAccountLabel(body.label)!;
      if (
        sessions
          .listAccounts(owner(c)!)
          .some(
            (a) => a.label?.normalize("NFC").replace(/^@/, "").toLowerCase() === label.replace(/^@/, "").toLowerCase(),
          )
      )
        throw new Error("duplicate");
      const token = sessions.createAddAccountToken(owner(c)!, label, 600);
      return c.redirect(`/accounts/add/${token}`, 303);
    } catch {
      return c.html(
        <TgPanel title="Новый аккаунт">
          <h1>Проверьте название</h1>
          <p role="alert">Укажите уникальное название длиной от 1 до 64 символов.</p>
          <a href="/my/accounts">Мои аккаунты</a>
        </TgPanel>,
        400,
      );
    }
  });
  app.post("/disconnect", async (c) => {
    const body = await c.req.parseBody();
    const id = accountId(body.account_id);
    if (id === null || body.confirm !== "yes")
      return accountsError(c, 400, "Подтвердите отключение выбранного аккаунта.");
    if (!sessions.disconnectAccount(owner(c)!, id)) return accountsError(c, 404, "Аккаунт уже отключён или не найден.");
    return c.redirect("/my/accounts", 303);
  });
  return app;
}
