import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, createApiClient, parsePayload } from "../src/api/client";

afterEach(() => vi.unstubAllGlobals());

describe("validated API boundary", () => {
  it("attaches only the in-memory token and validates successful payloads", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"id":42}'));
    vi.stubGlobal("fetch", fetch);
    await expect(createApiClient(() => "private-token").request("/api/runs")).rejects.toThrow(/invalid.*id/i);
    const options = fetch.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(options.headers).get("Authorization")).toBe("Bearer private-token");
    expect(options.credentials).toBe("same-origin");
  });

  it("reports unreadable responses and access failures", async () => {
    const accessRequired = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(new Response("not json"))
        .mockResolvedValueOnce(new Response('{"detail":"Access denied"}', { status: 401 })),
    );
    const client = createApiClient(() => "", accessRequired);
    await expect(client.request("/api/status")).rejects.toThrow(/unreadable response/);
    await expect(client.request("/api/status")).rejects.toMatchObject({
      status: 401,
      message: "Access denied",
    });
    expect(accessRequired).toHaveBeenCalledOnce();
  });

  it("keeps multipart boundaries intact and authenticates source downloads", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response('{"id":"job"}'))
      .mockResolvedValueOnce(new Response("original bytes"));
    vi.stubGlobal("fetch", fetch);
    const body = new FormData();
    body.append("file", new File(["source"], "source.txt"));
    const client = createApiClient(() => "operator");
    await client.request("/api/documents", { method: "POST", body });
    expect(
      new Headers((fetch.mock.calls[0]?.[1] as RequestInit | undefined)?.headers).has("Content-Type"),
    ).toBe(false);
    await client.download("/api/source-versions/v1/download");
    expect(
      new Headers((fetch.mock.calls[1]?.[1] as RequestInit | undefined)?.headers).get("Authorization"),
    ).toBe("Bearer operator");
  });

  it("rejects malformed nested answer and evidence fields", () => {
    expect(() => parsePayload({ answer: [{ text: 12 }] })).toThrow(/invalid.*text/i);
    expect(() => parsePayload({ evidence_pack: [{ citation_ids: [12] }] })).toThrow(/invalid.*citation_ids/i);
    expect(() => parsePayload({ answer: { blocks: "private" } })).toThrow(/invalid.*blocks/i);
  });

  it("accepts API version strings, ingestion pages and recorded evaluation metrics", () => {
    parsePayload({ version: "0.1.0", mode: "mock", profiles: { generator: { model: "fixture" } } });
    parsePayload({ result: { status: "needs_review", pages: 2, errors: ["OCR is not enabled"] } });
    parsePayload({ version: { pages: [{ page: 1, text: "Source", errors: ["Review image"] }] } });
    const report: unknown = JSON.parse(readFileSync("tests/fixtures/evaluation-report.json", "utf8"));
    parsePayload({ id: "evaluation", status: "succeeded", result: report });
  });

  it("does not expose an access-denied response as a source blob", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("denied", { status: 403 })));
    await expect(
      createApiClient(() => "").download("/api/source-versions/v1/download"),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

it("validates workflow metadata and rejects unsafe or malformed execution fields", () => {
  const orchestration = {
    engine: "langgraph",
    nodes: ["retrieve", "verify"],
    edges: [["retrieve", "verify"]],
    tools: ["search_documents"],
    memory: { enabled: true, max_turns: 8 },
    tracing: { provider: "langsmith", enabled: true, content: "metadata_only" },
  };
  const payload = parsePayload({
    orchestration,
    conversation_id: "conversation",
    graph_steps: [{ node: "retrieve", status: "completed", elapsed_seconds: 0.2 }],
  });
  expect(payload.orchestration?.engine).toBe("langgraph");
  expect(payload.graph_steps?.[0]?.elapsed_seconds).toBe(0.2);
  expect(() =>
    parsePayload({
      orchestration: { ...orchestration, tracing: { provider: "langsmith", enabled: true, content: "full" } },
    }),
  ).toThrow(/workflow metadata/);
  expect(() => parsePayload({ orchestration: { ...orchestration, edges: [["retrieve"]] } })).toThrow(
    /workflow metadata/,
  );
  expect(() =>
    parsePayload({ graph_steps: [{ node: "verify", status: "completed", elapsed_seconds: -1 }] }),
  ).toThrow(/workflow metadata/);
  expect(() =>
    parsePayload({ graph_steps: [{ node: "verify", status: "private_draft", elapsed_seconds: 1 }] }),
  ).toThrow(/workflow metadata/);
  expect(() => parsePayload({ conversation_id: 1 })).toThrow(/conversation_id/);
});
