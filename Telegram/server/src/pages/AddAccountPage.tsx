import type { FC } from "hono/jsx";
import { config } from "../config.js";
import { button, card, hidden, qrContainer, spinner, status, step, subtitle, title } from "../styles.js";
import { telegramCredentialsConfigured } from "../tg-credentials.js";
import { inlineJson } from "./AuthorizePage.js";
import { Layout } from "./Layout.js";
import { TwoFactorBlock, twoFactorSetupScript } from "./qr-2fa-inline.js";
import { SetupRequiredPage } from "./SetupRequiredPage.js";

const clientScript = (token: string) => `
  let eventSource = null;

  function startAdd() {
    document.getElementById('intro').style.display = 'none';
    document.getElementById('qr-section').style.display = 'block';

    eventSource = new EventSource('/accounts/add/' + encodeURIComponent(${inlineJson(token)}) + '/qr');
    window.__setupTwoFactor(eventSource);

    eventSource.addEventListener('qr', (e) => {
      const data = JSON.parse(e.data);
      document.getElementById('qr-container').innerHTML =
        '<img src="' + data.dataUrl + '" alt="QR Code">';
    });

    eventSource.addEventListener('status', (e) => {
      const data = JSON.parse(e.data);
      document.getElementById('status').textContent = data.message;
    });

    eventSource.addEventListener('added', (e) => {
      const data = JSON.parse(e.data);
      eventSource.close();
      document.getElementById('qr-section').style.display = 'none';
      const result = document.getElementById('result');
      result.style.display = 'block';
      const labelPart = data.label ? ' — ' + window.__esc(data.label) : '';
      result.innerHTML =
        '<div style="background:#F4F4F7;border:1px solid #007AFF;border-radius:12px;padding:20px;margin:20px 0">' +
        '<h2 style="color:#007AFF;font-size:20px;margin-bottom:8px">Аккаунт добавлен</h2>' +
        '<p>' + window.__esc(data.name || '') + ' (@' + window.__esc(data.username || 'unknown') + ')' + labelPart + '</p>' +
        '<p style="margin-top:12px;font-size:13px;color:#707579">Указывайте его название при работе в ChatGPT или Codex.</p>' +
        '<p style="margin-top:12px"><a href="/my/accounts">Мои аккаунты</a></p>' +
        '</div>';
    });

    eventSource.addEventListener('error_msg', (e) => {
      const data = JSON.parse(e.data);
      eventSource.close();
      document.getElementById('qr-section').style.display = 'none';
      const result = document.getElementById('result');
      result.style.display = 'block';
      result.innerHTML =
        '<div style="background:#F4F4F7;border:1px solid #E53935;border-radius:12px;padding:20px;margin:20px 0">' +
        '<p>' + window.__esc(data.message) + '</p>' +
        '<p style="margin-top:12px;font-size:13px;color:#707579">Откройте «Мои аккаунты» и создайте новую ссылку подключения.</p>' +
        '<p><a href="/my/accounts">Мои аккаунты</a></p>' +
        '</div>';
    });

    eventSource.onerror = () => {
      eventSource.close();
      document.getElementById('status').textContent = 'Соединение прервано. Создайте новую ссылку подключения в «Мои аккаунты».';
    };
  }
  document.getElementById('startBtn').addEventListener('click', startAdd);
`;

interface AddAccountPageProps {
  token: string;
  label: string | null;
}

export const AddAccountPage: FC<AddAccountPageProps> = ({ token, label }) => {
  if (!telegramCredentialsConfigured()) return <SetupRequiredPage />;
  return (
    <Layout title={`${config.brandName} — Добавить аккаунт`}>
      <div class={card}>
        <h1 class={title}>Добавить Telegram-аккаунт</h1>
        <p class={subtitle}>
          Подключите ещё один свой аккаунт.
          {label ? ` Название: «${label}».` : ""}
        </p>

        <div id="intro">
          <div class={step}>
            <strong>1.</strong> Откройте Telegram с аккаунтом, который хотите добавить
          </div>
          <div class={step}>
            <strong>2.</strong> Настройки → Устройства → Подключить устройство
          </div>
          <div class={step}>
            <strong>3.</strong> Нажмите кнопку и отсканируйте QR-код
          </div>
          <button type="button" class={button} id="startBtn">
            Показать QR-код
          </button>
        </div>

        <div id="qr-section" class={hidden}>
          <div class={qrContainer} id="qr-container">
            <div class={spinner} />
          </div>
          <div class={status} id="status">
            Подготавливаем QR-код…
          </div>
          <TwoFactorBlock />
        </div>

        <div id="result" class={hidden} />
      </div>

      <script dangerouslySetInnerHTML={{ __html: twoFactorSetupScript }} />
      <script dangerouslySetInnerHTML={{ __html: clientScript(token) }} />
    </Layout>
  );
};
