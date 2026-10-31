import type { FC } from "hono/jsx";
import { config } from "../config.js";
import { button, card, hidden, qrContainer, spinner, status, step, subtitle, title } from "../styles.js";
import { telegramCredentialsConfigured } from "../tg-credentials.js";
import { Layout } from "./Layout.js";
import { TwoFactorBlock, twoFactorSetupScript } from "./qr-2fa-inline.js";
import { SetupRequiredPage } from "./SetupRequiredPage.js";

const clientScript = `
  let eventSource = null;
  function startLogin() {
    document.getElementById('login-form').style.display = 'none';
    document.getElementById('qr-section').style.display = 'block';
    document.getElementById('startBtn').disabled = true;
    eventSource = new EventSource('/login/qr?userId=self');
    window.__setupTwoFactor(eventSource);
    eventSource.addEventListener('qr', (e) => {
      const data = JSON.parse(e.data);
      const image = document.createElement('img'); image.src = data.dataUrl; image.alt = 'QR-код входа в Telegram';
      document.getElementById('qr-container').replaceChildren(image);
    });
    eventSource.addEventListener('status', (e) => { document.getElementById('status').textContent = JSON.parse(e.data).message; });
    eventSource.addEventListener('connected', async (e) => {
      const data = JSON.parse(e.data); eventSource.close();
      document.getElementById('status').textContent = 'Аккаунт подключён. Открываем управление аккаунтами…';
      try {
        const response = await fetch('/oauth/authorize/qr/cookie', { method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({handoff:data.handoff}) });
        if (!response.ok) throw new Error('handoff');
        location.href = '/my/accounts';
      } catch { document.getElementById('status').textContent = 'Аккаунт сохранён, но браузер не получил разрешение. Обновите страницу и повторите вход.'; }
    });
    eventSource.addEventListener('error_msg', (e) => {
      eventSource.close(); document.getElementById('qr-container').style.display = 'none';
      document.getElementById('status').textContent = JSON.parse(e.data).message;
      document.getElementById('retry').style.display = 'block';
    });
    eventSource.onerror = () => {
      eventSource.close(); document.getElementById('status').textContent = 'Соединение прервано. Обновите QR-код и повторите вход.';
      document.getElementById('retry').style.display = 'block';
    };
  }
  document.getElementById('startBtn').addEventListener('click', startLogin);
`;

export const LoginPage: FC = () => {
  if (!telegramCredentialsConfigured()) return <SetupRequiredPage />;
  return (
    <Layout title={`${config.brandName} — Подключить аккаунт`}>
      <div class={card}>
        <h1 class={title}>Подключить Telegram</h1>
        <p class={subtitle}>Войдите через QR-код. Название аккаунта можно задать после подключения.</p>
        <div id="login-form">
          <div class={step}>Откройте Telegram на телефоне.</div>
          <div class={step}>Настройки → Устройства → Подключить устройство.</div>
          <div class={step}>Отсканируйте QR-код. Если включён облачный пароль, введите его здесь.</div>
          <button type="button" class={button} id="startBtn">
            Показать QR-код
          </button>
        </div>
        <div id="qr-section" class={hidden}>
          <div class={qrContainer} id="qr-container">
            <div class={spinner} />
          </div>
          <div class={status} id="status" role="status">
            Подготавливаем QR-код…
          </div>
          <TwoFactorBlock />
          <p id="retry" class={hidden}>
            <a href="/login">Обновить QR-код</a>
          </p>
        </div>
      </div>
      <script dangerouslySetInnerHTML={{ __html: twoFactorSetupScript }} />
      <script dangerouslySetInnerHTML={{ __html: clientScript }} />
    </Layout>
  );
};
