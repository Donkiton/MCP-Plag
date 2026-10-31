import { Style } from "hono/css";
import type { Child, FC } from "hono/jsx";
import { config, iconUrl } from "../config.js";
import { globalReset } from "../styles.js";

interface LayoutProps {
  title: string;
  description?: string;
  canonicalUrl?: string;
  children: Child;
  globalCss?: string;
}

export const Layout: FC<LayoutProps> = (props) => {
  return (
    <html lang="ru">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        {/* Functional-only host: keep out of search indexes (defense-in-depth
            alongside the global X-Robots-Tag header). */}
        <meta name="robots" content="noindex, nofollow" />
        <title>{props.title}</title>
        {props.description && <meta name="description" content={props.description} />}
        {props.canonicalUrl && <link rel="canonical" href={props.canonicalUrl} />}

        {/* Open Graph */}
        <meta property="og:type" content="website" />
        <meta property="og:title" content={props.title} />
        {props.description && <meta property="og:description" content={props.description} />}
        <meta property="og:site_name" content={config.brandName} />
        {props.canonicalUrl && <meta property="og:url" content={props.canonicalUrl} />}
        <meta property="og:image" content={iconUrl} />

        {/* Twitter Card */}
        <meta name="twitter:card" content="summary" />
        <meta name="twitter:title" content={props.title} />
        {props.description && <meta name="twitter:description" content={props.description} />}
        <meta name="twitter:image" content={iconUrl} />

        <link rel="icon" href="/favicon.ico?v=tg-a1" />
        <link rel="apple-touch-icon" href="/apple-touch-icon.png?v=tg-a1" />
        <Style />
        <style dangerouslySetInnerHTML={{ __html: props.globalCss ?? globalReset }} />
        <style
          dangerouslySetInnerHTML={{
            __html: `
          body { flex-direction: column; gap: 20px; }
          .tg-brand { display:flex; align-items:center; gap:12px; text-decoration:none; color:inherit; font-weight:700; font-size:23px; }
          .tg-brand img { width:56px; height:56px; display:block; }
          #qr-container { min-width:0; width:min(100%,288px); }
          #qr-container img { max-width:100%; height:auto; }
          @media(max-width:480px) { body { padding:14px; } body > div { padding:24px; } }
        `,
          }}
        />
      </head>
      <body>
        <a class="tg-brand" href="/my/accounts" aria-label="@tg — мои аккаунты">
          <img src={iconUrl} alt="Кастомная иконка @tg" width={56} height={56} />
          <span>@tg</span>
        </a>
        {props.children}
      </body>
    </html>
  );
};
