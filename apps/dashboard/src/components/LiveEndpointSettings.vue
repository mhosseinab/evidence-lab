<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { LiveSettings } from "../types/api";

const { state, credentials } = useDashboardContext();
type PriceField =
  | "embedding_input_usd_per_million"
  | "chat_input_usd_per_million"
  | "chat_output_usd_per_million";
type LimitField =
  | "embedding_dimensions"
  | "embedding_max_input_tokens"
  | "chat_max_input_tokens"
  | "chat_max_output_tokens";
type NumericField = PriceField | LimitField | "budget_usd";
type SetupDraft = Omit<LiveSettings, NumericField | "output_limit_parameter"> &
  Record<NumericField, number | ""> & { output_limit_parameter: LiveSettings["output_limit_parameter"] | "" };
const limits: LimitField[] = [
  "embedding_dimensions",
  "embedding_max_input_tokens",
  "chat_max_input_tokens",
  "chat_max_output_tokens",
];
function emptyDraft(): SetupDraft {
  return {
    embedding_source: "custom",
    embedding_endpoint: "",
    embedding_model: "",
    embedding_dimensions: "",
    embedding_max_input_tokens: "",
    chat_endpoint: "",
    chat_model: "",
    chat_max_input_tokens: "",
    chat_max_output_tokens: "",
    structured_output: "text_json",
    output_limit_parameter: "",
    cloudflare_account_id: "",
    budget_usd: 0,
    embedding_input_usd_per_million: "",
    chat_input_usd_per_million: "",
    chat_output_usd_per_million: "",
  };
}
const draft = ref<SetupDraft>(emptyDraft());
const message = ref("");
watch(
  credentials.settings,
  (settings) => {
    draft.value = settings ? { ...emptyDraft(), ...settings } : emptyDraft();
  },
  { immediate: true },
);
const allPrices: { field: PriceField; label: string }[] = [
  { field: "embedding_input_usd_per_million", label: "Embedding input price (USD / million tokens)" },
  { field: "chat_input_usd_per_million", label: "Chat input price (USD / million tokens)" },
  { field: "chat_output_usd_per_million", label: "Chat output price (USD / million tokens)" },
];
const workspaceEmbeddings = computed(() => draft.value.embedding_source === "workspace");
const prices = computed(() =>
  allPrices.filter(({ field }) => !workspaceEmbeddings.value || field !== "embedding_input_usd_per_million"),
);
function removeSetup() {
  if (state.mode !== "mock" || state.switchingMode) return;
  try {
    credentials.removeSetup();
    message.value = "Live setup removed from this browser. Your provider keys are still saved.";
  } catch (error) {
    message.value = error instanceof Error ? error.message : "Live setup could not be removed.";
  }
}
function save() {
  try {
    if (state.mode === "live") throw new Error("Select fixture mode before changing live endpoint setup.");
    if (prices.value.some(({ field }) => draft.value[field] === ""))
      throw new Error(
        workspaceEmbeddings.value
          ? "Enter both chat prices below. Use 0 for a free endpoint."
          : "Enter all three provider prices below. Use 0 for a free endpoint.",
      );
    if (
      limits
        .filter((field) => !workspaceEmbeddings.value || !field.startsWith("embedding_"))
        .some((field) => draft.value[field] === "")
    )
      throw new Error(
        workspaceEmbeddings.value
          ? "Enter chat input and output token limits from your model documentation."
          : "Enter embedding dimensions and all token limits from your provider's model documentation.",
      );
    if (!draft.value.output_limit_parameter)
      throw new Error("Select the output limit parameter supported by your chat model.");
    const settings: LiveSettings = {
      ...draft.value,
      embedding_dimensions: Number(draft.value.embedding_dimensions),
      embedding_max_input_tokens: Number(draft.value.embedding_max_input_tokens),
      chat_max_input_tokens: Number(draft.value.chat_max_input_tokens),
      chat_max_output_tokens: Number(draft.value.chat_max_output_tokens),
      budget_usd: draft.value.budget_usd === "" ? Number.NaN : draft.value.budget_usd,
      output_limit_parameter: draft.value.output_limit_parameter,
      embedding_input_usd_per_million: Number(draft.value.embedding_input_usd_per_million),
      chat_input_usd_per_million: Number(draft.value.chat_input_usd_per_million),
      chat_output_usd_per_million: Number(draft.value.chat_output_usd_per_million),
    };
    if (workspaceEmbeddings.value) {
      delete settings.embedding_endpoint;
      delete settings.embedding_model;
      delete settings.embedding_dimensions;
      delete settings.embedding_max_input_tokens;
      delete settings.embedding_input_usd_per_million;
    }
    credentials.savePreferences(credentials.mode.value, settings);
    message.value = "Live setup saved in this browser. Select live mode to validate it.";
  } catch (error) {
    message.value = error instanceof Error ? error.message : "Live setup could not be saved.";
  }
}
</script>

<template>
  <form id="live-endpoint-form" @submit.prevent="save">
    <h3>Live endpoint setup</h3>
    <p class="field-help">
      Use your provider's OpenAI-compatible chat endpoint and choose your embedding setup. Setup is saved in
      this browser. Your Cloudflare key is used for the Clef verifier.
    </p>
    <p class="field-help">
      * Required for live mode. Model limits and prices depend on your selected models.
    </p>
    <fieldset :disabled="!state.status || state.switchingMode || state.mode === 'live'">
      <label for="embedding-source">Embeddings <span aria-hidden="true">*</span></label>
      <select id="embedding-source" v-model="draft.embedding_source" required>
        <option value="custom">Custom embedding endpoint</option>
        <option value="workspace">Workspace embeddings · reuse pgvector vectors</option>
      </select>
      <p v-if="workspaceEmbeddings" class="field-help">
        Reuse the workspace's pgvector vectors and matching query embedding setup. Chat and Clef stay live.
        Workspace fixture embeddings remain deterministic demo data, not a measured local model.
      </p>
      <template v-if="!workspaceEmbeddings">
        <label for="embedding-endpoint">Embedding endpoint URL <span aria-hidden="true">*</span></label>
        <input
          id="embedding-endpoint"
          v-model="draft.embedding_endpoint"
          type="url"
          required
          autocomplete="off"
          placeholder="Complete embeddings endpoint URL"
        >
        <label for="embedding-model">Embedding model <span aria-hidden="true">*</span></label>
        <input
          id="embedding-model"
          v-model="draft.embedding_model"
          required
          placeholder="Provider model identifier"
        >
      </template>
      <label for="chat-endpoint">Chat endpoint URL <span aria-hidden="true">*</span></label>
      <input
        id="chat-endpoint"
        v-model="draft.chat_endpoint"
        type="url"
        required
        autocomplete="off"
        placeholder="Complete chat completions endpoint URL"
      >
      <label for="chat-model">Chat model <span aria-hidden="true">*</span></label>
      <input id="chat-model" v-model="draft.chat_model" required placeholder="Provider model identifier">
      <label for="cloudflare-account">Cloudflare account ID <span aria-hidden="true">*</span></label>
      <input
        id="cloudflare-account"
        v-model="draft.cloudflare_account_id"
        required
        maxlength="32"
        autocomplete="off"
        placeholder="32-character account ID"
      >
      <label for="live-budget">Live budget (USD) <span aria-hidden="true">*</span></label>
      <input id="live-budget" v-model.number="draft.budget_usd" type="number" min="0" step="any" required>
      <p class="field-help">
        A budget of 0 prevents paid calls. Confirm token limits and prices with your provider before running
        live work.
      </p>
      <details :open="!credentials.settings.value">
        <summary>Model limits, output format and pricing</summary>
        <template v-if="!workspaceEmbeddings">
          <label for="embedding-dimensions">Embedding dimensions <span aria-hidden="true">*</span></label>
          <input
            max="16000"
            id="embedding-dimensions"
            v-model.number="draft.embedding_dimensions"
            type="number"
            min="1"
            step="1"
            required
            placeholder="From your model documentation"
          >
          <label for="embedding-input-limit"
            >Embedding input token limit <span aria-hidden="true">*</span></label
          >
          <input
            id="embedding-input-limit"
            v-model.number="draft.embedding_max_input_tokens"
            type="number"
            min="1"
            step="1"
            required
            placeholder="From your model documentation"
          >
        </template>
        <label for="chat-input-limit">Chat input token limit <span aria-hidden="true">*</span></label>
        <input
          id="chat-input-limit"
          v-model.number="draft.chat_max_input_tokens"
          type="number"
          min="1"
          step="1"
          required
          placeholder="From your model documentation"
        >
        <label for="chat-output-limit">Chat output token limit <span aria-hidden="true">*</span></label>
        <input
          id="chat-output-limit"
          v-model.number="draft.chat_max_output_tokens"
          type="number"
          min="1"
          step="1"
          required
          placeholder="From your model documentation"
        >
        <label for="structured-output">Chat JSON output format <span aria-hidden="true">*</span></label>
        <select id="structured-output" v-model="draft.structured_output" required>
          <option value="text_json">JSON in text · no response_format parameter</option>
          <option value="json_object">JSON object</option>
          <option value="json_schema">JSON schema</option>
        </select>
        <p class="field-help">
          JSON in text sends no response_format parameter. Choose a structured format only if your model
          supports it.
        </p>
        <label for="output-limit-parameter"
          >Chat output limit parameter <span aria-hidden="true">*</span></label
        >
        <select id="output-limit-parameter" v-model="draft.output_limit_parameter" required>
          <option disabled value="">Select the parameter your model supports</option>
          <option value="max_tokens">max_tokens</option>
          <option value="max_completion_tokens">max_completion_tokens</option>
        </select>
        <template v-for="price in prices" :key="price.field">
          <label :for="price.field">{{ price.label }} <span aria-hidden="true">*</span></label>
          <input
            :id="price.field"
            v-model.number="draft[price.field]"
            type="number"
            min="0"
            step="any"
            required
            placeholder="Enter price, or 0 for free"
          >
        </template>
        <p class="field-help">
          Clef verifier: 65,536-token context; $0.24 per million input tokens. These defaults are included in
          the budget.
          <a
            href="https://developers.cloudflare.com/workers-ai/models/clef/"
            target="_blank"
            rel="noopener noreferrer"
            >Cloudflare model documentation</a
          >
        </p>
      </details>
      <button type="submit" class="button secondary">Save live setup in browser</button>
    </fieldset>
    <button
      v-if="credentials.settings.value"
      id="remove-live-setup"
      type="button"
      class="button secondary"
      :disabled="state.mode !== 'mock' || state.switchingMode"
      @click="removeSetup"
    >
      Remove live setup
    </button>
    <p v-if="state.mode === 'live'" class="field-help">Select fixture mode to edit this setup.</p>
    <p v-if="message" role="status" class="field-help">{{ message }}</p>
  </form>
</template>
