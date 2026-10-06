import { afterEach, describe, expect, it, vi } from "vitest";
import { useDashboard } from "../src/composables/useDashboard";
import { liveSettings } from "./fixtures/liveSettings";

const cleanups: (() => void)[] = [];
afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});
function workspace() {
  const dashboard = useDashboard();
  cleanups.push(dashboard.actions.dispose);
  return dashboard;
}
function response(value: unknown) {
  return new Response(JSON.stringify(value));
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("workspace asynchronous boundaries", () => {
  it("does not replace the selected corpus with stale documents", async () => {
    const stale = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string) => {
        if (path === "/api/documents?corpus_id=default") return stale.promise;
        if (path.startsWith("/api/documents"))
          return Promise.resolve(
            response({ documents: [{ id: "new", name: "New corpus document", state: "ready" }] }),
          );
        if (path.startsWith("/api/runs")) return Promise.resolve(response({ runs: [] }));
        return Promise.resolve(response({ mode: "mock" }));
      }),
    );
    const { state, actions } = workspace();
    const oldRequest = actions.refreshDocuments();
    await actions.selectCorpus("other");
    stale.resolve(response({ documents: [{ id: "old", name: "Stale", state: "ready" }] }));
    await oldRequest;
    expect(state.corpusId).toBe("other");
    expect(state.documents.map((doc) => doc.id)).toEqual(["new"]);
  });

  it("ignores a query submission that completes after corpus selection changes", async () => {
    const query = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string) => {
        if (path === "/api/queries") return query.promise;
        return Promise.resolve(response({ documents: [], runs: [], mode: "mock" }));
      }),
    );
    const { state, actions } = workspace();
    const submission = actions.submitQuestion("Question in default");
    await actions.selectCorpus("other");
    query.resolve(response({ id: "old-run", job_id: "old-job", status: "queued" }));
    await submission;
    expect(state.run).toBeNull();
    expect(state.busy.question).toBe(false);
  });

  it("stops polling, aborts pending requests and erases the token on disposal", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValue(response({ id: "run", job_id: "job", status: "running" }));
    vi.stubGlobal("fetch", fetch);
    const { state, actions } = workspace();
    state.token = "private-token";
    const polling = actions.openRun("run");
    await vi.advanceTimersByTimeAsync(0);
    expect(fetch).toHaveBeenCalledOnce();
    actions.dispose();
    await polling;
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetch).toHaveBeenCalledOnce();
    expect(state.token).toBe("");
    expect(vi.getTimerCount()).toBe(0);

    let signal: AbortSignal | undefined;
    const blocked = workspace();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_path: string, options: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            signal = options.signal ?? undefined;
            signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), {
              once: true,
            });
          }),
      ),
    );
    const request = blocked.actions.refreshDocuments();
    blocked.actions.dispose();
    await expect(request).rejects.toMatchObject({ name: "AbortError" });
    expect(signal?.aborted).toBe(true);
  });

  it("keeps only the most recently opened source preview", async () => {
    const oldSource = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string) =>
        path.endsWith("old")
          ? oldSource.promise
          : Promise.resolve(
              response({ version: { name: "New", pages: [{ page: 1, text: "Current source" }] } }),
            ),
      ),
    );
    const { state, actions } = workspace();
    const old = actions.previewSource("old", "Old");
    await actions.previewSource("new", "New");
    oldSource.resolve(response({ version: { name: "Old", pages: [{ page: 1, text: "Stale source" }] } }));
    await old;
    expect(state.source.versionId).toBe("new");
    expect(state.source.pages[0]?.text).toBe("Current source");
  });

  it("uploads a replacement to the captured corpus even if selection changes", async () => {
    const upload = deferred<Response>();
    const fetch = vi.fn((path: string, _options?: RequestInit) =>
      path === "/api/documents" ? upload.promise : Promise.resolve(response({ documents: [], runs: [] })),
    );
    vi.stubGlobal("fetch", fetch);
    const { state, actions } = workspace();
    const submission = actions.uploadFiles([new File(["New source"], "replacement.txt")], "doc", "original");
    state.corpusId = "other";
    upload.resolve(response({ duplicate: true }));
    await submission;
    const options = fetch.mock.calls.find((call) => call[0] === "/api/documents")?.[1] as
      | RequestInit
      | undefined;
    expect(options?.body).toBeInstanceOf(FormData);
    const form = options?.body as FormData;
    expect(form.get("corpus_id")).toBe("original");
    expect(form.get("document_id")).toBe("doc");
    expect(state.uploads[0]?.corpusId).toBe("original");
  });

  it("rejects unsupported and oversized uploads without sending a request", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const { state, actions } = workspace();
    state.status = { limits: { max_file_bytes: 2 } };
    await actions.uploadFiles([new File(["x"], "image.exe"), new File(["large"], "large.txt")]);
    expect(fetch.mock.calls.some((call) => (call[1] as RequestInit | undefined)?.method === "POST")).toBe(
      false,
    );
    expect(state.uploads.every((upload) => upload.error)).toBe(true);
  });
});

it("recovers an upload notice when a later refresh observes completed ingestion", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      response({
        documents: [{ id: "doc", latest_job_id: "job", job_status: "succeeded", state: "ready" }],
      }),
    ),
  );
  const { state, actions } = workspace();
  state.uploads = [
    {
      id: 1,
      corpusId: "default",
      name: "source.txt",
      jobId: "job",
      error: true,
      status: "Status unavailable. Refresh to check again.",
    },
  ];
  await actions.refreshDocuments();
  expect(state.uploads[0]?.error).toBe(false);
  expect(state.uploads[0]?.status).toBe("Complete");
});

it("continues the server conversation, then resets it for new questions and corpora", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn((path: string, options?: RequestInit) => {
    if (path === "/api/queries") {
      const body = JSON.parse(String(options?.body));
      return Promise.resolve(
        response({
          id: "run",
          conversation_id: body.conversation_id ?? "server-conversation",
          status: "answered",
        }),
      );
    }
    if (path === "/api/runs/run")
      return Promise.resolve(
        response({ id: "run", conversation_id: "server-conversation", status: "answered" }),
      );
    return Promise.resolve(response({ documents: [], runs: [] }));
  });
  vi.stubGlobal("fetch", fetch);
  const { state, actions } = workspace();
  await actions.submitQuestion("First question");
  await vi.advanceTimersByTimeAsync(0);
  expect(state.conversationId).toBe("server-conversation");
  await actions.submitQuestion("Follow-up question");
  await vi.advanceTimersByTimeAsync(0);
  const submissions = fetch.mock.calls
    .filter(([path]) => path === "/api/queries")
    .map(([, options]) => JSON.parse(String(options?.body)));
  expect(submissions[0]).not.toHaveProperty("conversation_id");
  expect(submissions[1].conversation_id).toBe("server-conversation");
  actions.newConversation();
  expect(state.conversationId).toBeNull();
  expect(state.run).toBeNull();
  expect(state.question).toBe("");
  await actions.submitQuestion("Fresh question");
  await vi.advanceTimersByTimeAsync(0);
  const last = fetch.mock.calls.filter(([path]) => path === "/api/queries").at(-1);
  expect(JSON.parse(String(last?.[1]?.body))).not.toHaveProperty("conversation_id");
  await actions.selectCorpus("other");
  expect(state.conversationId).toBeNull();
});

it("does not restore conversation memory from a submission completed after reset", async () => {
  const query = deferred<Response>();
  vi.stubGlobal(
    "fetch",
    vi.fn(() => query.promise),
  );
  const { state, actions } = workspace();
  const submission = actions.submitQuestion("Old question");
  actions.newConversation();
  query.resolve(response({ id: "old", conversation_id: "old-conversation", status: "queued" }));
  await submission;
  expect(state.conversationId).toBeNull();
  expect(state.run).toBeNull();
});

it("validates mode with key-free metadata, keeps scoped keys, and clears the old conversation", async () => {
  const fetch = vi.fn(async (path: string, options?: RequestInit) => {
    const mode = new Headers(options?.headers).get("X-Evidence-Lab-Mode") || "mock";
    return response(
      path.startsWith("/api/status")
        ? {
            mode,
            credentials: "browser",
            config_fingerprint: mode,
            byok_key_scope: "stable",
            byok_profiles: [{ name: "chat", model: "chat", roles: ["generator"], key_group: "llm" }],
            embedding_space_matches: false,
          }
        : { items: [] },
    );
  });
  vi.stubGlobal("fetch", fetch);
  const { state, actions, credentials } = workspace();
  await actions.refreshStatus();
  credentials.save("llm", "browser-secret");
  credentials.savePreferences(undefined, liveSettings);
  state.conversationId = "old-conversation";
  state.run = { id: "old", status: "answered" };
  await actions.switchMode("live");
  expect(state.mode).toBe("live");
  expect(state.status?.embedding_space_matches).toBe(false);
  expect(state.conversationId).toBeNull();
  expect(state.run).toBeNull();
  expect(credentials.saved.llm).toBe(true);
  expect(credentials.mode.value).toBe("live");
  const validation = fetch.mock.calls.find(
    ([path, options]) =>
      path.startsWith("/api/status") && new Headers(options?.headers).get("X-Evidence-Lab-Mode") === "live",
  );
  expect(validation).toBeDefined();
  const headers = new Headers(validation?.[1]?.headers);
  expect(headers.has("X-Evidence-Lab-Provider-Keys")).toBe(false);
  expect(JSON.parse(headers.get("X-Evidence-Lab-Live-Settings") || "{}")).toEqual(liveSettings);
  expect(fetch.mock.calls.every(([path]) => !path.includes("queries"))).toBe(true);
});
it("reverts failed mode selection without persisting it or losing the existing answer", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_path: string, options?: RequestInit) => {
      return new Headers(options?.headers).get("X-Evidence-Lab-Mode") === "live"
        ? new Response(JSON.stringify({ detail: "Embedding endpoint is not configured correctly." }), {
            status: 422,
          })
        : response({ mode: "mock", byok_key_scope: "stable" });
    }),
  );
  const { state, actions, credentials } = workspace();
  await actions.refreshStatus();
  credentials.savePreferences(undefined, liveSettings);
  state.run = { id: "existing", status: "answered" };
  await actions.switchMode("live");
  expect(state.mode).toBe("mock");
  expect(state.status?.mode).toBe("mock");
  expect(state.run?.id).toBe("existing");
  expect(state.modeError).toContain("Embedding endpoint");
  expect(credentials.mode.value).toBeUndefined();
});

it("operator sign-in adopts server mode, clears the current answer and preserves browser setup", async () => {
  const fetch = vi.fn(async (_path: string, options?: RequestInit) => {
    const headers = new Headers(options?.headers);
    return response({
      mode: headers.has("Authorization") ? "mock" : "live",
      credentials: "server",
      byok_key_scope: "login",
      corpora: [],
      documents: [],
      runs: [],
      evaluations: [],
    });
  });
  vi.stubGlobal("fetch", fetch);
  const { state, credentials, actions } = workspace();
  credentials.configure("login", []);
  credentials.savePreferences("live", liveSettings);
  state.mode = "live";
  state.run = { status: "answered", answer: "Previous browser answer" };
  state.conversationId = "previous";
  await actions.connect("operator-token");
  expect(state.token).toBe("operator-token");
  expect(state.mode).toBe("mock");
  expect(state.run).toBeNull();
  expect(state.conversationId).toBeNull();
  expect(credentials.settings.value).toEqual(liveSettings);
  expect(credentials.mode.value).toBe("live");
  for (const [, options] of fetch.mock.calls)
    expect(new Headers(options?.headers).has("X-Evidence-Lab-Mode")).toBe(false);
  await actions.switchMode("live");
  expect(state.mode).toBe("mock");
});

it("failed operator sign-in restores browser mode and discards the rejected token", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ detail: "Invalid token" }), { status: 401 })),
  );
  const { state, actions } = workspace();
  state.mode = "live";
  state.status = { mode: "live", credentials: "browser" };
  await actions.connect("rejected-token");
  expect(state.token).toBe("");
  expect(state.mode).toBe("live");
  expect(state.connectionError).toContain("Invalid token");
});

it("sign-out clears the session token and restores saved browser mode", async () => {
  const fetch = vi.fn(async (_path: string, options?: RequestInit) => {
    const headers = new Headers(options?.headers);
    return response({
      mode: headers.get("X-Evidence-Lab-Mode") || "mock",
      credentials: headers.get("X-Evidence-Lab-Mode") === "live" ? "browser" : "server",
      byok_key_scope: "logout",
      corpora: [],
      documents: [],
      runs: [],
      evaluations: [],
    });
  });
  vi.stubGlobal("fetch", fetch);
  const { state, credentials, actions } = workspace();
  credentials.configure("logout", []);
  credentials.savePreferences("live", liveSettings);
  state.token = "operator-token";
  state.mode = "mock";
  await actions.signOut();
  expect(state.token).toBe("");
  expect(state.mode).toBe("live");
  expect(state.status?.credentials).toBe("browser");
  for (const [, options] of fetch.mock.calls)
    expect(new Headers(options?.headers).has("Authorization")).toBe(false);
});
