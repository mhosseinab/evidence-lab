<script setup lang="ts">
import { computed, ref } from "vue";
import AnswerPanel from "../components/AnswerPanel.vue";
import EvidenceList from "../components/EvidenceList.vue";
import StatusPill from "../components/StatusPill.vue";
import WorkflowPanel from "../components/WorkflowPanel.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { Payload } from "../types/api";
import { dateText, documentStatus, idOf, pendingStates, statusOf } from "../utils/presentation";

const { state, actions } = useDashboardContext();
const questionInput = ref<HTMLTextAreaElement | null>(null);
const evidenceList = ref<InstanceType<typeof EvidenceList> | null>(null);
const active = computed(
  () => state.busy.question || Boolean(state.run && pendingStates.has(statusOf(state.run))),
);
const readyCount = computed(
  () =>
    state.documents.filter((doc) => documentStatus(doc) === "ready" || Boolean(doc.active_version_id)).length,
);
const scope = computed(() =>
  readyCount.value
    ? `Searches ${readyCount.value} ready document${readyCount.value === 1 ? "" : "s"}`
    : "Searches your indexed documents",
);
const suggestions = [
  { label: "Refund window", question: "Within how many days of purchase may customers request a refund?" },
  { label: "Support hours", question: "What are the support hours?" },
];

function suggest(question: string) {
  state.question = question;
  questionInput.value?.focus();
}
function submit() {
  if (active.value) return;
  if (!state.question.trim()) questionInput.value?.focus();
  else void actions.submitQuestion(state.question);
}
function handleShortcut(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    submit();
  }
}
function openRun(run: Payload) {
  state.question = run.question ?? "";
  void actions.openRun(idOf(run), run.job_id);
}
function highlightEvidence(index: number) {
  void evidenceList.value?.highlight(index);
}
async function refreshRuns() {
  try {
    await actions.refreshRuns();
    actions.toast("Questions refreshed.");
  } catch (error) {
    actions.notifyError(error);
  }
}
</script>

<template>
  <section id="view-ask" class="view" aria-labelledby="ask-heading">
    <div class="page-heading">
      <div>
        <p class="eyebrow">YOUR KNOWLEDGE, WITH CONTEXT</p>
        <h1 id="ask-heading">Ask your documents.</h1>
        <p class="page-description">Get an answer you can trace back to its source.</p>
      </div>
      <a class="button secondary" href="#documents"
        ><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>Add documents</a
      >
    </div>
    <div class="ask-layout">
      <div class="ask-main">
        <div v-if="state.status?.orchestration?.memory.enabled" class="section-heading">
          <span class="composer-hint"
            >{{ state.conversationId ? "Conversation continues" : "New conversation" }}
            · Up to {{ state.status.orchestration.memory.max_turns }} remembered turns</span
          >
          <button
            id="new-conversation"
            class="text-button"
            type="button"
            :disabled="active"
            @click="actions.newConversation"
          >
            New conversation
          </button>
        </div>
        <form id="question-form" class="composer panel" @submit.prevent="submit">
          <label for="question">What would you like to know?</label>
          <textarea
            id="question"
            ref="questionInput"
            v-model="state.question"
            name="question"
            rows="4"
            maxlength="4000"
            placeholder="Ask a question about the documents in your workspace…"
            required
            @keydown="handleShortcut"
          ></textarea>
          <div class="composer-footer">
            <span class="composer-hint"
              ><span class="tiny-doc" aria-hidden="true">▤</span
              ><span id="question-scope">{{ scope }}</span></span
            ><button id="ask-submit" type="submit" class="button primary" :disabled="active">
              Ask question<span aria-hidden="true">↗</span>
            </button>
          </div>
        </form>
        <div id="question-suggestions" class="suggestions">
          <span>Try a question</span
          ><button
            v-for="suggestion in suggestions"
            :key="suggestion.question"
            class="suggestion"
            type="button"
            :data-question="suggestion.question"
            @click="suggest(suggestion.question)"
          >
            {{ suggestion.label }} <span aria-hidden="true">↗</span>
          </button>
        </div>
        <WorkflowPanel />
        <AnswerPanel v-if="state.run" @citation="highlightEvidence" />
        <div v-else id="ask-welcome" class="panel welcome-panel">
          <span class="welcome-icon" aria-hidden="true"
            ><svg viewBox="0 0 24 24">
              <title>Workspace icon</title>
              <path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4Z" />
            </svg></span
          >
          <h2>A little more certainty.</h2>
          <p>Ask a question, inspect the evidence, and see whether the answer passed its checks.</p>
          <div class="workflow">
            <span><b>01</b>Find evidence</span><span aria-hidden="true">→</span
            ><span><b>02</b>Check the answer</span><span aria-hidden="true">→</span
            ><span><b>03</b>Show sources</span>
          </div>
        </div>
        <section class="recent-section" aria-labelledby="recent-heading">
          <div class="section-heading">
            <h2 id="recent-heading">Recent questions</h2>
            <button id="runs-refresh" class="text-button" type="button" @click="refreshRuns">
              Refresh <span aria-hidden="true">↻</span>
            </button>
          </div>
          <div id="recent-runs" class="panel list-panel">
            <p v-if="!state.runs.length" class="empty-inline">Your questions will appear here.</p>
            <button
              v-for="(run, index) in state.runs.slice(0, 12)"
              :key="idOf(run) ?? index"
              type="button"
              class="list-row"
              :class="{ selected: idOf(run) === state.runId }"
              @click="openRun(run)"
            >
              <span class="list-row-main"
                ><span class="list-row-title">{{ run.question || "Question" }}</span
                ><span class="list-row-meta">{{ dateText(run.created_at) }}</span></span
              >
              <span class="list-row-side"
                ><StatusPill :status="statusOf(run)" /><span class="list-row-arrow">↗</span></span
              >
            </button>
          </div>
        </section>
      </div>
      <EvidenceList ref="evidenceList" />
    </div>
  </section>
</template>
