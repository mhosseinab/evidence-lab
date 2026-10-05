<script setup lang="ts">
import { useDashboardContext } from "../composables/dashboardContext";
import type { Payload } from "../types/api";
import { dateText, documentStatus, documentVersion, shortId } from "../utils/presentation";
import StatusPill from "./StatusPill.vue";

const emit = defineEmits<{ update: [document: Payload] }>();
const { state, actions } = useDashboardContext();

function nameOf(doc: Payload) {
  return doc.name ?? doc.filename ?? doc.title ?? "Untitled document";
}
function fileType(doc: Payload) {
  const extension = nameOf(doc).split(".").pop()?.toUpperCase() ?? "DOC";
  return extension.length < 6 ? extension : "DOC";
}
function description(doc: Payload) {
  const details: string[] = [];
  const chunks = doc.chunk_count ?? doc.chunks_count ?? doc.active_version?.chunk_count;
  if (chunks !== undefined) details.push(`${chunks} chunk${chunks === 1 ? "" : "s"}`);
  const count = doc.version_count ?? doc.latest_version_no;
  if (count) details.push(`${count} version${count === 1 ? "" : "s"}`);
  if (doc.active_version_id && doc.active_version_id !== documentVersion(doc))
    details.push("Previous version searchable");
  return details.length ? details.join(" · ") : `Version ${shortId(documentVersion(doc))}`;
}
function recoverable(doc: Payload) {
  const status = documentStatus(doc);
  return (
    Boolean(doc.job_id || doc.latest_job_id) &&
    (doc.retryable ??
      (["failed", "cancelled"].includes(doc.job_status ?? status) &&
        !["needs_ocr", "needs_review"].includes(status)))
  );
}
function preview(doc: Payload) {
  const version = documentVersion(doc);
  if (version) void actions.previewSource(version, nameOf(doc));
}
</script>

<template>
  <div class="panel table-wrap">
    <table v-if="state.documents.length" class="documents-table">
      <thead>
        <tr>
          <th scope="col">Document</th>
          <th scope="col">Status</th>
          <th scope="col">Added</th>
          <th scope="col"><span class="visually-hidden">Actions</span></th>
        </tr>
      </thead>
      <tbody id="documents-table-body">
        <tr v-for="(doc, index) in state.documents" :key="doc.document_id ?? doc.id ?? index">
          <td>
            <div class="document-cell">
              <span class="file-icon">{{ fileType(doc) }}</span>
              <div>
                <div class="file-name">{{ nameOf(doc) }}</div>
                <div class="file-subtitle">{{ description(doc) }}</div>
              </div>
            </div>
          </td>
          <td><StatusPill :status="documentStatus(doc)" /></td>
          <td>{{ dateText(doc.created_at ?? doc.uploaded_at) }}</td>
          <td>
            <div class="document-actions">
              <button v-if="documentVersion(doc)" class="text-button" type="button" @click="preview(doc)">
                Preview ↗
              </button>
              <button
                v-if="doc.document_id || doc.id"
                class="text-button"
                type="button"
                :disabled="state.uploading"
                :aria-label="`Upload a new version of ${nameOf(doc)}`"
                @click="emit('update', doc)"
              >
                Update
              </button>
              <button
                v-if="recoverable(doc)"
                class="text-button"
                type="button"
                :disabled="state.busy.retry === (doc.job_id || doc.latest_job_id)"
                :aria-label="`Retry ingestion of ${nameOf(doc)}`"
                @click="actions.retryIngestion(doc)"
              >
                Retry
              </button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-else id="documents-empty" class="table-empty">
      <span aria-hidden="true">▤</span>
      <h3>Your workspace starts here.</h3>
      <p>Upload a document to make it available for questions.</p>
    </div>
  </div>
</template>
