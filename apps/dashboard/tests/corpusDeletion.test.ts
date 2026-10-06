import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import { defineComponent, h } from "vue";
import WorkspaceDialogs from "../src/components/WorkspaceDialogs.vue";
import { provideDashboard } from "../src/composables/dashboardContext";
import { useDashboard } from "../src/composables/useDashboard";

const cleanups: (() => void)[] = [];
afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup();
  vi.unstubAllGlobals();
});
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function workspace() {
  const dashboard = useDashboard();
  dashboard.state.corpora = [{ id: "default" }, { id: "remaining" }];
  cleanups.push(dashboard.actions.dispose);
  return dashboard;
}
it("requires a matching ID and cancellation never sends a deletion", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  const dashboard = workspace();
  const wrapper = mount(
    defineComponent({
      setup() {
        provideDashboard(dashboard);
        return () => h(WorkspaceDialogs);
      },
    }),
  );
  cleanups.push(() => wrapper.unmount());
  for (const dialog of wrapper.findAll("dialog")) {
    const element = dialog.element as HTMLDialogElement;
    element.showModal = () => {
      element.open = true;
    };
    element.close = () => {
      element.open = false;
    };
  }
  dashboard.actions.requestCorpusDeletion();
  await flushPromises();
  expect(wrapper.get("#delete-corpus-dialog").text()).toContain("This cannot be undone");
  expect(wrapper.get<HTMLButtonElement>("#delete-corpus-submit").element.disabled).toBe(true);
  await wrapper.get("#delete-corpus-confirmation").setValue("other");
  await wrapper.get("#delete-corpus-form").trigger("submit");
  expect(fetch).not.toHaveBeenCalled();
  await wrapper.get("#delete-corpus-confirmation").setValue("default");
  expect(wrapper.get<HTMLButtonElement>("#delete-corpus-submit").element.disabled).toBe(false);
  await wrapper.get("#delete-corpus-cancel").trigger("click");
  expect(dashboard.state.dialog).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
});
it("purges selected workspace state and selects the remaining corpus", async () => {
  const fetch = vi.fn(async (path: string) => {
    if (path === "/api/corpora/default") return response({ corpus_id: "default", deleted: true });
    if (path === "/api/corpora") return response({ corpora: [{ id: "remaining" }] });
    if (path.startsWith("/api/documents")) return response({ documents: [{ id: "new-source" }] });
    return response({ mode: "mock", runs: [] });
  });
  vi.stubGlobal("fetch", fetch);
  const { state, actions } = workspace();
  state.token = "operator";
  state.run = { id: "deleted-run" };
  state.conversationId = "deleted-conversation";
  state.source.pages = [{ text: "deleted text" }];
  state.trace.content = "deleted diagnostic";
  state.uploads = [{ id: 1, corpusId: "default", name: "old.txt", status: "Complete", error: false }];
  actions.requestCorpusDeletion();
  await actions.deleteCorpus("default");
  expect(fetch.mock.calls[0]).toEqual([
    "/api/corpora/default",
    expect.objectContaining({ method: "DELETE" }),
  ]);
  expect(state.corpusId).toBe("remaining");
  expect(state.documents.map((doc) => doc.id)).toEqual(["new-source"]);
  expect(state.run).toBeNull();
  expect(state.conversationId).toBeNull();
  expect(state.source.pages).toEqual([]);
  expect(state.trace.content).toBe("");
  expect(state.uploads).toEqual([]);
  expect(state.token).toBe("operator");
});
it("offers creation after deleting the final corpus without recreating default", async () => {
  const fetch = vi.fn(async (path: string) =>
    path === "/api/corpora/default" ? response({ deleted: true }) : response({ corpora: [] }),
  );
  vi.stubGlobal("fetch", fetch);
  const { state, actions } = workspace();
  actions.requestCorpusDeletion();
  await actions.deleteCorpus("default");
  expect(state.corpusId).toBe("");
  expect(state.dialog).toBe("new-corpus");
  await Promise.all([
    actions.refreshStatus(),
    actions.refreshDocuments(),
    actions.refreshRuns(),
    actions.submitQuestion("question"),
    actions.uploadFiles([new File(["text"], "file.txt")]),
  ]);
  expect(fetch).toHaveBeenCalledTimes(2);
});
it("keeps existing workspace and displays an actionable purge failure", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => response({ error: "Wait for active work to finish." }, 409)),
  );
  const { state, actions } = workspace();
  state.documents = [{ id: "keep" }];
  actions.requestCorpusDeletion();
  await actions.deleteCorpus("default");
  expect(state.corpusId).toBe("default");
  expect(state.documents[0]?.id).toBe("keep");
  expect(state.dialog).toBe("delete-corpus");
  expect(state.corpusError).toContain("Wait for active work");
  expect(state.busy.deleteCorpus).toBe(false);
});
it("ignores pending status and source responses after purge", async () => {
  let finishStatus!: (value: Response) => void;
  let finishSource!: (value: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string) => {
      if (path.startsWith("/api/status"))
        return new Promise<Response>((resolve) => {
          finishStatus = resolve;
        });
      if (path.startsWith("/api/source-versions"))
        return new Promise<Response>((resolve) => {
          finishSource = resolve;
        });
      return Promise.resolve(response(path === "/api/corpora/default" ? { deleted: true } : { corpora: [] }));
    }),
  );
  const { state, actions } = workspace();
  const status = actions.refreshStatus();
  const source = actions.previewSource("old-source", "Old source");
  actions.requestCorpusDeletion();
  await actions.deleteCorpus("default");
  finishStatus(response({ mode: "live" }));
  finishSource(response({ version: { pages: [{ text: "deleted source" }] } }));
  await Promise.all([status, source]);
  expect(state.status).toBeNull();
  expect(state.source.pages).toEqual([]);
  expect(state.dialog).toBe("new-corpus");
});
it("clears a nonexistent selected corpus after refreshing an empty workspace list", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => response({ corpora: [] })),
  );
  const { state, actions } = workspace();
  state.source.pages = [{ text: "deleted content" }];
  state.run = { id: "deleted run" };
  await actions.refreshCorpora();
  expect(state.corpusId).toBe("");
  expect(state.source.pages).toEqual([]);
  expect(state.run).toBeNull();
});
it.each([{ corpora: [] }, { corpora: [{ id: "remaining" }] }])(
  "signs in after default is deleted with remaining corpora %j",
  async ({ corpora }) => {
    const fetch = vi.fn(async (path: string) => {
      if (path === "/api/corpora") return response({ corpora });
      if (path.includes("corpus_id=default")) return response({ error: "Corpus not found" }, 404);
      return response({ mode: "live", credentials: "server", documents: [], runs: [], evaluations: [] });
    });
    vi.stubGlobal("fetch", fetch);
    const { state, actions } = workspace();
    state.accessRequired = true;
    await actions.connect("operator-token");
    expect(state.token).toBe("operator-token");
    expect(state.connectionError).toBe("");
    expect(state.accessRequired).toBe(false);
    expect(state.corpusId).toBe(corpora.length ? "remaining" : "");
    expect(state.dialog).toBe(corpora.length ? null : "new-corpus");
    expect(fetch.mock.calls.every(([path]) => !path.includes("corpus_id=default"))).toBe(true);
  },
);
it.each([{ corpora: [] }, { corpora: [{ id: "remaining" }] }])(
  "initializes after default is deleted with remaining corpora %j",
  async ({ corpora }) => {
    const fetch = vi.fn(async (path: string) => {
      if (path === "/api/corpora") return response({ corpora });
      if (path.includes("corpus_id=default")) return response({ error: "Corpus not found" }, 404);
      return response({ mode: "mock", documents: [], runs: [] });
    });
    vi.stubGlobal("fetch", fetch);
    const { state, actions } = workspace();
    await actions.initialize();
    expect(state.corpusId).toBe(corpora.length ? "remaining" : "");
    expect(state.notice).toBe("");
    expect(fetch.mock.calls.every(([path]) => !path.includes("corpus_id=default"))).toBe(true);
  },
);
