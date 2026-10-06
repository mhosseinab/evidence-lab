<script setup lang="ts">
import { useDashboardContext } from "../composables/dashboardContext";

const { state, actions } = useDashboardContext();
</script>

<template>
  <aside class="sidebar" aria-label="Workspace navigation">
    <a class="brand" href="#ask" aria-label="Evidence Lab home"
      ><span class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></span
      ><span>evidence<span class="brand-light">lab</span><small>DOCUMENT WORKSPACE</small></span></a
    >
    <div class="workspace-label">
      <span class="workspace-dot" aria-hidden="true"></span
      ><label class="visually-hidden" for="corpus-select">Selected corpus</label
      ><select
        id="corpus-select"
        :value="state.corpusId"
        @change="actions.selectCorpus(($event.target as HTMLSelectElement).value)"
      >
        <option
          v-for="corpus in state.corpora"
          :key="corpus.corpus_id || corpus.id"
          :value="corpus.corpus_id || corpus.id"
        >
          {{ corpus.name || corpus.corpus_id || corpus.id }}
        </option>
      </select><button
        id="new-corpus-open"
        type="button"
        class="new-corpus-button"
        aria-label="New corpus"
        title="New corpus"
        @click="state.dialog = 'new-corpus'"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
      </button>
    </div>
    <p class="nav-caption">WORKSPACE</p>
    <nav class="nav-list">
      <a
        class="nav-link"
        :class="{ active: state.view === 'ask' }"
        :aria-current="state.view === 'ask' ? 'page' : undefined"
        href="#ask"
        data-view="ask"
        ><svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M20 11.5a8.5 8.5 0 0 1-8.5 8.5 9 9 0 0 1-3.8-.9L3 21l1.8-4.7A8.5 8.5 0 1 1 20 11.5Z" />
          <path d="M8 10h7M8 14h4" />
        </svg><span>Ask your documents</span></a
      >
      <a
        class="nav-link"
        :class="{ active: state.view === 'documents' }"
        :aria-current="state.view === 'documents' ? 'page' : undefined"
        href="#documents"
        data-view="documents"
        ><svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M13 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10Z" />
          <path d="M13 3v7h7M8 14h8M8 17h5" />
        </svg><span>Documents</span
        ><span id="nav-document-count" class="nav-count">{{ state.documents.length }}</span></a
      >
      <a
        class="nav-link"
        :class="{ active: state.view === 'evaluations' }"
        :aria-current="state.view === 'evaluations' ? 'page' : undefined"
        href="#evaluations"
        data-view="evaluations"
        ><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 3v17h17M8 15v-4M13 15V7M18 15v-7" /></svg
        ><span>Evaluations</span></a
      >
    </nav>
    <div class="sidebar-bottom">
      <div class="sidebar-note">
        <span class="small-spark" aria-hidden="true">✧</span>
        <p>From source to answer.<br><strong>With the evidence attached.</strong></p>
      </div>
      <button
        id="connection-open"
        type="button"
        class="connection-button"
        @click="state.dialog = state.accessRequired ? 'operator-login' : 'connection'"
      >
        <span id="connection-dot" class="status-dot" :class="state.connected ? 'online' : 'offline'"></span
        ><span id="connection-label">{{
          state.connected
            ? "Workspace connected"
            : state.accessRequired
              ? "Access required"
              : "Connection unavailable"
        }}</span><svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 8v4m0 4h.01" />
          <circle cx="12" cy="12" r="9" />
        </svg>
      </button><span class="sidebar-version">RAG proof of concept</span>
    </div>
  </aside>
</template>
