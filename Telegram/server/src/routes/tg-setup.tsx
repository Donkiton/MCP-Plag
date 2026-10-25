import { type Context, Hono } from "hono";
import { bodyLimit } from "hono/body-limit";
import { config } from "../config.js";
import { TgPanel } from "../pages/TgPanel.js";
import {
  saveTelegramCredentials,
  telegramCredentialsConfigured,
  validateTelegramCredentials,
  validSetupToken,
} from "../tg-credentials.js";

const fragmentScript = `(() => { const p = new URLSearchParams(location.hash.slice(1)); const t = p.get('token'); if (t) { document.getElementById('setup-token').value = t; history.replaceState(null, '', location.pathname); } })();`;

const setupError = (c: Context, status: 400 | 403 | 409 | 413, message: string) =>
  c.html(
    <TgPanel title="Настройка">
      <h1>Настройка не выполнена</h1>
      <p role="alert">{message}</p>
      <a href="/setup">Вернуться к настройке</a>
    </TgPanel>,
    status,
  );

export function createTgSetupRoutes(): Hono {
  const app = new Hono({ strict: false });
  app.use("*", async (c, next) => {
    c.header("Cache-Control", "no-store");
    await next();
  });
  app.use(
    "*",
    bodyLimit({
      maxSize: 4096,
      onError: (c) => setupError(c, 413, "Форма слишком большая. Проверьте введённые значения."),
    }),
  );
  app.get("/", (c) => {
    if (telegramCredentialsConfigured())
      return c.html(
        <TgPanel title="Настройка завершена">
          <h1>Доступ Telegram настроен</h1>
          <p>Можно подключить аккаунт и затем задать ему своё название.</p>
          <a class="tg-button" href="/login">
            Подключить аккаунт
          </a>
          <p>
            <a href="/my/accounts">Мои аккаунты</a>
          </p>
        </TgPanel>,
      );
    return c.html(
      <TgPanel title="Первичная настройка">
        <h1>Подготовка подключения</h1>
        <p>
          Один раз укажите реквизиты собственного приложения Telegram. Их можно получить в{" "}
          <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">
            API development tools
          </a>
          .
        </p>
        <p class="tg-note">
          API ID и API Hash сохраняются только на вашем сервере. Пароль Telegram и код входа здесь не требуются; вход в
          аккаунт будет следующим шагом.
        </p>
        <form method="post" action="/setup" autocomplete="off">
          <label for="setup-token">Ключ первоначальной настройки</label>
          <input id="setup-token" name="setup_token" type="password" required autocomplete="off" maxlength={512} />
          <label for="api-id">API ID</label>
          <input id="api-id" name="api_id" type="text" inputmode="numeric" pattern="[1-9][0-9]{0,9}" required />
          <label for="api-hash">API Hash</label>
          <input
            id="api-hash"
            name="api_hash"
            type="password"
            pattern="[a-fA-F0-9]{32}"
            maxlength={32}
            required
            autocomplete="off"
          />
          <button type="submit">Сохранить и перейти к подключению</button>
        </form>
        <script dangerouslySetInnerHTML={{ __html: fragmentScript }} />
      </TgPanel>,
    );
  });
  app.post("/", async (c) => {
    if (c.req.header("origin") !== config.issuer)
      return setupError(c, 403, "Отправьте форму с этой страницы настройки.");
    if (telegramCredentialsConfigured())
      return setupError(c, 409, "Настройка уже выполнена. Можно перейти к подключению аккаунта.");
    const body = await c.req.parseBody();
    if (!validSetupToken(body.setup_token)) return setupError(c, 403, "Неверный ключ первоначальной настройки.");
    try {
      const credentials = validateTelegramCredentials(body.api_id, body.api_hash);
      saveTelegramCredentials(credentials);
      return c.redirect("/setup", 303);
    } catch (error) {
      // Validation text is fixed copy; never echo submitted fields or an arbitrary filesystem error.
      const message =
        error instanceof Error && /^(API ID|API Hash|Доступ Telegram|Файл настройки)/.test(error.message)
          ? error.message
          : "Не удалось сохранить настройку. Проверьте права доступа к закрытому файлу на сервере.";
      return c.html(
        <TgPanel title="Настройка">
          <h1>Настройка не сохранена</h1>
          <p role="alert">{message}</p>
          <a href="/setup">Вернуться к форме</a>
        </TgPanel>,
        400,
      );
    }
  });
  return app;
}
