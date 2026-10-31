import type { FC } from "hono/jsx";
import { config } from "../config.js";
import { button, card, clientBlock, label, scope, subtitle, title } from "../styles.js";
import { Layout } from "./Layout.js";

interface ConsentPageProps {
  clientId: string;
  clientName: string;
  redirectUri: string;
  /** Canonical destination shown to the human: scheme + host of redirectUri. */
  redirectOriginKey: string;
  state: string;
  codeChallenge: string;
  codeChallengeMethod: string;
}

/**
 * Explicit approval for a callback destination this account has never used.
 *
 * WHY THIS EXISTS: `/oauth/authorize` used to mint an authorization code from
 * nothing but the ambient `tg_user` cookie and hand it to any redirect_uri
 * registered through open RFC 7591 registration — no interaction at all.
 * Since the cookie is SameSite=Lax, a plain link click carried it, so one
 * click on a hostile link handed a stranger full access to the victim's
 * Telegram. Verified against production before the fix.
 *
 * Only UNKNOWN destinations land here; a returning Claude/ChatGPT/Cursor user
 * keeps the silent 302 (see OAuthProvider.hasGrant).
 *
 * The page shows the DESTINATION HOST, not just the client name: `client_name`
 * is attacker-supplied at registration time and can say "Claude", while the
 * host is where the code will actually be delivered and cannot be faked.
 */
export const ConsentPage: FC<ConsentPageProps> = (props) => {
  return (
    <Layout title={`${config.brandName} — Confirm connection`}>
      <div class={card}>
        <h1 class={title}>{config.brandName}</h1>
        <p class={subtitle}>Confirm this connection</p>

        <div class={clientBlock}>
          <strong>{props.clientName || "An MCP client"}</strong> is asking to connect to your Telegram: reading chats
          and messages, and acting on your requests — sending, editing, managing chats.
        </div>

        <p class={label}>
          The access code will be sent to:
          <br />
          <strong>{props.redirectOriginKey}</strong>
        </p>

        <p class={scope}>
          You have not connected this destination before. If you did not start this yourself — for example you just
          followed a link — close this page instead of confirming.
        </p>

        {/* POST, not GET: a GET approval could be triggered by an <img> tag.
            The server additionally requires Origin === issuer, and the session
            cookie is SameSite=Lax so it is not attached to a cross-site POST
            at all — three independent reasons a remote page cannot submit this. */}
        <form method="post" action="/oauth/authorize/approve">
          <input type="hidden" name="client_id" value={props.clientId} />
          <input type="hidden" name="redirect_uri" value={props.redirectUri} />
          <input type="hidden" name="state" value={props.state} />
          <input type="hidden" name="code_challenge" value={props.codeChallenge} />
          <input type="hidden" name="code_challenge_method" value={props.codeChallengeMethod} />
          <button class={button} type="submit">
            Connect
          </button>
        </form>

        <p class={scope}>
          <a href={`${config.issuer}/my/settings`}>Cancel</a>
        </p>
      </div>
    </Layout>
  );
};
