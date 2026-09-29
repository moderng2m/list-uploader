import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { authorizeUrl, completeSignIn, idToken, pkceChallenge, setConfig } from "../auth";

const CONFIG = {
  env: "dev",
  region: "us-east-1",
  apiUrl: "https://api.execute-api.us-east-1.amazonaws.com",
  userPoolId: "us-east-1_demo",
  clientId: "client123",
  cognitoDomain: "https://list-uploader-dev-1.auth.us-east-1.amazoncognito.com",
  redirectUri: "https://site.example/auth/callback",
  logoutUri: "https://site.example/",
};

function jwt(expSeconds: number): string {
  const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
  return `${b64({ alg: "none" })}.${b64({ exp: expSeconds, email: "u@example.com" })}.sig`;
}

function tokenEndpoint(response: object, calls: { url: string; body: URLSearchParams }[], status = 200) {
  return (async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), body: new URLSearchParams(String(init?.body)) });
    return new Response(JSON.stringify(response), { status });
  }) as typeof fetch;
}

beforeEach(() => setConfig(CONFIG));
afterEach(() => {
  sessionStorage.clear();
  setConfig(null);
});

describe("PKCE sign-in", () => {
  it("uses the S256 challenge: base64url(sha256(verifier)), unpadded", async () => {
    // Expected value computed independently with Python's hashlib.
    expect(await pkceChallenge("synthetic-verifier-0123456789")).toBe("aWfUBlh6GVeQdUeCfQO_rZ2TmybJlPT7as7fWF3N8Xc");
  });

  it("sends the user to the hosted sign-in with a challenge and state", async () => {
    const url = new URL(await authorizeUrl("/history"));
    expect(url.origin + url.pathname).toBe(`${CONFIG.cognitoDomain}/oauth2/authorize`);
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("redirect_uri")).toBe(CONFIG.redirectUri);
    const pending = JSON.parse(sessionStorage.getItem("lu.pkce")!);
    expect(url.searchParams.get("state")).toBe(pending.state);
    expect(url.searchParams.get("code_challenge")).toBe(await pkceChallenge(pending.verifier));
  });

  it("exchanges the code with the verifier and returns to the page", async () => {
    await authorizeUrl("/jobs/j1/analysis");
    const { state, verifier } = JSON.parse(sessionStorage.getItem("lu.pkce")!);
    const calls: { url: string; body: URLSearchParams }[] = [];
    const exp = Math.floor(Date.now() / 1000) + 3600;
    const back = await completeSignIn(
      `?code=abc&state=${state}`,
      tokenEndpoint({ id_token: jwt(exp), refresh_token: "r1" }, calls),
    );
    expect(back).toBe("/jobs/j1/analysis");
    expect(calls[0]!.url).toBe(`${CONFIG.cognitoDomain}/oauth2/token`);
    expect(calls[0]!.body.get("code_verifier")).toBe(verifier);
    expect(calls[0]!.body.get("grant_type")).toBe("authorization_code");
    expect(await idToken()).toBe(jwt(exp));
    expect(sessionStorage.getItem("lu.pkce")).toBeNull();
  });

  it("refuses a callback whose state doesn't match", async () => {
    await authorizeUrl("/");
    await expect(completeSignIn("?code=abc&state=forged", tokenEndpoint({}, []))).rejects.toThrow(/stale/);
    expect(await idToken()).toBeNull();
  });

  it("never returns to another site", async () => {
    await authorizeUrl("//evil.example/");
    const { state } = JSON.parse(sessionStorage.getItem("lu.pkce")!);
    const exp = Math.floor(Date.now() / 1000) + 3600;
    expect(await completeSignIn(`?code=c&state=${state}`, tokenEndpoint({ id_token: jwt(exp) }, []))).toBe("/");
  });
});

describe("tokens", () => {
  it("refreshes an ID token that is about to expire", async () => {
    const now = Date.now();
    sessionStorage.setItem(
      "lu.tokens",
      JSON.stringify({ id_token: "old", refresh_token: "r1", expires_at: now + 30_000 }),
    );
    const calls: { url: string; body: URLSearchParams }[] = [];
    const fresh = jwt(Math.floor(now / 1000) + 3600);
    expect(await idToken(tokenEndpoint({ id_token: fresh }, calls), now)).toBe(fresh);
    expect(calls[0]!.body.get("grant_type")).toBe("refresh_token");
    // The refresh token is kept when Cognito doesn't send a new one.
    expect(JSON.parse(sessionStorage.getItem("lu.tokens")!).refresh_token).toBe("r1");
  });

  it("asks for sign-in again when the refresh fails", async () => {
    sessionStorage.setItem("lu.tokens", JSON.stringify({ id_token: "old", refresh_token: "r1", expires_at: 0 }));
    expect(await idToken(tokenEndpoint({ error: "invalid_grant" }, [], 400))).toBeNull();
    expect(sessionStorage.getItem("lu.tokens")).toBeNull();
  });
});
