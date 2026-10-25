import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, AuthError, type ReviewItem } from "./api";

const item = { run_id: "run/1", opportunity_id: "006jV000002RKcsQAG" } as ReviewItem;

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("review API client", () => {
  it("sends the bearer token and a JSON body to an encoded path", async () => {
    const fetch = mockFetch(200, { review_id: "r", action: "approve", task_id: "00T1" });
    await api.approve("secret-token", item, { note: "ok" });
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/reviews/run%2F1/006jV000002RKcsQAG/approve");
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer secret-token");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ note: "ok" });
  });

  it("never sends the token to /health", async () => {
    const fetch = mockFetch(200, { ok: true, dry_run: true });
    await api.health();
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
  });

  it("maps 401 to AuthError and other errors to ApiError with the server's detail", async () => {
    mockFetch(401, { detail: "missing or invalid bearer token" });
    await expect(api.reviews("bad")).rejects.toBeInstanceOf(AuthError);

    mockFetch(409, { detail: "this opportunity already has an open Deal Desk Agent task" });
    const err = await api.approve("t", item, {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).message).toContain("already has an open");
  });
});
