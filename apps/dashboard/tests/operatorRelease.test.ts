import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import { defineComponent, h } from "vue";
import AnswerPanel from "../src/components/AnswerPanel.vue";
import WorkspaceDialogs from "../src/components/WorkspaceDialogs.vue";
import { provideDashboard } from "../src/composables/dashboardContext";
import { useDashboard } from "../src/composables/useDashboard";
import type { Payload } from "../src/types/api";

const cleanups: (() => void)[] = [];
afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup();
  vi.unstubAllGlobals();
});
const shadow: Payload = {
  id: "run-one",
  corpus_id: "default",
  status: "shadow",
  mode: "live",
  code: "policy_not_qualified",
  qualification: "shadow",
};
const released: Payload = {
  ...shadow,
  status: "answered",
  code: "operator_approved_release",
  qualification: "operator_approved",
  answer: [{ id: "b1", text: "Checked answer", citation_ids: ["e1"] }],
  evidence: [{ id: "e1", title: "Source" }],
};
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function workspace() {
  const dashboard = useDashboard();
  Object.assign(dashboard.state, {
    token: "operator",
    status: { credentials: "server", policy_state: "shadow" },
    runId: "run-one",
    run: { ...shadow },
    runs: [{ ...shadow }],
  });
  const wrapper = mount(
    defineComponent({
      setup() {
        provideDashboard(dashboard);
        return () => [h(AnswerPanel), h(WorkspaceDialogs)];
      },
    }),
  );
  cleanups.push(() => {
    wrapper.unmount();
    dashboard.actions.dispose();
  });
  for (const dialog of wrapper.findAll("dialog")) {
    const element = dialog.element as HTMLDialogElement;
    element.showModal = () => {
      element.open = true;
    };
    element.close = () => {
      element.open = false;
    };
  }
  return { ...dashboard, wrapper };
}
it("requires a reason and explicit confirmation, then releases with approval and citations", async () => {
  const fetch = vi.fn(async (_path: string, _options: RequestInit) => response(released));
  vi.stubGlobal("fetch", fetch);
  const { state, wrapper } = workspace();
  await wrapper.get("#operator-release-action").trigger("click");
  await flushPromises();
  expect(wrapper.get("#operator-release-description").text()).toContain("policy remains unqualified");
  expect(wrapper.get<HTMLButtonElement>("#operator-release-submit").element.disabled).toBe(true);
  await wrapper.get("#operator-release-form").trigger("submit");
  expect(fetch).not.toHaveBeenCalled();
  await wrapper.get("#operator-release-reason").setValue(" Reviewed the sources ");
  await wrapper.get("#operator-release-form").trigger("submit");
  await flushPromises();
  expect(fetch).toHaveBeenCalledWith(
    "/api/runs/run-one/operator-release",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ action: "release", reason: "Reviewed the sources" }),
    }),
  );
  const headers = fetch.mock.calls[0]?.[1]?.headers as Headers;
  expect(headers.get("Authorization")).toBe("Bearer operator");
  expect(headers.has("X-Evidence-Lab-Mode")).toBe(false);
  expect(state.run?.status).toBe("answered");
  expect(state.runs[0]?.qualification).toBe("operator_approved");
  expect(state.status?.policy_state).toBe("shadow");
  expect(wrapper.get("#operator-approved-label").text()).toContain("Operator-approved");
  expect(wrapper.get("#run-answer").text()).toContain("Checked answer");
  expect(wrapper.get(".citation-button").attributes("aria-label")).toContain("Source");
  expect(wrapper.get("#operator-release-action").text()).toContain("Revoke");
  expect(state.dialog).toBeNull();
});
it("revokes an operator release and hides its answer", async () => {
  const fetch = vi.fn(async () => response(shadow));
  vi.stubGlobal("fetch", fetch);
  const { state, wrapper } = workspace();
  state.run = released;
  await flushPromises();
  await wrapper.get("#operator-release-action").trigger("click");
  expect(wrapper.get("#operator-release-description").text()).toContain("removes the public answer");
  await wrapper.get("#operator-release-reason").setValue("Retract approval");
  await wrapper.get("#operator-release-form").trigger("submit");
  await flushPromises();
  expect(fetch).toHaveBeenCalledWith(
    "/api/runs/run-one/operator-release",
    expect.objectContaining({ body: JSON.stringify({ action: "revoke", reason: "Retract approval" }) }),
  );
  expect(wrapper.get("#run-answer").text()).not.toContain("Checked answer");
  expect(wrapper.find("#operator-approved-label").exists()).toBe(false);
});
it("cancel never publishes and an API rejection preserves the draft boundary", async () => {
  const fetch = vi.fn(async () => response({ error: "A source was deleted. Refresh the run." }, 409));
  vi.stubGlobal("fetch", fetch);
  const { state, actions, wrapper } = workspace();
  actions.requestOperatorRelease("release");
  await flushPromises();
  await wrapper.get("#operator-release-cancel").trigger("click");
  expect(fetch).not.toHaveBeenCalled();
  actions.requestOperatorRelease("release");
  await flushPromises();
  await wrapper.get("#operator-release-reason").setValue("Review complete");
  await wrapper.get("#operator-release-form").trigger("submit");
  await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain("source was deleted");
  expect(state.run?.status).toBe("shadow");
  expect(state.busy.operatorRelease).toBe(false);
  expect(state.dialog).toBe("operator-release");
});
it.each([
  { token: "", credentials: "server", run: shadow },
  { token: "operator", credentials: "browser", run: shadow },
  { token: "operator", credentials: "server", run: { ...shadow, status: "failed" } },
  { token: "operator", credentials: "server", run: { ...shadow, code: "verification_failed" } },
  { token: "operator", credentials: "server", run: { ...released, qualification: "qualified" } },
])(
  "does not offer unsupported overrides for $credentials / $run.status",
  async ({ token, credentials, run }) => {
    const { state, actions, wrapper } = workspace();
    state.token = token;
    state.status = { credentials: credentials as "server" | "browser" };
    state.run = run;
    await flushPromises();
    expect(wrapper.find("#operator-release-action").exists()).toBe(false);
    actions.requestOperatorRelease("release");
    expect(state.dialog).toBeNull();
  },
);
it("ignores late mutation results after a run selection changes", async () => {
  let finish: (value: Response) => void = () => {};
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string) =>
      path.includes("operator-release")
        ? new Promise<Response>((resolve) => {
            finish = resolve;
          })
        : Promise.resolve(response({ id: "run-two", status: "shadow" })),
    ),
  );
  const { state, actions } = workspace();
  actions.requestOperatorRelease("release");
  state.operatorRelease.reason = "Reviewed";
  const pending = actions.changeOperatorRelease();
  await actions.openRun("run-two");
  finish(response(released));
  await pending;
  expect(state.run?.id).toBe("run-two");
  expect(state.busy.operatorRelease).toBe(false);
  expect(state.dialog).toBeNull();
});
it("aborts a pending mutation on disposal and suppresses late responses", async () => {
  let finish: (value: Response) => void = () => {};
  const fetch = vi.fn(
    (_path: string, _options: RequestInit) =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  const { state, actions } = workspace();
  actions.requestOperatorRelease("release");
  state.operatorRelease.reason = "Reviewed";
  const pending = actions.changeOperatorRelease();
  const signal = fetch.mock.calls[0]?.[1].signal;
  actions.dispose();
  expect(signal?.aborted).toBe(true);
  finish(response(released));
  await pending;
  expect(state.run?.status).toBe("shadow");
});
