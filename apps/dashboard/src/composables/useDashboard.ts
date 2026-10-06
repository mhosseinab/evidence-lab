import { reactive } from "vue";
import { ApiError, createApiClient } from "../api/client";
import type { Payload, RuntimeMode, View } from "../types/api";
import {
  dateText,
  documentStatus,
  errorText,
  idOf,
  listOf,
  pendingStates,
  positiveStates,
  readable,
  shortId,
  statusLabels,
  statusOf,
  textValue,
} from "../utils/presentation";
import { useBrowserKeys } from "./useBrowserKeys";

export interface UploadRecord {
  id: number;
  corpusId: string;
  name: string;
  status: string;
  error: boolean;
  jobId?: string;
}
export interface DashboardState {
  token: string;
  mode: RuntimeMode | undefined;
  modeError: string;
  connectionError: string;
  switchingMode: boolean;
  status: Payload | null;
  corpusId: string;
  conversationId: string | null;
  corpora: Payload[];
  documents: Payload[];
  runs: Payload[];
  evaluations: Payload[];
  run: Payload | null;
  runId: string | null;
  runJobId: string | null;
  evidence: Payload[];
  evaluation: Payload | null;
  evaluationId: string | null;
  view: View;
  question: string;
  notice: string;
  toast: string;
  dialog: "source" | "trace" | "connection" | "operator-login" | "new-corpus" | null;
  uploading: boolean;
  uploads: UploadRecord[];
  corpusError: string;
  connected: boolean;
  accessRequired: boolean;
  busy: {
    question: boolean;
    corpus: boolean;
    evaluation: boolean;
    cancelRun: boolean;
    cancelEvaluation: boolean;
    retry: string | null;
    connection: boolean;
  };
  source: {
    title: string;
    pages: Payload[];
    versionId: string;
    loading: boolean;
    error: string;
    meta: string;
    notes: string;
  };
  trace: { title: string; content: string; loading: boolean };
}

export function useDashboard() {
  const credentials = useBrowserKeys();
  const state = reactive<DashboardState>({
    token: "",
    mode: "mock",
    modeError: "",
    connectionError: "",
    switchingMode: false,
    status: null,
    corpusId: "default",
    conversationId: null,
    corpora: [],
    documents: [],
    runs: [],
    evaluations: [],
    run: null,
    runId: null,
    runJobId: null,
    evidence: [],
    evaluation: null,
    evaluationId: null,
    view: "ask",
    question: "",
    notice: "",
    toast: "",
    dialog: null,
    uploading: false,
    uploads: [],
    corpusError: "",
    connected: false,
    accessRequired: false,
    busy: {
      question: false,
      corpus: false,
      evaluation: false,
      cancelRun: false,
      cancelEvaluation: false,
      retry: null,
      connection: false,
    },
    source: { title: "", pages: [], versionId: "", loading: false, error: "", meta: "", notes: "" },
    trace: { title: "", content: "", loading: false },
  });
  let disposed = false;
  let initialized = false;
  let selectionVersion = 0;
  let queryPoll = 0;
  let submittingQuestion = false;
  let evaluationPoll = 0;
  let statusRequest = 0;
  let corporaRequest = 0;
  let documentsRequest = 0;
  let runsRequest = 0;
  let evaluationsRequest = 0;
  let sourceRequest = 0;
  let traceRequest = 0;
  let uploadId = 0;
  let toastTimer: ReturnType<typeof setTimeout> | undefined;
  const uploadPolls = new Map<string, UploadRecord | undefined>();
  const controllers = new Set<AbortController>();
  const delays = new Map<ReturnType<typeof setTimeout>, () => void>();
  const blobUrls = new Set<string>();
  const client = createApiClient(
    () => state.token,
    () => {
      if (!disposed) state.accessRequired = true;
    },
    () =>
      state.status?.credentials === "browser" && state.status?.mode === "live"
        ? credentials.header()
        : undefined,
    () => state.mode,
    () => (state.mode === "live" ? credentials.settingsHeader() : undefined),
  );
  async function api(path: string, options: RequestInit = {}) {
    const controller = new AbortController();
    controllers.add(controller);
    try {
      return await client.request(path, { ...options, signal: controller.signal });
    } finally {
      controllers.delete(controller);
    }
  }
  function sleep(ms: number) {
    return new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        delays.delete(timer);
        resolve();
      }, ms);
      delays.set(timer, resolve);
    });
  }
  function notifyError(error: unknown) {
    if (!disposed) state.notice = errorText(error);
  }
  function toast(message: string) {
    if (disposed) return;
    clearTimeout(toastTimer);
    state.toast = message;
    toastTimer = setTimeout(() => {
      state.toast = "";
    }, 3500);
  }
  function corpusPath(path: string, corpusId = state.corpusId) {
    return `${path}?corpus_id=${encodeURIComponent(corpusId)}`;
  }
  async function refreshCorpora() {
    const request = ++corporaRequest;
    const result = await api("/api/corpora");
    if (disposed || request !== corporaRequest) return;
    state.corpora = listOf(result, "corpora", "items");
    if (!state.corpora.some((corpus) => corpus.id === state.corpusId) && state.corpora[0]?.id)
      await selectCorpus(state.corpora[0].id);
  }
  async function refreshStatus() {
    const corpusId = state.corpusId;
    const request = ++statusRequest;
    try {
      const result = await api(corpusPath("/api/status", corpusId));
      if (disposed || corpusId !== state.corpusId || request !== statusRequest) return;
      credentials.configure(
        result.byok_key_scope || result.config_fingerprint || "",
        result.byok_profiles || [],
      );
      if (!state.mode && (result.mode === "mock" || result.mode === "live")) state.mode = result.mode;
      state.status = result;
      state.connected = true;
      state.accessRequired = false;
    } catch (error) {
      if (disposed || corpusId !== state.corpusId || request !== statusRequest) return;
      state.connected = false;
      state.accessRequired = error instanceof ApiError && [401, 403].includes(error.status);
      throw error;
    }
  }
  function stopWorkspaceRequests() {
    selectionVersion++;
    queryPoll++;
    evaluationPoll++;
    documentsRequest++;
    runsRequest++;
    evaluationsRequest++;
    sourceRequest++;
    traceRequest++;
    uploadPolls.clear();
    for (const controller of controllers) controller.abort();
  }
  function clearWorkspaceResults() {
    state.conversationId = null;
    state.run = null;
    state.runId = null;
    state.runJobId = null;
    state.evidence = [];
    state.evaluation = null;
    state.evaluationId = null;
    state.documents = [];
    state.runs = [];
    state.evaluations = [];
  }
  async function switchMode(mode: RuntimeMode) {
    if (
      disposed ||
      state.token ||
      state.switchingMode ||
      state.uploading ||
      state.busy.question ||
      state.busy.evaluation
    )
      return;
    const previousMode = state.mode;
    const previousStatus = state.status;
    state.switchingMode = true;
    state.modeError = "";
    stopWorkspaceRequests();
    state.mode = mode;
    try {
      await refreshStatus();
      if (disposed) return;
      if (state.status?.mode !== mode)
        throw new Error("The API did not activate the requested mode. Check the workspace configuration.");
      credentials.savePreferences(mode);
      clearWorkspaceResults();
      const results = await Promise.allSettled([refreshDocuments(), refreshRuns(), refreshEvaluations()]);
      const failure = results.find((result) => result.status === "rejected");
      if (failure?.status === "rejected") notifyError(failure.reason);
    } catch (error) {
      if (!disposed) {
        state.mode = previousMode;
        state.status = previousStatus;
        state.connected = !!previousStatus;
        if (previousStatus)
          credentials.configure(
            previousStatus.byok_key_scope || previousStatus.config_fingerprint || "",
            previousStatus.byok_profiles || [],
          );
        state.modeError = errorText(error);
      }
    } finally {
      if (!disposed) state.switchingMode = false;
    }
  }
  async function refreshDocuments() {
    const corpusId = state.corpusId;
    const request = ++documentsRequest;
    const result = await api(corpusPath("/api/documents", corpusId));
    if (disposed || corpusId !== state.corpusId || request !== documentsRequest) return;
    state.documents = listOf(result, "documents", "items");
    for (const doc of state.documents) {
      const jobId = doc.job_id || doc.latest_job_id;
      const jobStatus = (doc.job_status || "").toLowerCase();
      if (jobId && (pendingStates.has(jobStatus) || positiveStates.has(jobStatus))) {
        for (const upload of state.uploads) {
          if (upload.corpusId === corpusId && upload.jobId === jobId) {
            upload.error = false;
            upload.status = statusLabels[jobStatus] || readable(jobStatus);
          }
        }
      }
      if (jobId && pendingStates.has(documentStatus(doc))) void pollDocument(jobId);
    }
  }
  async function refreshRuns() {
    const corpusId = state.corpusId;
    const request = ++runsRequest;
    const result = await api(corpusPath("/api/runs", corpusId));
    if (disposed || corpusId !== state.corpusId || request !== runsRequest) return;
    state.runs = listOf(result, "runs", "items").filter((run) => (run.corpus_id || "default") === corpusId);
  }
  async function refreshEvaluations() {
    const request = ++evaluationsRequest;
    const result = await api("/api/evaluations");
    if (!disposed && request === evaluationsRequest)
      state.evaluations = listOf(result, "evaluations", "jobs", "items");
  }
  async function selectCorpus(corpusId: string) {
    if (disposed || state.switchingMode || corpusId === state.corpusId) return;
    state.corpusId = corpusId;
    state.conversationId = null;
    const selection = ++selectionVersion;
    queryPoll++;
    state.run = null;
    state.runId = null;
    state.runJobId = null;
    state.evidence = [];
    state.documents = [];
    state.runs = [];
    state.status = null;
    state.notice = "";
    state.busy.question = submittingQuestion;
    const results = await Promise.allSettled([refreshStatus(), refreshDocuments(), refreshRuns()]);
    const failure = results.find((result) => result.status === "rejected");
    if (failure?.status === "rejected" && selection === selectionVersion) notifyError(failure.reason);
  }
  async function createCorpus(id: string) {
    const corpusId = id.trim();
    state.corpusError = "";
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(corpusId)) {
      state.corpusError = "Use 1–64 letters, numbers, underscores, or hyphens.";
      return;
    }
    state.busy.corpus = true;
    try {
      await api("/api/corpora", { method: "POST", body: JSON.stringify({ corpus_id: corpusId }) });
      if (disposed) return;
      await refreshCorpora();
      await selectCorpus(corpusId);
      if (!disposed) {
        state.dialog = null;
        toast(`Corpus ${corpusId} is ready.`);
      }
    } catch (error) {
      if (!disposed) state.corpusError = errorText(error);
    } finally {
      state.busy.corpus = false;
    }
  }
  function newConversation() {
    selectionVersion++;
    queryPoll++;
    state.conversationId = null;
    state.run = null;
    state.runId = null;
    state.runJobId = null;
    state.evidence = [];
    state.question = "";
    state.busy.question = submittingQuestion;
    state.notice = "";
  }
  function updateRun(run: Payload) {
    state.run = run;
    if (run.conversation_id) state.conversationId = run.conversation_id;
    state.runJobId = run.job_id || state.runJobId;
    const pack = run.evidence_pack || run.evidence || run.result?.evidence_pack || run.result?.evidence;
    state.evidence = Array.isArray(pack) ? pack : listOf(pack, "items", "evidence");
    state.busy.question = submittingQuestion || pendingStates.has(statusOf(run));
  }
  async function submitQuestion(input: string) {
    const question = input.trim();
    if (!question || state.busy.question || state.switchingMode || disposed) return;
    submittingQuestion = true;
    state.question = question;
    state.busy.question = true;
    state.notice = "";
    const selection = selectionVersion;
    const poll = queryPoll;
    try {
      const result = await api("/api/queries", {
        method: "POST",
        body: JSON.stringify({
          question,
          corpus_id: state.corpusId,
          ...(state.conversationId ? { conversation_id: state.conversationId } : {}),
        }),
      });
      if (disposed || selection !== selectionVersion || poll !== queryPoll) return;
      const id = result.run_id ?? result.id;
      if (!id) throw new Error("The workspace did not return a run identifier.");
      updateRun({ ...result, question, status: result.status || "queued" });
      void openRun(id, result.job_id);
      void refreshRuns().catch(() => {});
    } catch (error) {
      if (!disposed && selection === selectionVersion && poll === queryPoll) {
        notifyError(error);
      }
    } finally {
      submittingQuestion = false;
      if (!disposed) state.busy.question = Boolean(state.run && pendingStates.has(statusOf(state.run)));
    }
  }
  async function openRun(id: string | null, jobId?: string | null) {
    if (!id || disposed) return;
    const token = ++queryPoll;
    state.runId = id;
    state.runJobId = jobId || null;
    if (idOf(state.run) !== id) updateRun({ id, status: "queued", question: state.question });
    let failures = 0;
    while (!disposed && token === queryPoll) {
      try {
        const result = await api(`/api/runs/${encodeURIComponent(id)}`);
        if (disposed || token !== queryPoll) return;
        updateRun(result.run || result);
        failures = 0;
        if (!pendingStates.has(statusOf(state.run))) {
          await Promise.allSettled([refreshRuns(), refreshStatus()]);
          return;
        }
      } catch (error) {
        if (disposed || token !== queryPoll) return;
        if (++failures >= 4 || (error instanceof ApiError && [404, 401, 403].includes(error.status))) {
          updateRun({
            ...state.run,
            status: "status_unavailable",
            message: "The run may still be processing. Refresh recent questions to reconnect.",
          });
          notifyError(error);
          return;
        }
      }
      await sleep(failures ? 2500 : 1200);
    }
  }
  async function cancelRun() {
    if (!state.runJobId) return;
    state.busy.cancelRun = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(state.runJobId)}/cancel`, { method: "POST" });
      toast("Cancellation requested.");
    } catch (error) {
      notifyError(error);
    } finally {
      state.busy.cancelRun = false;
    }
  }
  async function retryIngestion(doc: Payload) {
    const jobId = doc.job_id || doc.latest_job_id;
    if (!jobId) return;
    state.busy.retry = jobId;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST" });
      if (disposed) return;
      doc.job_status = job.status || "queued";
      void pollDocument(job.id || job.job_id || jobId);
      await refreshDocuments();
      toast("Ingestion retry queued.");
    } catch (error) {
      notifyError(error);
    } finally {
      state.busy.retry = null;
    }
  }
  async function uploadFiles(
    files: File[] | FileList | null | undefined,
    documentId: string | null = null,
    corpusId = state.corpusId,
  ) {
    const list = Array.from(files || []);
    if (!list.length || state.switchingMode || disposed) return;
    if (state.uploading) {
      toast("Wait for the current upload to finish.");
      return;
    }
    const limit =
      state.status?.limits?.max_file_bytes ??
      state.status?.limits?.max_upload_bytes ??
      state.status?.ingestion?.max_upload_bytes ??
      Infinity;
    state.uploading = true;
    try {
      for (const file of list) {
        if (disposed) break;
        const item = reactive<UploadRecord>({
          id: ++uploadId,
          corpusId,
          name: file.name,
          status: "Uploading…",
          error: false,
        });
        state.uploads.unshift(item);
        try {
          if (file.size > limit)
            throw new Error(`Exceeds the ${Math.round(limit / 1048576)} MiB upload limit.`);
          if (!/\.(txt|md|markdown|pdf)$/i.test(file.name))
            throw new Error("Use a text, Markdown, or PDF file.");
          const body = new FormData();
          body.append("file", file);
          body.append("corpus_id", corpusId);
          if (documentId) body.append("document_id", documentId);
          const result = await api("/api/documents", { method: "POST", body });
          if (disposed) return;
          item.jobId = result.job_id;
          item.status = result.duplicate ? "Already uploaded" : "Queued for indexing";
          if (result.job_id) void pollDocument(result.job_id, item);
          await refreshDocuments();
        } catch (error) {
          if (!disposed) {
            item.error = true;
            item.status = errorText(error);
          }
        }
      }
    } finally {
      state.uploading = false;
    }
    if (!disposed) void refreshStatus().catch(() => {});
  }
  async function pollDocument(jobId: string, item?: UploadRecord) {
    if (disposed) return;
    if (uploadPolls.has(jobId)) {
      if (item) uploadPolls.set(jobId, item);
      return;
    }
    uploadPolls.set(jobId, item);
    let failures = 0;
    while (!disposed && uploadPolls.has(jobId)) {
      await sleep(1400);
      if (disposed || !uploadPolls.has(jobId)) return;
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        if (disposed || !uploadPolls.has(jobId)) return;
        const status = statusOf(job);
        const row = uploadPolls.get(jobId);
        if (row) row.status = statusLabels[status] || readable(status);
        failures = 0;
        if (!pendingStates.has(status)) {
          uploadPolls.delete(jobId);
          if (row && !positiveStates.has(status)) {
            row.error = true;
            row.status = textValue(job.error, statusLabels[status] || readable(status));
          }
          await refreshDocuments();
          void refreshStatus().catch(() => {});
          return;
        }
      } catch {
        if (disposed) return;
        if (++failures >= 4) {
          const row = uploadPolls.get(jobId);
          if (row) {
            row.error = true;
            row.status = "Status unavailable. Refresh to check again.";
          }
          uploadPolls.delete(jobId);
          return;
        }
        await sleep(2000);
      }
    }
  }
  async function previewSource(versionId: string, title = "Document preview") {
    const request = ++sourceRequest;
    Object.assign(state.source, {
      title,
      versionId,
      pages: [],
      loading: true,
      error: "",
      meta: `Version ${versionId}`,
      notes: "",
    });
    state.dialog = "source";
    try {
      const raw = await api(`/api/source-versions/${encodeURIComponent(versionId)}`);
      if (disposed || request !== sourceRequest) return;
      const source = typeof raw.version === "object" ? raw.version : raw;
      state.source.title = source.name ?? source.filename ?? source.title ?? title;
      state.source.meta = `Version ${versionId} · ${readable(source.state || source.status || "source")} · ${dateText(source.created_at)}`;
      let pages = listOf(source, "pages", "extracted_pages");
      if (!pages.length && source.chunks)
        pages = source.chunks.map((chunk) => ({ page: chunk.page, text: chunk.text }));
      if (!pages.length && typeof source.text === "string") pages = [{ page: 1, text: source.text }];
      state.source.pages = pages;
      state.source.notes = (source.errors ?? [])
        .map((error) => textValue(error, JSON.stringify(error)))
        .join(" · ");
    } catch (error) {
      if (!disposed && request === sourceRequest) state.source.error = errorText(error);
    } finally {
      if (request === sourceRequest) state.source.loading = false;
    }
  }
  function saveBlob(blob: Blob, name: string) {
    if (disposed) return;
    const url = URL.createObjectURL(blob);
    blobUrls.add(url);
    const link = document.createElement("a");
    link.href = url;
    link.download = name.replace(/[\\/]/g, "_");
    link.click();
    const timer = setTimeout(() => {
      URL.revokeObjectURL(url);
      blobUrls.delete(url);
      delays.delete(timer);
    }, 30000);
    delays.set(timer, () => URL.revokeObjectURL(url));
  }
  async function downloadSource() {
    if (!state.source.versionId) return;
    const controller = new AbortController();
    controllers.add(controller);
    try {
      const blob = await client.download(
        `/api/source-versions/${encodeURIComponent(state.source.versionId)}/download`,
        { signal: controller.signal },
      );
      saveBlob(blob, state.source.title || "source");
    } catch (error) {
      if (!disposed) toast(errorText(error));
    } finally {
      controllers.delete(controller);
    }
  }
  async function showTrace() {
    if (!state.runId) return;
    const request = ++traceRequest;
    Object.assign(state.trace, { title: "Run trace", content: "Loading operator trace…", loading: true });
    state.dialog = "trace";
    try {
      const result = await api(`/api/runs/${encodeURIComponent(state.runId)}/trace`);
      if (!disposed && request === traceRequest) state.trace.content = JSON.stringify(result, null, 2);
    } catch (error) {
      if (!disposed && request === traceRequest) state.trace.content = errorText(error);
    } finally {
      if (request === traceRequest) state.trace.loading = false;
    }
  }
  async function copyAnswer(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Answer copied.");
    } catch {
      toast("Copy is unavailable in this browser. Select the answer text to copy it.");
    }
  }
  function showEvaluationTrace() {
    traceRequest++;
    const result = state.evaluation?.result || state.evaluation?.report || state.evaluation;
    Object.assign(state.trace, {
      title: "Evaluation trace",
      content: JSON.stringify(result, null, 2),
      loading: false,
    });
    state.dialog = "trace";
  }
  async function startEvaluation() {
    if (state.busy.evaluation || state.switchingMode || disposed) return;
    state.busy.evaluation = true;
    try {
      const result = await api("/api/evaluations", {
        method: "POST",
        body: JSON.stringify({ dataset: "demo" }),
      });
      if (disposed) return;
      const id = result.job_id ?? result.id ?? result.evaluation_id;
      if (!id) throw new Error("The workspace did not return an evaluation identifier.");
      state.evaluation = { ...result, status: result.status || "queued" };
      void openEvaluation(id);
      void refreshEvaluations().catch(() => {});
    } catch (error) {
      notifyError(error);
      state.busy.evaluation = false;
    }
  }
  async function openEvaluation(id: string | null) {
    if (!id || disposed) return;
    const token = ++evaluationPoll;
    state.evaluationId = id;
    state.evaluation = { id, status: "queued" };
    state.busy.evaluation = true;
    let failures = 0;
    while (!disposed && token === evaluationPoll) {
      try {
        const result = await api(`/api/evaluations/${encodeURIComponent(id)}`);
        if (disposed || token !== evaluationPoll) return;
        state.evaluation = result.evaluation || result;
        failures = 0;
        state.busy.evaluation = pendingStates.has(statusOf(state.evaluation));
        if (!state.busy.evaluation) {
          await Promise.allSettled([refreshEvaluations(), refreshDocuments(), refreshStatus()]);
          return;
        }
      } catch (error) {
        if (disposed || token !== evaluationPoll) return;
        if (++failures >= 4 || (error instanceof ApiError && [404, 401, 403].includes(error.status))) {
          notifyError(error);
          state.busy.evaluation = false;
          return;
        }
      }
      await sleep(failures ? 2500 : 1700);
    }
  }
  async function cancelEvaluation() {
    if (!state.evaluationId) return;
    state.busy.cancelEvaluation = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(state.evaluationId)}/cancel`, { method: "POST" });
      toast("Cancellation requested.");
    } catch (error) {
      notifyError(error);
    } finally {
      state.busy.cancelEvaluation = false;
    }
  }
  function exportEvaluation() {
    if (state.evaluation)
      saveBlob(
        new Blob([`${JSON.stringify(state.evaluation, null, 2)}\n`], { type: "application/json" }),
        `evaluation-${shortId(state.evaluationId)}.json`,
      );
  }
  function sessionChangeBlocked() {
    return (
      disposed ||
      state.busy.connection ||
      state.switchingMode ||
      state.uploading ||
      state.busy.question ||
      state.busy.evaluation
    );
  }
  async function connect(token: string) {
    if (sessionChangeBlocked()) return;
    const value = token.trim();
    if (!value) {
      state.connectionError = "Enter your operator token to sign in.";
      return;
    }
    const previous = {
      token: state.token,
      mode: state.mode,
      status: state.status,
      connected: state.connected,
    };
    state.busy.connection = true;
    state.connectionError = "";
    state.notice = "";
    stopWorkspaceRequests();
    state.token = value;
    state.mode = undefined;
    try {
      await refreshStatus();
      if (disposed) return;
      clearWorkspaceResults();
      const results = await Promise.allSettled([
        refreshCorpora(),
        refreshDocuments(),
        refreshRuns(),
        refreshEvaluations(),
      ]);
      const failure = results.find((result) => result.status === "rejected");
      if (failure?.status === "rejected") notifyError(failure.reason);
      if (!disposed) {
        state.dialog = null;
        toast("Signed in. Using server configuration.");
      }
    } catch (error) {
      if (!disposed) {
        Object.assign(state, previous);
        state.connectionError = errorText(error);
      }
    } finally {
      if (!disposed) state.busy.connection = false;
    }
  }
  async function signOut() {
    if (sessionChangeBlocked()) return;
    state.busy.connection = true;
    state.connectionError = "";
    stopWorkspaceRequests();
    clearWorkspaceResults();
    state.token = "";
    state.mode = "mock";
    state.status = null;
    state.connected = false;
    try {
      await refreshStatus();
      if (disposed) return;
      if (credentials.mode.value && credentials.mode.value !== state.mode)
        await switchMode(credentials.mode.value);
      await Promise.all([refreshCorpora(), refreshDocuments(), refreshRuns(), refreshEvaluations()]);
      if (!disposed) {
        state.dialog = null;
        toast("Signed out. Browser settings restored.");
      }
    } catch (error) {
      notifyError(error);
      if (!disposed) state.dialog = state.accessRequired ? "operator-login" : null;
    } finally {
      if (!disposed) state.busy.connection = false;
    }
  }
  function setView(view: View) {
    state.view = view;
    if (view === "documents") void refreshDocuments().catch(notifyError);
    if (view === "evaluations") void refreshEvaluations().catch(notifyError);
    if (view === "ask") void refreshRuns().catch(notifyError);
  }
  function switchView() {
    const view = window.location.hash.replace(/^#/, "");
    setView(view === "documents" || view === "evaluations" ? view : "ask");
  }
  async function initialize() {
    if (initialized || disposed) return;
    initialized = true;
    window.addEventListener("hashchange", switchView);
    switchView();
    const results = await Promise.allSettled([refreshCorpora(), refreshStatus(), refreshDocuments()]);
    const failure = results.find((result) => result.status === "rejected");
    if (failure?.status === "rejected") notifyError(failure.reason);
    if (credentials.mode.value && credentials.mode.value !== state.mode)
      await switchMode(credentials.mode.value);
  }
  function dispose() {
    disposed = true;
    queryPoll++;
    evaluationPoll++;
    selectionVersion++;
    uploadPolls.clear();
    window.removeEventListener("hashchange", switchView);
    clearTimeout(toastTimer);
    for (const controller of controllers) controller.abort();
    controllers.clear();
    for (const [timer, resolve] of delays) {
      clearTimeout(timer);
      resolve();
    }
    delays.clear();
    for (const url of blobUrls) URL.revokeObjectURL(url);
    blobUrls.clear();
    state.token = "";
    credentials.dispose();
    state.conversationId = null;
  }
  return {
    state,
    credentials,
    actions: {
      refreshStatus,
      switchMode,
      refreshCorpora,
      refreshDocuments,
      refreshRuns,
      refreshEvaluations,
      selectCorpus,
      createCorpus,
      submitQuestion,
      newConversation,
      openRun,
      cancelRun,
      uploadFiles,
      retryIngestion,
      previewSource,
      downloadSource,
      showTrace,
      showEvaluationTrace,
      copyAnswer,
      startEvaluation,
      openEvaluation,
      cancelEvaluation,
      exportEvaluation,
      connect,
      signOut,
      notifyError,
      toast,
      setView,
      initialize,
      dispose,
    },
  };
}
export type Dashboard = ReturnType<typeof useDashboard>;
