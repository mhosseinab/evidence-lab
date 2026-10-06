import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type Component, defineComponent, h } from "vue";
import AnswerPanel from "../src/components/AnswerPanel.vue";
import { provideDashboard } from "../src/composables/dashboardContext";
import { useDashboard } from "../src/composables/useDashboard";
import type { Payload } from "../src/types/api";
import AskView from "../src/views/AskView.vue";
import DocumentsView from "../src/views/DocumentsView.vue";
import EvaluationsView from "../src/views/EvaluationsView.vue";
import { liveSettings } from "./fixtures/liveSettings";

const cleanups: (() => void)[] = [];
afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup();
  vi.unstubAllGlobals();
});
function workspace(component: Component) {
  const dashboard = useDashboard();
  const wrapper: VueWrapper = mount(
    defineComponent({
      setup() {
        provideDashboard(dashboard);
        return () => h(component);
      },
    }),
  );
  cleanups.push(() => {
    wrapper.unmount();
    dashboard.actions.dispose();
  });
  return { ...dashboard, wrapper };
}

describe("answer release boundary", () => {
  it.each([
    "running",
    "failed",
    "shadow",
    "policy_not_ready",
    "abstained",
    "verification_unavailable",
    "cancelled",
  ])("hides answer fields for %s", async (status) => {
    const { state, wrapper } = workspace(AnswerPanel);
    state.run = {
      status,
      answer: "PRIVATE ANSWER",
      blocks: [{ text: "PRIVATE BLOCK" }],
      draft: "PRIVATE DRAFT",
    };
    await flushPromises();
    expect(wrapper.text()).not.toContain("PRIVATE");
    expect(wrapper.find("#copy-answer").exists()).toBe(false);
  });

  it("renders released untrusted text literally and connects citations", async () => {
    const { state, wrapper } = workspace(AnswerPanel);
    const content = '<img src=x onerror="alert(1)">';
    state.evidence = [{ id: "e1", title: "Policy", text: "Source", version_id: "v1" }];
    state.run = { status: "answered", answer: [{ text: content, citation_ids: ["e1"] }] };
    await flushPromises();
    expect(wrapper.find(".answer-text").text()).toBe(content);
    expect(wrapper.find("img").exists()).toBe(false);
    await wrapper.find(".citation-button").trigger("click");
    expect(wrapper.findComponent(AnswerPanel).emitted("citation")).toEqual([[0]]);
  });

  it.each([
    { workspaceMode: "mock", runMode: "live", qualification: undefined, fixture: false },
    { workspaceMode: "live", runMode: "mock", qualification: undefined, fixture: true },
    { workspaceMode: "live", runMode: undefined, qualification: "fixture_only", fixture: true },
    { workspaceMode: "mock", runMode: undefined, qualification: undefined, fixture: true },
  ])("labels historical answer provenance using $runMode / $qualification", async (scenario) => {
    const { state, wrapper } = workspace(AnswerPanel);
    state.status = { mode: scenario.workspaceMode };
    state.run = {
      status: "answered",
      mode: scenario.runMode,
      qualification: scenario.qualification,
      answer: "Released historical answer",
    };
    await flushPromises();

    expect(wrapper.get("#run-answer").text()).toContain("Released historical answer");
    expect(wrapper.get("#run-meta").text().includes("Deterministic fixture")).toBe(scenario.fixture);
  });
});

it("submits controlled question state and supports a suggestion", async () => {
  const { state, actions, wrapper } = workspace(AskView);
  const submit = vi.spyOn(actions, "submitQuestion").mockResolvedValue();
  await wrapper.find('[data-question="What are the support hours?"]').trigger("click");
  expect(state.question).toBe("What are the support hours?");
  await wrapper.find("#question-form").trigger("submit");
  expect(submit).toHaveBeenCalledWith("What are the support hours?");
  state.busy.question = true;
  await flushPromises();
  expect(wrapper.find<HTMLButtonElement>("#ask-submit").element.disabled).toBe(true);
});

it("keeps source preview and new version actions available for documents", async () => {
  const { state, actions, wrapper } = workspace(DocumentsView);
  state.documents = [
    {
      id: "d1",
      document_id: "d1",
      name: "Policy",
      latest_version_id: "v1",
      state: "ready",
      active_version_id: "v1",
    },
  ];
  const preview = vi.spyOn(actions, "previewSource").mockResolvedValue();
  await flushPromises();
  const source = wrapper.findAll("button").find((button) => /preview|open source/i.test(button.text()));
  expect(source).toBeDefined();
  await source?.trigger("click");
  expect(preview).toHaveBeenCalledWith("v1", "Policy");
  expect(wrapper.text()).toContain("Update");
});

it("renders recorded evaluation metrics with mock limitations and exports the result", async () => {
  const { state, actions, wrapper } = workspace(EvaluationsView);
  state.evaluation = {
    id: "evaluation",
    status: "succeeded",
    result: {
      fixture_only: true,
      variants: { ungated: { coverage: { numerator: 1, denominator: 2, rate: 0.5 } } },
      qualification: { qualified: false, human_reviewed: false },
    },
  } as Payload;
  const exportEvaluation = vi.spyOn(actions, "exportEvaluation").mockImplementation(() => {});
  await flushPromises();
  expect(wrapper.text()).toContain("does not establish model accuracy");
  expect(wrapper.text()).toContain("Quality not qualified");
  expect(wrapper.text()).toContain("50.0%");
  await wrapper.find("#evaluation-export").trigger("click");
  expect(exportEvaluation).toHaveBeenCalledOnce();
});

it("shows actual graph execution and offers a fresh conversation without exposing drafts", async () => {
  const { state, wrapper } = workspace(AskView);
  state.status = {
    orchestration: {
      engine: "langgraph",
      nodes: ["retrieve", "generate", "verify", "repair"],
      edges: [
        ["retrieve", "generate"],
        ["generate", "verify"],
      ],
      tools: ["search_documents"],
      memory: { enabled: true, max_turns: 8 },
      tracing: { provider: "langsmith", enabled: true, content: "metadata_only" },
    },
  };
  state.conversationId = "conversation";
  state.run = {
    status: "running",
    stage: "verify",
    draft: "PRIVATE DRAFT",
    graph_steps: [
      { node: "retrieve", status: "completed", elapsed_seconds: 0.1 },
      { node: "generate", status: "completed", elapsed_seconds: 0.2 },
    ],
  };
  await flushPromises();
  expect(wrapper.text()).toContain("LangGraph workflow");
  expect(wrapper.text()).toContain("LangSmith active");
  expect(wrapper.text()).toContain("metadata only");
  if (state.status.orchestration) state.status.orchestration.tracing.content = "provider_payloads";
  await flushPromises();
  expect(wrapper.text()).toContain("records provider request payloads and responses");
  expect(wrapper.text()).not.toContain("text stay private");
  expect(wrapper.findAll('[data-status="completed"]')).toHaveLength(2);
  expect(wrapper.find('[aria-current="step"]').text()).toContain("Verify claims");
  expect(wrapper.find('[data-status="waiting"]').text()).toContain("Not run");
  expect(wrapper.text()).not.toContain("PRIVATE");
  expect(wrapper.find<HTMLButtonElement>("#new-conversation").element.disabled).toBe(true);
  state.run = {
    status: "answered",
    conversation_id: "conversation",
    graph_steps: [{ node: "verify", status: "failed", elapsed_seconds: 0.3 }],
  };
  await flushPromises();
  expect(wrapper.find('[data-status="failed"]').text()).toContain("Failed");
  await wrapper.find("#new-conversation").trigger("click");
  expect(state.conversationId).toBeNull();
  expect(wrapper.text()).toContain("New conversation");
});

describe("browser key management", () => {
  it("saves a masked key locally and removes it without a server request", async () => {
    const { default: BrowserKeySettings } = await import("../src/components/BrowserKeySettings.vue");
    const { state, credentials, wrapper } = workspace(BrowserKeySettings);
    state.status = { mode: "live", credentials: "browser" };
    credentials.configure("component-test", [{ name: "chat", model: "chat-v1", roles: ["generator"] }]);
    await flushPromises();
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    await wrapper.get("#llm-key").setValue("client-only-secret");
    expect(wrapper.get("#llm-key").attributes("type")).toBe("password");
    await wrapper.get("#browser-key-form form").trigger("submit");
    expect(wrapper.text()).toContain("Key saved in this browser");
    expect((wrapper.get("#llm-key").element as HTMLInputElement).value).toBe("");
    expect(wrapper.text()).not.toContain("client-only-secret");
    await wrapper.get("#remove-llm-key").trigger("click");
    expect(wrapper.text()).toContain("Key removed");
    expect(credentials.saved.llm).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
    localStorage.clear();
  });
});

it("shows BYOK settings in workspace connection during fixture mode", async () => {
  const { default: WorkspaceDialogs } = await import("../src/components/WorkspaceDialogs.vue");
  const { state, credentials, wrapper } = workspace(WorkspaceDialogs);
  state.status = { mode: "mock", credentials: "server" };
  credentials.configure("fixture-visibility", [
    { name: "fixture-chat", model: "fixture", roles: ["generator"] },
  ]);
  const dialog = wrapper.get("#connection-dialog").element as HTMLDialogElement;
  Object.defineProperty(dialog, "showModal", {
    value: () => {
      dialog.open = true;
    },
  });
  state.dialog = "connection";
  await flushPromises();
  expect(wrapper.get("#browser-key-form").text()).toContain("Mode and provider keys");
  expect(wrapper.get("#browser-key-form").text()).toContain("Fixture mode");
  expect(wrapper.get("#llm-key").attributes("disabled")).toBeUndefined();
  expect(wrapper.get("#cloudflare-key").attributes("disabled")).toBeUndefined();
  expect(wrapper.find("#workspace-mode").exists()).toBe(true);
});

it("saves both keys and live setup in fixture mode without calling the server", async () => {
  const { default: BrowserKeySettings } = await import("../src/components/BrowserKeySettings.vue");
  const { state, credentials, wrapper } = workspace(BrowserKeySettings);
  state.status = { mode: "mock", byok_key_scope: "form-setup" };
  state.mode = "mock";
  credentials.configure("form-setup", []);
  await flushPromises();
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  expect(wrapper.findAll('input[type="password"]')).toHaveLength(2);
  await wrapper.get("#llm-key").setValue("llm-secret");
  await wrapper.get("#browser-key-form form").trigger("submit");
  await wrapper.get("#cloudflare-key").setValue("cf-secret");
  await wrapper.findAll("#browser-key-form form")[1]?.trigger("submit");
  expect(credentials.saved).toEqual({ llm: true, cloudflare: true });
  await wrapper.get("#embedding-endpoint").setValue("https://provider.example/v1/embeddings");
  await wrapper.get("#embedding-model").setValue("embedding-model");
  await wrapper.get("#chat-endpoint").setValue("https://provider.example/v1/chat/completions");
  await wrapper.get("#chat-model").setValue("chat-model");
  await wrapper.get("#cloudflare-account").setValue("a".repeat(32));
  await wrapper.get("#live-endpoint-form").trigger("submit");
  expect(wrapper.text()).toContain("Enter all three provider prices");
  for (const field of [
    "embedding_input_usd_per_million",
    "chat_input_usd_per_million",
    "chat_output_usd_per_million",
  ])
    await wrapper.get(`#${field}`).setValue("0");
  for (const [id, value] of [
    ["embedding-dimensions", "1536"],
    ["embedding-input-limit", "8192"],
    ["chat-input-limit", "65536"],
    ["chat-output-limit", "1200"],
  ])
    await wrapper.get(`#${id}`).setValue(value);
  await wrapper.get("#output-limit-parameter").setValue("max_tokens");
  await wrapper.get("#live-endpoint-form").trigger("submit");
  expect(wrapper.text()).toContain("Live setup saved in this browser");
  expect(credentials.settings.value?.budget_usd).toBe(0);
  expect(fetch).not.toHaveBeenCalled();
  expect(wrapper.text()).not.toContain("llm-secret");
  await wrapper.get("#remove-cloudflare-key").trigger("click");
  expect(credentials.saved.cloudflare).toBe(false);
  expect(credentials.saved.llm).toBe(true);
  localStorage.clear();
});

it("removes saved live setup in fixture mode, resets the form and retains keys", async () => {
  const { default: LiveEndpointSettings } = await import("../src/components/LiveEndpointSettings.vue");
  const { state, credentials, wrapper } = workspace(LiveEndpointSettings);
  state.status = { mode: "mock" };
  credentials.configure("remove-setup", []);
  credentials.save("llm", "llm-secret");
  credentials.save("cloudflare", "cf-secret");
  credentials.savePreferences("live", { ...liveSettings, budget_usd: 5 });
  state.mode = "live";
  await flushPromises();
  expect(wrapper.get("#remove-live-setup").attributes("disabled")).toBeDefined();
  expect((wrapper.get("#embedding-endpoint").element as HTMLInputElement).value).toContain(
    "provider.example",
  );
  state.mode = "mock";
  await flushPromises();
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  await wrapper.get("#remove-live-setup").trigger("click");
  expect(wrapper.text()).toContain("Live setup removed");
  expect(wrapper.find("#remove-live-setup").exists()).toBe(false);
  expect((wrapper.get("#embedding-endpoint").element as HTMLInputElement).value).toBe("");
  expect((wrapper.get("#live-budget").element as HTMLInputElement).value).toBe("0");
  expect((wrapper.get("#embedding_input_usd_per_million").element as HTMLInputElement).value).toBe("");
  expect(credentials.settings.value).toBeUndefined();
  expect(localStorage.getItem("evidence-lab:connection:remove-setup")).toBeNull();
  expect(credentials.saved).toEqual({ llm: true, cloudflare: true });
  expect(localStorage.getItem("evidence-lab:provider-keys:remove-setup")).toContain("llm-secret");
  expect(fetch).not.toHaveBeenCalled();
  localStorage.clear();
});

it("requires provider-specific limits and pricing instead of guessing model defaults", async () => {
  const { default: BrowserKeySettings } = await import("../src/components/BrowserKeySettings.vue");
  const { state, credentials, wrapper } = workspace(BrowserKeySettings);
  state.status = { mode: "mock" };
  credentials.configure("explicit-limits", []);
  await flushPromises();
  for (const id of [
    "embedding-dimensions",
    "embedding-input-limit",
    "chat-input-limit",
    "chat-output-limit",
    "embedding_input_usd_per_million",
    "chat_input_usd_per_million",
    "chat_output_usd_per_million",
  ]) {
    expect((wrapper.get(`#${id}`).element as HTMLInputElement).value).toBe("");
    expect(wrapper.get(`label[for="${id}"]`).text()).toContain("*");
    expect(wrapper.get(`#${id}`).attributes("required")).toBeDefined();
  }
  expect((wrapper.get("#live-budget").element as HTMLInputElement).value).toBe("0");
  expect((wrapper.get("#structured-output").element as HTMLSelectElement).value).toBe("text_json");
  expect((wrapper.get("#output-limit-parameter").element as HTMLSelectElement).value).toBe("");
  expect(wrapper.text()).toContain("Required for live mode");
  expect(wrapper.get('label[for="llm-key"]').text()).toContain("*");
  expect(wrapper.get('label[for="cloudflare-key"]').text()).toContain("*");
  for (const id of [
    "embedding_input_usd_per_million",
    "chat_input_usd_per_million",
    "chat_output_usd_per_million",
  ])
    await wrapper.get(`#${id}`).setValue("0");
  await wrapper.get("#live-endpoint-form").trigger("submit");
  expect(wrapper.text()).toContain("Enter embedding dimensions and all token limits");
  expect(credentials.settings.value).toBeUndefined();
  for (const [id, value] of [
    ["embedding-dimensions", "768"],
    ["embedding-input-limit", "4096"],
    ["chat-input-limit", "8192"],
    ["chat-output-limit", "512"],
  ])
    await wrapper.get(`#${id}`).setValue(value);
  await wrapper.get("#live-endpoint-form").trigger("submit");
  expect(wrapper.text()).toContain("Select the output limit parameter supported by your chat model");
  expect(credentials.settings.value).toBeUndefined();
  for (const id of [
    "embedding-endpoint",
    "embedding-model",
    "chat-endpoint",
    "chat-model",
    "cloudflare-account",
    "live-budget",
    "structured-output",
    "output-limit-parameter",
  ]) {
    expect(wrapper.get(`label[for="${id}"]`).text()).toContain("*");
    expect(wrapper.get(`#${id}`).attributes("required")).toBeDefined();
  }
  localStorage.clear();
});

it("separates operator sign-in from connection setup and hides browser overrides for operators", async () => {
  const { default: WorkspaceDialogs } = await import("../src/components/WorkspaceDialogs.vue");
  const { state, wrapper } = workspace(WorkspaceDialogs);
  state.status = { mode: "mock" };
  state.dialog = "connection";
  const dialog = wrapper.get("#connection-dialog").element as HTMLDialogElement;
  Object.defineProperty(dialog, "showModal", {
    value: () => {
      dialog.open = true;
    },
  });
  await flushPromises();
  expect(wrapper.get("#connection-dialog").find("#operator-token").exists()).toBe(false);
  expect(wrapper.find("#operator-login-dialog #operator-token").exists()).toBe(true);
  state.token = "operator-token";
  await flushPromises();
  expect(wrapper.get("#connection-dialog").find("#browser-key-form").exists()).toBe(false);
  expect(wrapper.get("#connection-dialog").text()).toContain("Using server configuration");
});

it("saves live chat setup using workspace embeddings without requesting embedding fields", async () => {
  const { default: LiveEndpointSettings } = await import("../src/components/LiveEndpointSettings.vue");
  const { state, credentials, wrapper } = workspace(LiveEndpointSettings);
  state.status = { mode: "mock", profiles: { embeddings: { model: "fixture-hash-embeddings-v1" } } };
  credentials.configure("workspace-vectors", []);
  await flushPromises();
  await wrapper.get("#embedding-source").setValue("workspace");
  expect(wrapper.find("#embedding-endpoint").exists()).toBe(false);
  expect(wrapper.find("#embedding-dimensions").exists()).toBe(false);
  expect(wrapper.find("#embedding_input_usd_per_million").exists()).toBe(false);
  expect(wrapper.text()).toContain("Reuse the workspace's pgvector vectors");
  await wrapper.get("#chat-endpoint").setValue(liveSettings.chat_endpoint);
  await wrapper.get("#chat-model").setValue(liveSettings.chat_model);
  await wrapper.get("#cloudflare-account").setValue(liveSettings.cloudflare_account_id);
  await wrapper.get("#chat-input-limit").setValue("8192");
  await wrapper.get("#chat-output-limit").setValue("512");
  await wrapper.get("#output-limit-parameter").setValue("max_tokens");
  await wrapper.get("#chat_input_usd_per_million").setValue("0");
  await wrapper.get("#chat_output_usd_per_million").setValue("0");
  await wrapper.get("#live-endpoint-form").trigger("submit");
  expect(wrapper.text()).toContain("Live setup saved in this browser");
  const saved = JSON.parse(credentials.settingsHeader());
  expect(saved.embedding_source).toBe("workspace");
  expect(saved.embedding_endpoint).toBeUndefined();
  expect(saved.embedding_dimensions).toBeUndefined();
  localStorage.clear();
});
