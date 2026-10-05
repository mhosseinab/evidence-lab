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
