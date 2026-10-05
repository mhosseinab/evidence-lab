<script setup lang="ts">
import { computed } from "vue";
import { useDashboardContext } from "../composables/dashboardContext";
import { pendingStates, readable, statusOf } from "../utils/presentation";

const { state } = useDashboardContext();
const graph = computed(() => state.status?.orchestration);
const labels: Record<string, string> = {
  retrieve: "Find evidence",
  generate: "Draft answer",
  verify: "Verify claims",
  repair: "Repair answer",
  release: "Release or abstain",
  structural: "Check answer structure",
  terminal: "Finish run",
};
const runningNodes: Record<string, string> = {
  retrieving: "retrieve",
  generating: "generate",
  verifying: "verify",
  repairing: "repair",
};
const steps = computed(() =>
  (graph.value?.nodes ?? []).map((node) => {
    const events = state.run?.graph_steps?.filter((step) => step.node === node) ?? [];
    const last = events.at(-1);
    const status = statusOf(state.run);
    const currentNode = state.run?.stage ?? runningNodes[status];
    const current = !last && pendingStates.has(status) && currentNode === node;
    return {
      node,
      label: labels[node] ?? readable(node),
      status: current ? "running" : (last?.status ?? "waiting"),
      seconds: events.reduce((total, step) => total + step.elapsed_seconds, 0),
      attempts: events.length,
    };
  }),
);
</script>

<template>
  <section v-if="graph" class="panel graph-panel" aria-labelledby="workflow-heading">
    <div class="section-heading">
      <h2 id="workflow-heading">LangGraph workflow</h2>
      <span class="graph-tracing">LangSmith {{ graph.tracing.enabled ? "active" : "off" }}</span>
    </div>
    <ol class="graph-steps" aria-label="Execution stages">
      <li
        v-for="step in steps"
        :key="step.node"
        :data-status="step.status"
        :aria-current="step.status === 'running' ? 'step' : undefined"
      >
        <span class="graph-dot" aria-hidden="true">{{
          step.status === "completed" ? "✓" : step.status === "failed" ? "!" : "·"
        }}</span>
        <span>{{ step.label }}</span>
        <small
          >{{
            step.status === "waiting"
              ? "Not run"
              : step.status === "running"
                ? "Running"
                : `${step.status === "failed" ? "Failed" : "Completed"} · ${step.seconds.toFixed(2)}s`
          }}{{ step.attempts > 1 ? ` · ${step.attempts} executions` : "" }}</small
        >
      </li>
    </ol>
    <p v-if="graph.tools.length" class="graph-note">Tools: {{ graph.tools.map(readable).join(" · ") }}</p>
    <p v-if="graph.tracing.enabled" class="graph-note">
      LangSmith records execution metadata only; question, answer and document text stay private.
    </p>
  </section>
</template>

<style scoped>
.graph-panel {
  padding: 22px;
  margin-top: 24px;
}
.graph-panel h2 {
  font-size: 16px;
}
.graph-tracing,
.graph-note {
  font-size: 12px;
  color: var(--muted, #6d716c);
}
.graph-steps {
  display: grid;
  gap: 10px;
  margin: 18px 0;
  padding: 0;
  list-style: none;
}
.graph-steps li {
  display: grid;
  grid-template-columns: 20px 1fr auto;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.graph-steps small {
  font-size: 11px;
  color: var(--muted, #6d716c);
}
.graph-dot {
  text-align: center;
  border: 1px solid currentColor;
  border-radius: 50%;
}
.graph-steps [data-status="completed"] .graph-dot {
  color: #34714c;
}
.graph-steps [data-status="failed"] .graph-dot {
  color: #9d3b36;
}
.graph-steps [data-status="running"] .graph-dot {
  color: #966717;
}
.graph-note {
  margin: 10px 0 0;
}
@media (max-width: 480px) {
  .graph-steps li {
    grid-template-columns: 20px 1fr;
  }
  .graph-steps small {
    grid-column: 2;
  }
}
</style>
