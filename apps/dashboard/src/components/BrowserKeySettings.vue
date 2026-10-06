<script setup lang="ts">
import { computed, ref } from "vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { KeyGroup, RuntimeMode } from "../types/api";
import LiveEndpointSettings from "./LiveEndpointSettings.vue";

const { state, credentials, actions } = useDashboardContext();
const fields: { group: KeyGroup; label: string }[] = [
  { group: "llm", label: "LLM provider key" },
  { group: "cloudflare", label: "Cloudflare key" },
];
const drafts = ref<Record<KeyGroup, string>>({ llm: "", cloudflare: "" });
const message = ref("");
const connected = computed(() => !!state.status);
const switchingDisabled = computed(
  () =>
    !connected.value ||
    state.switchingMode ||
    state.uploading ||
    state.busy.question ||
    state.busy.evaluation,
);
function selectMode(event: Event) {
  const mode = (event.target as HTMLSelectElement).value as RuntimeMode;
  void actions.switchMode(mode);
}
function save(group: KeyGroup) {
  const value = drafts.value[group];
  drafts.value[group] = "";
  try {
    credentials.save(group, value);
    message.value = "Key saved in this browser.";
  } catch (error) {
    message.value = error instanceof Error ? error.message : "The key could not be saved.";
  }
}
function remove(group: KeyGroup) {
  drafts.value[group] = "";
  try {
    credentials.remove(group);
    message.value = "Key removed from this browser.";
  } catch (error) {
    message.value = error instanceof Error ? error.message : "The key could not be removed.";
  }
}
</script>

<template>
  <section id="browser-key-form">
    <h3>Mode and provider keys</h3>
    <label for="workspace-mode">Mode</label>
    <select
      id="workspace-mode"
      :value="state.mode || state.status?.mode || 'mock'"
      :disabled="switchingDisabled"
      @change="selectMode"
    >
      <option value="mock">Fixture mode · no model calls</option>
      <option value="live">Live mode · your provider keys</option>
    </select>
    <p class="field-help">
      Fixture mode makes no model calls. Live mode uses configured endpoints and budgets. Changing mode makes
      no model calls.
    </p>
    <p v-if="state.switchingMode" role="status">Checking mode…</p>
    <p v-if="state.modeError" role="alert">{{ state.modeError }}</p>
    <p v-if="state.uploading || state.busy.question || state.busy.evaluation" class="field-help">
      Wait for active work to finish before changing mode.
    </p>
    <p class="field-help">
      * Both keys are required for live model calls. Keys are saved only in this browser. Sent to the API for
      model calls and never saved on the server.
    </p>
    <form v-for="field in fields" :key="field.group" @submit.prevent="save(field.group)">
      <label :for="`${field.group}-key`">{{ field.label }} <span aria-hidden="true">*</span></label>
      <input
        :id="`${field.group}-key`"
        v-model="drafts[field.group]"
        type="password"
        autocomplete="off"
        required
        maxlength="4096"
        placeholder="Paste your key"
        :disabled="!connected || state.switchingMode"
      >
      <p class="field-help">
        {{ credentials.saved[field.group] ? "Key saved in this browser" : "No browser key saved" }}
      </p>
      <div class="dialog-footer">
        <button
          :id="`remove-${field.group}-key`"
          type="button"
          class="button secondary"
          :disabled="!credentials.saved[field.group] || state.switchingMode"
          @click="remove(field.group)"
        >
          Remove key
        </button>
        <button type="submit" class="button primary" :disabled="!connected || state.switchingMode">
          Save in browser
        </button>
      </div>
    </form>
    <LiveEndpointSettings />
    <p v-if="message" role="status" class="field-help">{{ message }}</p>
  </section>
</template>
