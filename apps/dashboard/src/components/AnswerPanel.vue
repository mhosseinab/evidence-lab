<script setup lang="ts">
import { computed } from "vue";
import { useDashboardContext } from "../composables/dashboardContext";
import { answerState, operatorReleaseAction, releasedAnswer } from "../utils/answers";
import { pendingStates, progressLabels, shortId, statusOf } from "../utils/presentation";
import StatusPill from "./StatusPill.vue";

const emit = defineEmits<{ citation: [index: number] }>();
const { state, actions } = useDashboardContext();
const run = computed(() => state.run ?? {});
const status = computed(() => statusOf(state.run));
const operatorAction = computed(() =>
  state.token && !state.accessRequired && state.status?.credentials === "server"
    ? operatorReleaseAction(state.run)
    : null,
);
const active = computed(() => pendingStates.has(status.value));
const progress = computed(
  () =>
    progressLabels[String(run.value.stage || run.value.progress?.stage || status.value)] ??
    progressLabels.running ?? ["Working on your question", "Checking the response."],
);
const answer = computed(() => releasedAnswer(state.run));
const hasAnswer = computed(() => answer.value.blocks.length > 0 || Boolean(answer.value.text));
const outcome = computed(() => answerState(run.value));
const blocks = computed(() =>
  answer.value.blocks.map((block, blockIndex) => ({
    key: block.id ?? `block-${blockIndex}`,
    text: block.text ?? "",
    citations: (block.citation_ids ?? []).flatMap((id) => {
      const index = state.evidence.findIndex((item) => item.id === id);
      return index >= 0 ? [{ id, index, title: state.evidence[index]?.title ?? "Source document" }] : [];
    }),
  })),
);
const metadata = computed(() => {
  const values = [`Run ${shortId(state.runId)}`];
  const current = run.value;
  if (current.qualification === "verified") values.push("Checks passed · Policy unqualified");
  const elapsed =
    current.elapsed_seconds ??
    current.duration_seconds ??
    current.timings?.worker_total_seconds ??
    current.metrics?.latency_seconds;
  if (typeof elapsed === "number" && Number.isFinite(elapsed)) values.push(`${elapsed.toFixed(1)} s`);
  if (
    current.repaired ||
    (current.repair_count ?? 0) > 0 ||
    current.timings?.repair_generation_seconds !== undefined
  )
    values.push("One repair, rechecked");
  if (
    current.qualification === "fixture_only" ||
    current.mode === "mock" ||
    (!current.mode && !current.qualification && state.status?.mode === "mock")
  )
    values.push("Deterministic fixture");
  return values;
});

async function copyAnswer() {
  const text = answer.value.blocks.length
    ? answer.value.blocks.map((block) => block.text ?? "").join("\n\n")
    : (answer.value.text ?? "");
  if (text) await actions.copyAnswer(text);
}
</script>

<template>
  <section id="run-panel" class="panel result-panel" aria-labelledby="run-title" aria-live="polite">
    <div class="panel-header">
      <div class="section-title">
        <span class="section-icon" aria-hidden="true">✧</span>
        <h2 id="run-title">Answer</h2>
      </div>
      <StatusPill id="run-status" :status="status" />
    </div>
    <div v-if="active" id="run-progress" class="run-progress">
      <span class="spinner" aria-hidden="true"></span>
      <div>
        <strong id="run-progress-title">{{ progress[0] }}</strong>
        <p id="run-progress-text">{{ progress[1] }}</p>
      </div>
      <button
        v-if="state.runJobId"
        id="run-cancel"
        class="text-button"
        type="button"
        :disabled="state.busy.cancelRun"
        @click="actions.cancelRun()"
      >
        Cancel
      </button>
    </div>
    <div id="run-answer" class="answer-body">
      <template v-if="hasAnswer">
        <div
          v-if="run.qualification === 'operator_approved'"
          class="trace-warning"
          id="operator-approved-label"
        >
          <strong>Operator-approved release</strong>
          <p>
            This answer was published by an operator after passing its checks. The evaluation policy remains
            unqualified.
          </p>
        </div>
        <div v-for="block in blocks" :key="block.key" class="answer-block">
          <span class="answer-text">{{ block.text }}</span>
          <span v-if="block.citations.length" class="citations">
            <button
              v-for="citation in block.citations"
              :key="citation.id"
              type="button"
              class="citation-button"
              :aria-label="`View source ${citation.index + 1}: ${citation.title}`"
              @click="emit('citation', citation.index)"
            >
              {{ citation.index + 1 }}
            </button>
          </span>
        </div>
        <div v-if="!blocks.length" class="answer-block answer-text">{{ answer.text }}</div>
      </template>
      <div v-else-if="!active" class="answer-state" :class="{ error: outcome.error }">
        <span class="answer-state-symbol">{{ outcome.symbol }}</span>
        <div>
          <h3>{{ outcome.title }}</h3>
          <p>{{ outcome.message }}</p>
        </div>
      </div>
    </div>
    <div id="run-meta" class="run-meta">
      <template v-if="!active"><span v-for="value in metadata" :key="value">{{ value }}</span></template>
    </div>
    <div class="run-actions">
      <button
        v-if="operatorAction"
        id="operator-release-action"
        type="button"
        class="text-button"
        :disabled="state.busy.operatorRelease"
        @click="actions.requestOperatorRelease(operatorAction)"
      >
        {{ operatorAction === "release" ? "Release with operator approval" : "Revoke operator release" }}
      </button>
      <button v-if="hasAnswer" id="copy-answer" class="text-button" type="button" @click="copyAnswer">
        Copy answer
      </button>
      <button
        v-if="state.runId"
        id="open-trace"
        class="text-button"
        type="button"
        @click="actions.showTrace()"
      >
        View operator trace<span aria-hidden="true">↗</span>
      </button>
    </div>
  </section>
</template>
