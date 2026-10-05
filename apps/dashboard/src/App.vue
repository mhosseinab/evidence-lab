<script setup lang="ts">
import { computed, onMounted, onUnmounted } from "vue";
import WorkspaceDialogs from "./components/WorkspaceDialogs.vue";
import WorkspaceHeader from "./components/WorkspaceHeader.vue";
import WorkspaceSidebar from "./components/WorkspaceSidebar.vue";
import { provideDashboard } from "./composables/dashboardContext";
import { useDashboard } from "./composables/useDashboard";
import AskView from "./views/AskView.vue";
import DocumentsView from "./views/DocumentsView.vue";
import EvaluationsView from "./views/EvaluationsView.vue";

const dashboard = useDashboard();
provideDashboard(dashboard);
const { state, actions } = dashboard;
const fixture = computed(() =>
  ["mock", "fixture"].includes(state.status?.mode || state.status?.runtime?.mode || ""),
);
const shadow = computed(() => state.status?.policy_state === "shadow");
onMounted(() => {
  void actions.initialize();
});
onUnmounted(() => {
  actions.dispose();
});
</script>

<template>
  <a class="skip-link" href="#main">Skip to content</a>
  <div class="app-shell">
    <WorkspaceSidebar />
    <div class="main-shell">
      <WorkspaceHeader />
      <main id="main" tabindex="-1">
        <div v-if="state.notice" id="global-notice" class="notice" role="alert">{{ state.notice }}</div>
        <div
          v-if="state.status?.embedding_space_matches === false"
          id="space-mismatch-banner"
          class="notice"
          role="status"
        >
          This corpus uses a different embedding profile. Create a new corpus and re-upload sources, or
          restore its configuration.
        </div>
        <div v-if="fixture || shadow" id="mode-banner" class="mode-banner">
          <span class="banner-icon" aria-hidden="true">◈</span>
          <div>
            <strong id="mode-banner-title">{{ fixture ? "Fixture mode" : "Shadow policy" }}</strong
            ><span id="mode-banner-text">{{
              fixture
                ? "Deterministic demo responses. No model calls. Model quality has not been evaluated."
                : "Answers are being evaluated. This policy is not qualified for release."
            }}</span>
          </div>
        </div>
        <AskView v-show="state.view === 'ask'" /><DocumentsView v-show="state.view === 'documents'" />
        <EvaluationsView v-show="state.view === 'evaluations'" />
        <footer class="main-footer">
          <span>Evidence Lab</span><span>Local storage · Configured inference · Visible checks</span>
        </footer>
      </main>
    </div>
  </div>
  <WorkspaceDialogs />
  <div v-if="state.toast" id="toast" class="toast" role="status">{{ state.toast }}</div>
</template>
