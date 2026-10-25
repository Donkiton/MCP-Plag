import type { Child, FC } from "hono/jsx";
import { Layout } from "./Layout.js";

const panelCss = `
  body { flex-direction: column; gap: 20px; background: #eef3f7; color: #172b3a; }
  .tg-panel { width: min(100%, 680px); background: #fff; padding: 28px; border-radius: 20px; box-shadow: 0 8px 40px #172b3a0d; }
  .tg-panel h1 { font-size: 25px; margin: 0 0 12px; }
  .tg-panel h2 { font-size: 18px; margin: 0 0 12px; }
  .tg-panel p { line-height: 1.55; margin: 10px 0; overflow-wrap: anywhere; }
  .tg-panel a { color: #087fba; }
  .tg-panel label { display: block; margin: 18px 0 7px; font-size: 14px; font-weight: 600; }
  .tg-panel input:not([type=checkbox]) { width: 100%; background: transparent; color: inherit; border: 1px solid #b8cad6; border-radius: 10px; padding: 12px; font: inherit; }
  .tg-panel input:focus { outline: 2px solid #229ed9; outline-offset: 2px; }
  .tg-panel button, .tg-panel .tg-button { display: inline-block; margin-top: 14px; padding: 12px 18px; background: #087fba; color: white; font: inherit; border: 0; border-radius: 10px; cursor: pointer; text-decoration: none; }
  .tg-panel .tg-danger { background: #ad3232; }
  .tg-panel .tg-note { color: #536b7b; font-size: 14px; }
  .tg-panel .tg-message { padding: 12px 14px; background: #e8f5fc; border-radius: 10px; }
  .tg-account { border-top: 1px solid #dbe5ec; padding: 22px 0; }
  .tg-panel summary { cursor: pointer; margin-top: 16px; }
  .tg-panel .tg-confirm { font-weight: 400; }
  .tg-panel .tg-confirm input { margin-right: 8px; }
  @media (prefers-color-scheme: dark) { body { background: #161e25; color: #edf3f8; } .tg-panel { background: #222e38; } .tg-panel a { color: #6dcef9; } .tg-panel .tg-note { color: #b5c8d5; } .tg-panel .tg-message { background: #173e54; } .tg-account { border-color: #40515d; } }
  @media (max-width: 480px) { .tg-panel { padding: 20px; } body { padding: 14px; } }
`;

export const TgPanel: FC<{ title: string; children: Child }> = ({ title, children }) => (
  <Layout title={`@tg — ${title}`}>
    <style dangerouslySetInnerHTML={{ __html: panelCss }} />
    <main class="tg-panel">{children}</main>
  </Layout>
);
