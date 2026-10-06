import { afterEach, describe, expect, it, vi } from "vitest";
import { onRequest } from "../functions/[[path]]";

const env = { EVIDENCE_LAB_BACKEND_ORIGIN: "https://api.example.com" };

afterEach(() => vi.unstubAllGlobals());

describe("Pages backend proxy", () => {
  it("forwards upload bodies, operator headers and query parameters on the configured origin", async () => {
    const fetchMock = vi.fn(async (_request: Request, _options: RequestInit) => {
      return new Response('{"id":"job"}', { status: 202, headers: { "Set-Cookie": "secret=value" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const response = await onRequest({
      env,
      request: new Request("https://dashboard.example.com/api/documents?corpus=default", {
        method: "POST",
        body: "document content",
        headers: {
          Authorization: "Bearer operator",
          Origin: "https://dashboard.example.com",
          Cookie: "session=value",
        },
      }),
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    const call = fetchMock.mock.calls[0];
    if (!call) throw new Error("Expected a backend request");
    const [request, options] = call;
    expect(request.url).toBe("https://api.example.com/api/documents?corpus=default");
    expect(request.method).toBe("POST");
    expect(await request.text()).toBe("document content");
    const headers = new Headers(options.headers);
    expect(headers.get("Authorization")).toBe("Bearer operator");
    expect(headers.get("Origin")).toBe("https://api.example.com");
    expect(headers.has("Cookie")).toBe(false);
    expect(options.redirect).toBe("manual");
    expect(response.status).toBe(202);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(response.headers.has("Set-Cookie")).toBe(false);
    expect(await response.json()).toEqual({ id: "job" });
  });

  it.each([
    undefined,
    "http://api.example.com",
    "https://user:pass@api.example.com",
    "https://api.example.com/path",
  ])("fails closed for invalid backend configuration %s", async (backend) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await onRequest({
      env: { EVIDENCE_LAB_BACKEND_ORIGIN: backend },
      request: new Request("https://dashboard.example.com/api/queries"),
    });
    expect(response.status).toBe(503);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects cross-origin writes before contacting the backend", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await onRequest({
      env,
      request: new Request("https://dashboard.example.com/api/queries", {
        method: "POST",
        headers: { Origin: "https://attacker.example.com" },
      }),
    });
    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves backend authentication failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json({ detail: "Unauthorized" }, { status: 401 })),
    );
    const response = await onRequest({
      env,
      request: new Request("https://dashboard.example.com/api/queries"),
    });
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ detail: "Unauthorized" });
  });

  it("blocks redirects and hides network error details", async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.redirect("https://other.example.com"));
    vi.stubGlobal("fetch", fetchMock);
    const request = new Request("https://dashboard.example.com/health/ready");
    expect((await onRequest({ env, request })).status).toBe(502);
    fetchMock.mockRejectedValue(new Error("credential=secret"));
    const response = await onRequest({ env, request });
    expect(response.status).toBe(502);
    expect(await response.text()).not.toContain("secret");
  });
});
