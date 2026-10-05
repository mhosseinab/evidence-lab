<script setup lang="ts">
import { computed, ref } from "vue";
import DocumentTable from "../components/DocumentTable.vue";
import { useDashboardContext } from "../composables/dashboardContext";
import type { Payload } from "../types/api";
import { documentStatus, pendingStates, positiveStates, statusLabels } from "../utils/presentation";

const { state, actions } = useDashboardContext();
const fileInput = ref<HTMLInputElement | null>(null);
const versionInput = ref<HTMLInputElement | null>(null);
const dragOver = ref(false);
const revision = ref<{ documentId: string; corpusId: string } | null>(null);
const accepted = ".txt,.md,.markdown,.pdf,text/plain,text/markdown,application/pdf";
const counts = computed(() => ({
  ready: state.documents.filter((doc) => documentStatus(doc) === "ready" || Boolean(doc.active_version_id))
    .length,
  processing: state.documents.filter((doc) => pendingStates.has(documentStatus(doc))).length,
  attention: state.documents.filter(
    (doc) => !positiveStates.has(documentStatus(doc)) && !pendingStates.has(documentStatus(doc)),
  ).length,
}));
const limit = computed(() => {
  const bytes =
    state.status?.limits?.max_file_bytes ??
    state.status?.limits?.max_upload_bytes ??
    state.status?.ingestion?.max_upload_bytes ??
    20 * 1048576;
  return `Up to ${Math.round(bytes / 1048576)} MiB per file · scanned PDFs need OCR before upload`;
});
const uploads = computed(() => state.uploads.filter((upload) => upload.corpusId === state.corpusId));

function updateDocument(doc: Payload) {
  const documentId = doc.document_id ?? doc.id;
  if (!documentId || state.uploading) return;
  // Capture the selected document's corpus before the native picker opens.
  revision.value = { documentId, corpusId: doc.corpus_id ?? state.corpusId };
  versionInput.value?.click();
}
async function chooseFiles(event: Event, update = false) {
  const input = event.target;
  if (!(input instanceof HTMLInputElement)) return;
  const target = update ? revision.value : null;
  try {
    if (input.files)
      await actions.uploadFiles(input.files, target?.documentId, target?.corpusId ?? state.corpusId);
  } finally {
    input.value = "";
    if (update) revision.value = null;
  }
}
function dropFiles(event: DragEvent) {
  dragOver.value = false;
  if (event.dataTransfer?.files) void actions.uploadFiles(event.dataTransfer.files);
}
async function refreshDocuments() {
  try {
    await actions.refreshDocuments();
    actions.toast("Documents refreshed.");
  } catch (error) {
    actions.notifyError(error);
  }
}
</script>

<template>
  <section id="view-documents" class="view" aria-labelledby="documents-heading">
    <div class="page-heading">
      <div>
        <p class="eyebrow">A HOME FOR YOUR SOURCES</p>
        <h1 id="documents-heading">Documents</h1>
        <p class="page-description">Add your documents and follow their journey into the search index.</p>
      </div>
      <button id="documents-refresh" class="button secondary" type="button" @click="refreshDocuments">
        <span aria-hidden="true">↻</span>Refresh
      </button>
    </div>
    <div class="stat-grid">
      <div class="stat-card">
        <span>Documents</span><strong id="stat-documents">{{ state.documents.length }}</strong
        ><small>In this workspace</small>
      </div>
      <div class="stat-card">
        <span>Ready to search</span><strong id="stat-ready">{{ counts.ready }}</strong
        ><small>Completed ingestion</small>
      </div>
      <div class="stat-card">
        <span>Processing</span><strong id="stat-processing">{{ counts.processing }}</strong
        ><small>Queued or in progress</small>
      </div>
      <div class="stat-card">
        <span>Needs attention</span><strong id="stat-attention">{{ counts.attention }}</strong
        ><small>Review or retry required</small>
      </div>
    </div>
    <section
      id="upload-zone"
      class="upload-zone"
      aria-label="Upload documents"
      :class="{ uploading: state.uploading, 'drag-over': dragOver }"
      @dragenter.prevent="dragOver = true"
      @dragover.prevent="dragOver = true"
      @dragleave.prevent="dragOver = false"
      @drop.prevent="dropFiles"
    >
      <input
        id="file-input"
        ref="fileInput"
        type="file"
        multiple
        :accept="accepted"
        class="visually-hidden"
        :disabled="state.uploading"
        @change="chooseFiles($event)"
      >
      <span class="upload-icon" aria-hidden="true"
        ><svg viewBox="0 0 24 24">
          <title>Workspace icon</title>
          <path d="M12 16V4m-4 4 4-4 4 4M4 16v4h16v-4" />
        </svg></span
      >
      <h2>Drop your documents here</h2>
      <p>Text, Markdown, or text-readable PDF</p>
      <button
        id="choose-files"
        type="button"
        class="button secondary"
        :disabled="state.uploading"
        @click="fileInput?.click()"
      >
        Choose files
      </button><small id="upload-limit">{{ limit }}</small>
    </section>
    <input
      id="version-input"
      ref="versionInput"
      type="file"
      :accept="accepted"
      class="visually-hidden"
      :disabled="state.uploading"
      @change="chooseFiles($event, true)"
      @cancel="revision = null"
    >
    <div id="upload-status" class="upload-status" aria-live="polite">
      <div
        v-for="upload in uploads"
        :key="upload.id"
        class="upload-item"
        :class="{ error: upload.error }"
        :data-corpus-id="upload.corpusId"
        :data-job-id="upload.jobId"
      >
        <span>{{ upload.name }}</span><span>{{ statusLabels[upload.status] || upload.status }}</span>
      </div>
    </div>
    <section class="documents-section" aria-labelledby="source-list-heading">
      <div class="section-heading">
        <h2 id="source-list-heading">Your sources</h2>
        <span id="document-list-count" class="count-label"
          >{{ state.documents.length }}
          document{{ state.documents.length === 1 ? "" : "s" }}</span
        >
      </div>
      <DocumentTable @update="updateDocument" />
    </section>
  </section>
</template>
