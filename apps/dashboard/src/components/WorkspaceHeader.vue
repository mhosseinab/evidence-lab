<script setup lang="ts">
import { computed } from "vue";
import { useDashboardContext } from "../composables/dashboardContext";
import { readable } from "../utils/presentation";

const { state } = useDashboardContext();
const titles = { ask: "Ask your documents", documents: "Documents", evaluations: "Evaluations" };
const mode = computed(() => state.status?.mode || state.status?.runtime?.mode || "unknown");
const label = computed(() =>
  !state.connected
    ? "Disconnected"
    : ["mock", "fixture"].includes(mode.value)
      ? "◈  Fixture mode"
      : mode.value === "live"
        ? "Live endpoints"
        : readable(mode.value),
);
</script>

<template>
  <header class="topbar">
    <div class="breadcrumb">
      <span id="breadcrumb-corpus">{{ state.corpusId }}</span><span aria-hidden="true">/</span
      ><strong id="breadcrumb-title">{{ titles[state.view] }}</strong>
    </div>
    <div class="topbar-status">
      <button
        id="runtime-badge"
        type="button"
        class="runtime-badge"
        :class="{ live: mode === 'live' }"
        aria-label="Open workspace connection"
        @click="state.dialog = 'connection'"
      >
        {{ label }}
      </button><span class="avatar" role="img" aria-label="Local operator">L</span>
    </div>
  </header>
</template>
