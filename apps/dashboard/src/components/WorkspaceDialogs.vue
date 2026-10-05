<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useDashboardContext } from "../composables/dashboardContext";
import { readable } from "../utils/presentation";

const { state, actions } = useDashboardContext();
const sourceDialog = ref<HTMLDialogElement>();
const traceDialog = ref<HTMLDialogElement>();
const connectionDialog = ref<HTMLDialogElement>();
const corpusDialog = ref<HTMLDialogElement>();
const token = ref("");
const corpusId = ref("");
const dialogs = {
  source: sourceDialog,
  trace: traceDialog,
  connection: connectionDialog,
  "new-corpus": corpusDialog,
};
watch(
  () => state.dialog,
  (active) => {
    for (const [name, dialog] of Object.entries(dialogs)) {
      if (name === active && dialog.value && !dialog.value.open) dialog.value.showModal();
      else if (name !== active && dialog.value?.open) dialog.value.close();
    }
    if (active !== "connection") token.value = "";
    if (active !== "new-corpus") corpusId.value = "";
    else state.corpusError = "";
  },
  { flush: "post" },
);
function close() {
  state.dialog = null;
}
function backdrop(event: MouseEvent) {
  if (event.target === event.currentTarget) close();
}
async function connect() {
  const value = token.value;
  token.value = "";
  await actions.connect(value);
}
async function createCorpus() {
  await actions.createCorpus(corpusId.value);
}
const connectionRows = computed(() => {
  const status = state.status;
  const rows: [string, string | number][] = [
    ["Connection", state.connected ? "Connected" : "Unavailable"],
    ["Execution mode", readable(status?.mode || status?.runtime?.mode || "unknown")],
    ["Policy", readable(status?.policy_state || status?.policy?.state || "Not reported")],
  ];
  for (const [role, profile] of Object.entries(status?.profiles || {}))
    rows.push([
      readable(role),
      typeof profile === "string" ? profile : profile.model || profile.name || "Configured",
    ]);
  const budgets = status?.budgets;
  if (budgets && Number.isFinite(Number(budgets.live_charged_cost)))
    rows.push(["Recorded live cost", `$${Number(budgets.live_charged_cost).toFixed(4)}`]);
  if (budgets?.active_calls !== undefined) rows.push(["Active calls", budgets.active_calls]);
  return rows;
});
</script>

<template>
  <dialog
    id="source-dialog"
    ref="sourceDialog"
    class="dialog source-dialog"
    aria-labelledby="source-dialog-title"
    @click="backdrop"
    @keydown.esc.prevent="close"
    @cancel.prevent="close"
    @close="state.dialog === 'source' && close()"
  >
    <div class="dialog-heading">
      <div>
        <p class="eyebrow">IMMUTABLE SOURCE VERSION</p>
        <h2 id="source-dialog-title">{{ state.source.title }}</h2>
      </div>
      <button type="button" class="icon-button" aria-label="Close document preview" @click="close">×</button>
    </div>
    <div id="source-dialog-meta" class="dialog-meta">{{ state.source.meta }}</div>
    <div id="source-dialog-body" class="source-body">
      <p v-if="state.source.loading" class="source-notice">Loading the source version…</p>
      <p v-else-if="state.source.error" class="source-notice">{{ state.source.error }}</p>
      <template v-else
        ><p v-if="state.source.notes" class="source-notice">{{ state.source.notes }}</p>
        <p v-if="!state.source.pages.length" class="source-notice">
          No extracted text is available yet. Check the document’s ingestion status, or download the original.
        </p>
        <section v-for="(page, index) in state.source.pages" :key="index" class="source-page">
          <h3>Page {{ page.page ?? page.number ?? index + 1 }}</h3>
          <div class="source-page-text">
            {{ page.text ?? page.content ?? "No text was extracted from this page." }}
          </div>
        </section></template
      >
    </div>
    <div class="dialog-footer">
      <a
        id="source-download"
        class="button secondary"
        :href="`/api/source-versions/${encodeURIComponent(state.source.versionId || 'unselected')}/download`"
        @click.prevent="actions.downloadSource()"
        >Download original <span aria-hidden="true">↓</span></a
      ><button type="button" class="button secondary" @click="close">Close</button>
    </div>
  </dialog>
  <dialog
    id="trace-dialog"
    ref="traceDialog"
    class="dialog trace-dialog"
    aria-labelledby="trace-dialog-title"
    @click="backdrop"
    @keydown.esc.prevent="close"
    @cancel.prevent="close"
    @close="state.dialog === 'trace' && close()"
  >
    <div class="dialog-heading">
      <div>
        <p class="eyebrow">OPERATOR VIEW</p>
        <h2 id="trace-dialog-title">{{ state.trace.title }}</h2>
      </div>
      <button type="button" class="icon-button" aria-label="Close operator trace" @click="close">×</button>
    </div>
    <div class="trace-warning">
      <strong>Unverified drafts may appear below.</strong>
      <p>Trace content is for inspection. It is not a released answer or a claim of model quality.</p>
    </div>
    <pre
      id="trace-content"
      class="trace-content"
      tabindex="0"
    >{{ state.trace.loading ? 'Loading trace…' : state.trace.content }}</pre>
    <div class="dialog-footer">
      <button type="button" class="button secondary" @click="close">Close</button>
    </div>
  </dialog>
  <dialog
    id="connection-dialog"
    ref="connectionDialog"
    class="dialog connection-dialog"
    aria-labelledby="connection-title"
    @click="backdrop"
    @keydown.esc.prevent="close"
    @cancel.prevent="close"
    @close="state.dialog === 'connection' && close()"
  >
    <div class="dialog-heading">
      <div>
        <p class="eyebrow">LOCAL OPERATOR</p>
        <h2 id="connection-title">Workspace connection</h2>
      </div>
      <button type="button" class="icon-button" aria-label="Close connection settings" @click="close">
        ×
      </button>
    </div>
    <div id="connection-details" class="connection-details">
      <div v-for="[ label, value ] in connectionRows" :key="label" class="connection-row">
        <span>{{ label }}</span><strong>{{ value }}</strong>
      </div>
    </div>
    <form id="token-form" @submit.prevent="connect">
      <label for="operator-token">Operator token <span class="optional">optional</span></label>
      <p class="field-help">
        If access protection is enabled, enter the token from your private configuration.
      </p>
      <input
        id="operator-token"
        v-model="token"
        type="password"
        autocomplete="off"
        placeholder="Enter operator token"
      >
      <div class="dialog-footer">
        <span class="field-help">Used for this page session.</span
        ><button type="submit" class="button primary" :disabled="state.busy.connection">
          {{ state.busy.connection ? "Connecting…" : "Connect" }}
        </button>
      </div>
    </form>
  </dialog>
  <dialog
    id="new-corpus-dialog"
    ref="corpusDialog"
    class="dialog connection-dialog"
    aria-labelledby="corpus-title"
    @click="backdrop"
    @keydown.esc.prevent="close"
    @cancel.prevent="close"
    @close="state.dialog === 'new-corpus' && close()"
  >
    <div class="dialog-heading">
      <div>
        <p class="eyebrow">DOCUMENT COLLECTION</p>
        <h2 id="corpus-title">New corpus</h2>
      </div>
      <button type="button" class="icon-button" aria-label="Close new corpus" @click="close">×</button>
    </div>
    <form id="new-corpus-form" class="corpus-form" @submit.prevent="createCorpus">
      <label for="new-corpus-id">Corpus ID</label>
      <p class="field-help">
        Use 1–64 letters, numbers, underscores, or hyphens. Documents in this collection are searched
        separately.
      </p>
      <input
        id="new-corpus-id"
        v-model="corpusId"
        name="corpus_id"
        type="text"
        required
        minlength="1"
        maxlength="64"
        pattern="[A-Za-z0-9_\-]{1,64}"
        autocomplete="off"
        placeholder="project-notes"
      >
      <p v-if="state.corpusError" id="new-corpus-error" class="field-error" role="alert">
        {{ state.corpusError }}
      </p>
      <div class="dialog-footer">
        <button type="button" class="button secondary" @click="close">Cancel</button
        ><button id="new-corpus-submit" type="submit" class="button primary" :disabled="state.busy.corpus">
          {{ state.busy.corpus ? "Creating…" : "Create corpus" }}
        </button>
      </div>
    </form>
  </dialog>
</template>
