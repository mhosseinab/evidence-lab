<script setup lang="ts">
import { computed } from "vue";
import MetricTable from "../components/MetricTable.vue";
import StatusPill from "../components/StatusPill.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { Payload } from "../types/api";
import {
  dateText,
  formatMetric,
  idOf,
  pendingStates,
  positiveStates,
  readable,
  shortId,
  statusOf,
  textValue,
} from "../utils/presentation";

const { state, actions } = useDashboardContext();
const run = computed(() => state.evaluation || {});
const status = computed(() => statusOf(run.value));
const active = computed(() => !!state.evaluation && pendingStates.has(status.value));
const result = computed(() => run.value.result || run.value.report || run.value);
const metrics = computed(() => result.value.metrics || result.value.summary || result.value);
const qualification = computed(() =>
  typeof result.value.qualification === "object" ? result.value.qualification : null,
);
const introduction = computed(() => {
  const report = result.value;
  const note = report.note || qualification.value?.reason || report.disclaimer;
  return typeof note === "string"
    ? note
    : report.fixture_only || report.runtime_mode === "mock" || report.mode === "mock"
      ? "Synthetic demo results describe the fixture workflow. They do not qualify any model or release policy."
      : "Results apply to this evaluation and its recorded review coverage. Inspect the qualification requirements before using a release policy.";
});
const failure = computed(() =>
  textValue(
    run.value.error,
    textValue(
      run.value.message,
      "The evaluation did not finish. Its partial results are available in the export when recorded.",
    ),
  ),
);
const comparison = computed(() => {
  const variants = metrics.value.variants || result.value.variants || metrics.value.variant_metrics;
  const entries: [string, Payload][] = !variants
    ? []
    : Array.isArray(variants)
      ? variants.map((value, index) => [
          value.variant || value.name || String.fromCharCode(65 + index),
          value,
        ])
      : Object.entries(variants);
  const excluded = [
    "name",
    "variant",
    "rows",
    "cases",
    "records",
    "results",
    "runs",
    "statuses",
    "zero_error_upper95_if_independent",
  ];
  const preferred = [
    "attempted",
    "live_completion",
    "released",
    "released_count",
    "substantive_released",
    "correct_complete",
    "correct_and_complete",
    "unsupported_released",
    "unsupported_release_rate",
    "false_acceptance_rate",
    "supported_retention",
    "abstained",
    "missing_evidence_handling",
    "conflict_handling",
    "final_response_audit",
    "reviewed_substantive_releases",
    "technical_failures",
    "latency_seconds",
    "cost_usd",
  ];
  const labels: Record<string, string> = {
    live_completion: "Completed runs",
    released_count: "Answers released",
    correct_and_complete: "Correct and complete",
    unsupported_release_rate: "Unsupported answer rate",
    missing_evidence_handling: "Missing-evidence handling",
    conflict_handling: "Conflict handling",
    final_response_audit: "Final response audit",
    reviewed_substantive_releases: "Substantive answers reviewed",
  };
  const keys = [...new Set(entries.flatMap(([, value]) => Object.keys(value.metrics || value)))].filter(
    (key) => !excluded.includes(key),
  );
  keys.sort(
    (a, b) =>
      (preferred.includes(a) ? preferred.indexOf(a) : 100) -
        (preferred.includes(b) ? preferred.indexOf(b) : 100) || a.localeCompare(b),
  );
  return {
    headers: ["Metric", ...entries.map(([key]) => readable(key))],
    rows: keys
      .slice(0, 18)
      .map((key) => [
        labels[key] || readable(key),
        ...entries.map(([, value]) => formatMetric((value.metrics || value)[key])),
      ]),
  };
});
const controlledRows = computed(() => {
  const controlled = metrics.value.controlled;
  if (!controlled || Array.isArray(controlled)) return [];
  return [
    ["false_acceptance", "Unsupported claims accepted"],
    ["supported_retention", "Supported claims retained"],
    ["operational_completion", "Completed claim checks"],
  ].flatMap(([key, label]) =>
    key && label && controlled[key] ? [[label, formatMetric(controlled[key])]] : [],
  );
});
const summaryRows = computed(() => {
  const report = result.value;
  const summary = metrics.value;
  const keys = [
    "total_questions",
    "questions",
    "attempted",
    "completed",
    "technical_failures",
    "cost_usd",
    "estimated_cost_usd",
    "duration_seconds",
    "mode",
    "qualified",
    "qualification_status",
  ];
  const rows = keys.flatMap((key) => {
    const value = summary[key] ?? report[key];
    return value !== undefined && value !== null && typeof value !== "object"
      ? [[readable(key), formatMetric(value)]]
      : [];
  });
  if (summary.required_evidence_coverage_at_8)
    rows.push(["Required evidence coverage", formatMetric(summary.required_evidence_coverage_at_8)]);
  if (report.runtime_mode) rows.push(["Execution mode", readable(report.runtime_mode)]);
  if (report.ledger_summary) {
    const ledger = report.ledger_summary;
    rows.push(["Recorded attempts", formatMetric(ledger.attempts)]);
    if (typeof ledger.known_actual_usd === "number" && Number.isFinite(ledger.known_actual_usd))
      rows.push(["Known recorded cost", `$${ledger.known_actual_usd.toFixed(4)}`]);
    if (ledger.unknown_usage_or_cost_attempts)
      rows.push(["Attempts with unknown usage or cost", formatMetric(ledger.unknown_usage_or_cost_attempts)]);
  }
  return rows;
});
function refreshEvaluations() {
  void actions.refreshEvaluations().catch(actions.notifyError);
}
</script>

<template>
  <section id="view-evaluations" class="view" aria-labelledby="evaluations-heading">
    <div class="page-heading">
      <div>
        <p class="eyebrow">MAKE THE DIFFERENCE MEASURABLE</p>
        <h1 id="evaluations-heading">Evaluations</h1>
        <p class="page-description">Compare the same questions across four answer policies.</p>
      </div>
      <button
        id="start-evaluation"
        class="button primary"
        type="button"
        :disabled="!state.corpusId || state.busy.deleteCorpus || active || state.busy.evaluation"
        @click="actions.startEvaluation"
      >
        <span aria-hidden="true">▷</span>Run demo evaluation
      </button>
    </div>
    <div class="evaluation-explainer panel">
      <div>
        <span class="pill neutral">PAIRED COMPARISON</span>
        <h2>One draft. Four ways to handle it.</h2>
        <p>
          Each variant begins with the same draft and evidence. Compare how checking and repair affect the
          final response.
        </p>
      </div>
      <div class="variant-grid">
        <div>
          <span class="variant-letter">A</span><strong>Ungated</strong>
          <p>The initial draft</p>
        </div>
        <div>
          <span class="variant-letter">B</span><strong>Structure</strong>
          <p>Format and citations</p>
        </div>
        <div>
          <span class="variant-letter">C</span><strong>Semantic gate</strong>
          <p>Support and consistency</p>
        </div>
        <div>
          <span class="variant-letter">D</span><strong>Gate + repair</strong>
          <p>One repair, fully rechecked</p>
        </div>
      </div>
    </div>
    <div class="evaluation-note">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 11v6m0-10h.01" />
      </svg>
      <p>
        The bundled demo uses synthetic fixtures. It checks the workflow; it does not establish model
        accuracy. A quality claim requires a frozen, human-reviewed held-out evaluation.
      </p>
    </div>
    <div v-if="state.evaluation" id="evaluation-current" class="panel evaluation-current" aria-live="polite">
      <div class="panel-header">
        <h2 id="evaluation-title">{{ active ? "Evaluation in progress" : "Evaluation results" }}</h2>
        <StatusPill id="evaluation-status" :status="status" />
      </div>
      <div id="evaluation-body">
        <div v-if="active" class="run-progress">
          <span class="spinner" aria-hidden="true"></span>
          <div>
            <strong>{{
              status === "queued" ? "Waiting for the evaluation worker" : "Comparing the answer policies"
            }}</strong>
            <p>The run and its results remain available in the history.</p>
          </div>
        </div>
        <template v-else
          ><div v-if="!positiveStates.has(status)" class="evaluation-results-intro"><p>{{ failure }}</p></div>
          <p class="evaluation-results-intro">{{ introduction }}</p>
          <div v-if="qualification" class="evaluation-qualification">
            <span class="pill" :class="qualification.qualified === true ? 'positive' : 'warning'">{{
              qualification.qualified === true ? "Quality gates passed" : "Quality not qualified"
            }}</span><span>{{
              qualification.human_reviewed ? "Human review recorded" : "Human review incomplete"
            }}</span>
          </div>
          <MetricTable
            v-if="comparison.headers.length > 1"
            :headers="comparison.headers"
            :rows="comparison.rows"
          /><template v-if="controlledRows.length"
            ><h3 class="metric-section-title">Controlled claim challenge</h3>
            <p class="evaluation-results-intro">
              Supplied claims, repair disabled. These rates are separate from natural answer errors.
            </p>
            <MetricTable :headers="['Measure', 'Count and interval']" :rows="controlledRows" /></template
          ><MetricTable v-if="summaryRows.length" :headers="['Run summary', 'Value']" :rows="summaryRows" />
          <div class="metrics-details">
            <button class="text-button" type="button" @click="actions.showEvaluationTrace">
              View evaluation operator trace ↗
            </button>
          </div></template
        >
      </div>
      <div class="run-actions">
        <button
          v-if="!active"
          id="evaluation-export"
          class="text-button"
          type="button"
          @click="actions.exportEvaluation"
        >
          Export results as JSON <span aria-hidden="true">↓</span>
        </button><button
          v-if="active"
          id="evaluation-cancel"
          class="text-button"
          type="button"
          :disabled="state.busy.cancelEvaluation"
          @click="actions.cancelEvaluation"
        >
          Cancel evaluation
        </button>
      </div>
    </div>
    <section class="recent-section" aria-labelledby="evaluation-history-heading">
      <div class="section-heading">
        <h2 id="evaluation-history-heading">Evaluation history</h2>
        <button id="evaluations-refresh" class="text-button" type="button" @click="refreshEvaluations">
          Refresh <span aria-hidden="true">↻</span>
        </button>
      </div>
      <div id="evaluation-history" class="panel list-panel">
        <p v-if="!state.evaluations.length" class="empty-inline">No evaluation runs yet.</p>
        <button
          v-for="item in state.evaluations"
          :key="idOf(item) || undefined"
          type="button"
          class="list-row"
          :class="{ selected: idOf(item) === state.evaluationId }"
          @click="actions.openEvaluation(idOf(item))"
        >
          <span class="list-row-main"
            ><span class="list-row-title">{{
              item.name || `${readable(item.dataset || item.payload?.dataset || "demo")} evaluation`
            }}</span><span class="list-row-meta"
              >{{ dateText(item.created_at) }}
              · {{ shortId(idOf(item)) }}</span
            ></span
          ><span class="list-row-side"
            ><StatusPill :status="statusOf(item)" /><span class="list-row-arrow">↗</span></span
          >
        </button>
      </div>
    </section>
  </section>
</template>
