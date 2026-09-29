// Sign-in for the live SPA: Cognito hosted sign-in, authorization code + PKCE, no
// client secret. Tokens live in sessionStorage, so they end with the tab. The ID
// token goes to the API (it carries the email and groups the BFF authorizes on).

export interface RuntimeConfig {
  env: string;
  region: string;
  apiUrl: string;
  userPoolId: string;
  clientId: string;
  /** e.g. https://list-uploader-dev-123.auth.us-east-1.amazoncognito.com */
  cognitoDomain: string;
  redirectUri: string;
  logoutUri: string;
}

interface Tokens {
  id_token: string;
  refresh_token?: string;
  /** Epoch ms when the ID token expires. */
  expires_at: number;
}

const TOKENS = "lu.tokens";
const PENDING = "lu.pkce";
// Refresh a minute early so a request never goes out with a token about to expire.
const SKEW_MS = 60_000;

let config: RuntimeConfig | null = null;

export async function loadConfig(fetchImpl: typeof fetch = fetch): Promise<RuntimeConfig> {
  const res = await fetchImpl("/config.json", { cache: "no-store" });
  if (!res.ok) throw new Error("The app's configuration couldn't be loaded. Refresh the page.");
  config = (await res.json()) as RuntimeConfig;
  return config;
}

export function setConfig(c: RuntimeConfig | null) {
  config = c;
}

export function getConfig(): RuntimeConfig {
  if (!config) throw new Error("config not loaded");
  return config;
}

function base64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(bytes = 32): string {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export async function pkceChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** Where the browser goes to sign in; remembers the verifier, state and return path. */
export async function authorizeUrl(returnTo: string): Promise<string> {
  const c = getConfig();
  const verifier = randomString(48);
  const state = randomString(16);
  sessionStorage.setItem(PENDING, JSON.stringify({ verifier, state, returnTo }));
  const q = new URLSearchParams({
    response_type: "code",
    client_id: c.clientId,
    redirect_uri: c.redirectUri,
    scope: "openid email profile",
    state,
    code_challenge: await pkceChallenge(verifier),
    code_challenge_method: "S256",
  });
  return `${c.cognitoDomain}/oauth2/authorize?${q.toString()}`;
}

export async function signIn(returnTo = location.pathname + location.search): Promise<never> {
  location.assign(await authorizeUrl(returnTo));
  // The page is navigating away.
  return new Promise<never>(() => undefined);
}

function jwtExpiry(token: string): number {
  const payload = JSON.parse(atob(token.split(".")[1]!.replace(/-/g, "+").replace(/_/g, "/"))) as { exp: number };
  return payload.exp * 1000;
}

async function tokenRequest(body: Record<string, string>, fetchImpl: typeof fetch): Promise<Tokens> {
  const c = getConfig();
  const res = await fetchImpl(`${c.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: c.clientId, ...body }).toString(),
  });
  if (!res.ok) throw new Error("Sign-in failed. Try again.");
  const data = (await res.json()) as { id_token: string; refresh_token?: string };
  return { id_token: data.id_token, refresh_token: data.refresh_token, expires_at: jwtExpiry(data.id_token) };
}

function save(tokens: Tokens) {
  sessionStorage.setItem(TOKENS, JSON.stringify(tokens));
}

function stored(): Tokens | null {
  const raw = sessionStorage.getItem(TOKENS);
  return raw ? (JSON.parse(raw) as Tokens) : null;
}

/** Finish sign-in on /auth/callback. Returns the path to go back to. */
export async function completeSignIn(search: string, fetchImpl: typeof fetch = fetch): Promise<string> {
  const params = new URLSearchParams(search);
  const pending = JSON.parse(sessionStorage.getItem(PENDING) ?? "null") as
    | { verifier: string; state: string; returnTo: string }
    | null;
  sessionStorage.removeItem(PENDING);
  if (params.get("error")) throw new Error("Sign-in was cancelled or refused. Try again.");
  const code = params.get("code");
  if (!pending || !code || params.get("state") !== pending.state)
    throw new Error("This sign-in link is stale. Sign in again.");
  save(
    await tokenRequest(
      {
        grant_type: "authorization_code",
        code,
        redirect_uri: getConfig().redirectUri,
        code_verifier: pending.verifier,
      },
      fetchImpl,
    ),
  );
  return pending.returnTo.startsWith("/") && !pending.returnTo.startsWith("//") ? pending.returnTo : "/";
}

/** A valid ID token, refreshed if needed; null when the user must sign in again. */
export async function idToken(fetchImpl: typeof fetch = fetch, now = Date.now()): Promise<string | null> {
  const t = stored();
  if (!t) return null;
  if (t.expires_at - SKEW_MS > now) return t.id_token;
  if (!t.refresh_token) return null;
  try {
    const fresh = await tokenRequest({ grant_type: "refresh_token", refresh_token: t.refresh_token }, fetchImpl);
    save({ ...fresh, refresh_token: fresh.refresh_token ?? t.refresh_token });
    return fresh.id_token;
  } catch {
    sessionStorage.removeItem(TOKENS);
    return null;
  }
}

export function signOut(): void {
  const c = getConfig();
  sessionStorage.removeItem(TOKENS);
  const q = new URLSearchParams({ client_id: c.clientId, logout_uri: c.logoutUri });
  location.assign(`${c.cognitoDomain}/logout?${q.toString()}`);
}

export const isLive = (): boolean => import.meta.env.VITE_API_MODE === "live";
