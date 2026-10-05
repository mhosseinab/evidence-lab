"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    token: "",
    status: null,
    corpusId: "default",
    corpora: [],
    selectionVersion: 0,
    statusRequest: 0,
    documentsRequest: 0,
    runsRequest: 0,
    documents: [],
    runs: [],
    evaluations: [],
    run: null,
    runId: null,
    runJobId: null,
    evidence: [],
    evaluation: null,
    evaluationId: null,
    queryPoll: 0,
    evaluationPoll: 0,
    uploadPolls: new Map(),
    uploading: false,
    updateDocumentId: null,
    updateCorpusId: null,
    toastTimer: null,
    view: "ask",
  };
  const pendingStates = new Set([
    "queued",
    "pending",
    "running",
    "processing",
    "ingesting",
    "extracting",
    "embedding",
    "retrieving",
    "generating",
    "verifying",
    "repairing",
    "retrying",
    "claimed",
  ]);
  const positiveStates = new Set([
    "ready",
    "answered",
    "accepted",
    "released",
    "complete",
    "completed",
    "succeeded",
    "success",
    "done",
  ]);
  const warningStates = new Set([
    "abstained",
    "abstention",
    "unsupported",
    "not_supported",
    "needs_review",
    "needs_ocr",
    "policy_not_ready",
    "shadow",
    "cancelled",
    "canceled",
  ]);
  const statusLabels = {
    queued: "Queued",
    processing: "Processing",
    running: "Running",
    ready: "Ready",
    answered: "Checks passed",
    accepted: "Checks passed",
    released: "Released",
    abstained: "Abstained",
    abstention: "Abstained",
    verification_unavailable: "Check unavailable",
    failed: "Failed",
    error: "Error",
    needs_review: "Needs review",
    needs_ocr: "Needs OCR",
    policy_not_ready: "Policy not ready",
    shadow: "Shadow run",
    cancelled: "Cancelled",
    canceled: "Cancelled",
    timed_out: "Timed out",
    timeout: "Timed out",
    budget_exhausted: "Budget exhausted",
    complete: "Complete",
    completed: "Complete",
    done: "Complete",
    succeeded: "Complete",
    success: "Complete",
  };
  const progressLabels = {
    queued: [
      "Waiting to start",
      "The worker will pick up this question shortly.",
    ],
    retrieving: [
      "Finding supporting evidence",
      "Searching the indexed documents.",
    ],
    generating: [
      "Drafting an answer",
      "The draft stays private while it is checked.",
    ],
    verifying: [
      "Checking the answer",
      "Checking each answer block and the complete response.",
    ],
    repairing: [
      "Rechecking a revised answer",
      "One repair is allowed, followed by a complete check.",
    ],
    running: [
      "Working on your question",
      "Finding evidence, drafting, and checking the response.",
    ],
    processing: [
      "Working on your question",
      "Finding evidence, drafting, and checking the response.",
    ],
  };

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }
  function clear(el) {
    el.replaceChildren();
  }
  function show(el, visible = true) {
    el.classList.toggle("hidden", !visible);
  }
  function readable(value) {
    return String(value ?? "")
      .replaceAll("_", " ")
      .replace(/\b\w/g, (x) => x.toUpperCase());
  }
  function statusOf(value) {
    return String(value?.status ?? value?.state ?? "queued").toLowerCase();
  }
  function idOf(value) {
    return value?.id ?? value?.run_id ?? value?.job_id ?? null;
  }
  function listOf(value, ...keys) {
    if (Array.isArray(value)) return value;
    for (const key of keys) if (Array.isArray(value?.[key])) return value[key];
    return [];
  }
  function dateText(value) {
    if (!value) return "Just now";
    const date =
      typeof value === "number"
        ? new Date(value < 1e12 ? value * 1000 : value)
        : new Date(value);
    if (!Number.isFinite(date.getTime())) return "—";
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }
  function shortId(value) {
    return value ? String(value).slice(0, 8) : "—";
  }
  function textValue(value, fallback = "") {
    if (typeof value === "string") return value;
    if (value && typeof value.message === "string") return value.message;
    if (value && typeof value.detail === "string") return value.detail;
    return fallback;
  }
  function errorText(error) {
    return error?.message || "The request could not be completed.";
  }
  function pill(status) {
    const el = node("span", "pill", statusLabels[status] || readable(status));
    setPill(el, status);
    return el;
  }
  function setPill(el, status) {
    el.className = "pill";
    el.textContent = statusLabels[status] || readable(status);
    if (positiveStates.has(status)) el.classList.add("positive");
    else if (warningStates.has(status)) el.classList.add("warning");
    else if (pendingStates.has(status)) el.classList.add("processing");
    else el.classList.add("negative");
  }
  function notify(message) {
    const box = $("global-notice");
    clear(box);
    box.append(node("span", "", message));
    const close = node("button", "", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss notice");
    close.addEventListener("click", () => show(box, false));
    box.append(close);
    show(box);
  }
  function toast(message) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    show($("toast"));
    state.toastTimer = setTimeout(() => show($("toast"), false), 3500);
  }
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
    if (options.body && !(options.body instanceof FormData))
      headers.set("Content-Type", "application/json");
    let response;
    try {
      response = await fetch(path, {
        ...options,
        headers,
        credentials: "same-origin",
      });
    } catch {
      throw new Error(
        "Cannot reach the workspace. Check that the application is running, then refresh.",
      );
    }
    let data;
    if (response.status === 204) data = {};
    else {
      try {
        data = await response.json();
      } catch {
        throw new Error(
          `The workspace returned an unreadable response (${response.status}).`,
        );
      }
    }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        $("connection-label").textContent = "Access required";
        $("connection-dot").className = "status-dot offline";
      }
      const message = textValue(
        data.detail,
        textValue(
          data.error,
          response.status === 401
            ? "An operator token is required. Open workspace connection to enter it."
            : `Request failed (${response.status}).`,
        ),
      );
      const error = new Error(message);
      error.status = response.status;
      error.code = data.code;
      throw error;
    }
    return data;
  }

  function switchView() {
    const view = location.hash.replace(/^#/, "");
    state.view = ["ask", "documents", "evaluations"].includes(view)
      ? view
      : "ask";
    for (const name of ["ask", "documents", "evaluations"])
      show($(`view-${name}`), name === state.view);
    document.querySelectorAll(".nav-link").forEach((link) => {
      const active = link.dataset.view === state.view;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    $("breadcrumb-title").textContent = {
      ask: "Ask your documents",
      documents: "Documents",
      evaluations: "Evaluations",
    }[state.view];
    if (state.view === "documents") refreshDocuments().catch(notifyError);
    if (state.view === "evaluations") refreshEvaluations().catch(notifyError);
    if (state.view === "ask") refreshRuns().catch(notifyError);
  }
  function notifyError(error) {
    notify(errorText(error));
  }

  function corpusPath(path, corpusId = state.corpusId) {
    return `${path}?corpus_id=${encodeURIComponent(corpusId)}`;
  }

  async function refreshCorpora() {
    const result = await api("/api/corpora");
    state.corpora = listOf(result, "corpora", "items");
    const selector = $("corpus-select");
    clear(selector);
    for (const corpus of state.corpora) {
      const option = node("option", "", corpus.id);
      option.value = corpus.id;
      selector.append(option);
    }
    selector.disabled = !state.corpora.length;
    if (
      !state.corpora.some((corpus) => corpus.id === state.corpusId) &&
      state.corpora.length
    ) {
      await selectCorpus(state.corpora[0].id);
    }
    selector.value = state.corpusId;
  }

  async function selectCorpus(corpusId) {
    if (corpusId === state.corpusId) return;
    state.corpusId = corpusId;
    state.selectionVersion++;
    state.queryPoll++;
    state.run = null;
    state.runId = null;
    state.runJobId = null;
    state.evidence = [];
    state.documents = [];
    state.runs = [];
    $("corpus-select").value = corpusId;
    $("breadcrumb-corpus").textContent = corpusId;
    $("ask-submit").disabled = false;
    show($("run-panel"), false);
    clear($("run-answer"));
    clear($("run-meta"));
    show($("ask-welcome"));
    show($("global-notice"), false);
    show($("space-mismatch-banner"), false);
    renderEvidence();
    renderDocuments();
    renderRuns();
    $("upload-status")
      .querySelectorAll("[data-corpus-id]")
      .forEach((item) => show(item, item.dataset.corpusId === corpusId));
    const results = await Promise.allSettled([
      refreshStatus(),
      refreshDocuments(),
      refreshRuns(),
    ]);
    const failure = results.find((result) => result.status === "rejected");
    if (failure && state.corpusId === corpusId) notifyError(failure.reason);
  }

  async function createCorpus(event) {
    event.preventDefault();
    const corpusId = $("new-corpus-id").value.trim();
    const error = $("new-corpus-error");
    show(error, false);
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(corpusId)) {
      error.textContent = "Use 1–64 letters, numbers, underscores, or hyphens.";
      show(error);
      return;
    }
    $("new-corpus-submit").disabled = true;
    try {
      await api("/api/corpora", {
        method: "POST",
        body: JSON.stringify({ corpus_id: corpusId }),
      });
      await refreshCorpora();
      await selectCorpus(corpusId);
      $("new-corpus-dialog").close();
      $("new-corpus-id").value = "";
      toast(`Corpus ${corpusId} is ready.`);
    } catch (failure) {
      error.textContent = errorText(failure);
      show(error);
    } finally {
      $("new-corpus-submit").disabled = false;
    }
  }

  async function refreshStatus() {
    const corpusId = state.corpusId;
    const requestId = ++state.statusRequest;
    try {
      const result = await api(corpusPath("/api/status", corpusId));
      if (corpusId !== state.corpusId || requestId !== state.statusRequest)
        return;
      state.status = result;
      show(
        $("space-mismatch-banner"),
        result.embedding_space_matches === false,
      );
      $("connection-label").textContent = "Workspace connected";
      $("connection-dot").className = "status-dot online";
      const mode = String(
        state.status.mode ?? state.status.runtime?.mode ?? "unknown",
      );
      const mock = mode === "mock" || mode === "fixture";
      $("runtime-badge").textContent = mock
        ? "◈  Fixture mode"
        : mode === "live"
          ? "Live endpoints"
          : readable(mode);
      $("runtime-badge").classList.toggle("live", mode === "live");
      show($("mode-banner"), mock || state.status.policy_state === "shadow");
      if (mock) {
        $("mode-banner-title").textContent = "Fixture mode";
        $("mode-banner-text").textContent =
          "Deterministic demo responses. No model calls. Model quality has not been evaluated.";
      } else if (state.status.policy_state === "shadow") {
        $("mode-banner-title").textContent = "Shadow policy";
        $("mode-banner-text").textContent =
          "Answers are being evaluated. This policy is not qualified for release.";
      }
      show($("question-suggestions"), mock);
      const limits = state.status.limits || state.status.ingestion;
      const uploadBytes = limits?.max_file_bytes ?? limits?.max_upload_bytes;
      $("upload-limit").textContent = uploadBytes
        ? `Up to ${Math.round(uploadBytes / 1048576)} MiB per file · scanned PDFs need OCR before upload`
        : "Workspace admission limits apply · scanned PDFs need OCR before upload";
      renderConnection();
    } catch (error) {
      if (corpusId !== state.corpusId || requestId !== state.statusRequest)
        return;
      $("connection-label").textContent =
        error.status === 401 ? "Access required" : "Connection unavailable";
      $("connection-dot").className = "status-dot offline";
      $("runtime-badge").textContent = "Disconnected";
      renderConnection();
      throw error;
    }
  }

  function renderConnection() {
    const target = $("connection-details");
    clear(target);
    const rows = [
      ["Connection", state.status ? "Connected" : "Unavailable"],
      [
        "Execution mode",
        readable(
          state.status?.mode || state.status?.runtime?.mode || "unknown",
        ),
      ],
      [
        "Policy",
        readable(
          state.status?.policy_state ||
            state.status?.policy?.state ||
            "Not reported",
        ),
      ],
    ];
    const profiles = state.status?.profiles || {};
    for (const [role, profile] of Object.entries(profiles))
      rows.push([
        readable(role),
        typeof profile === "string"
          ? profile
          : profile.model || profile.name || "Configured",
      ]);
    const budgets = state.status?.budgets;
    if (budgets && Number.isFinite(Number(budgets.live_charged_cost)))
      rows.push([
        "Recorded live cost",
        `$${Number(budgets.live_charged_cost).toFixed(4)}`,
      ]);
    if (budgets?.active_calls !== undefined)
      rows.push(["Active calls", budgets.active_calls]);
    for (const [label, value] of rows) {
      const row = node("div", "connection-row");
      row.append(node("span", "", label), node("strong", "", value));
      target.append(row);
    }
  }

  async function refreshDocuments() {
    const corpusId = state.corpusId;
    const requestId = ++state.documentsRequest;
    const result = await api(corpusPath("/api/documents", corpusId));
    if (corpusId !== state.corpusId || requestId !== state.documentsRequest)
      return;
    state.documents = listOf(result, "documents", "items");
    renderDocuments();
    for (const doc of state.documents) {
      const jobId = doc.job_id || doc.latest_job_id;
      if (jobId && pendingStates.has(documentStatus(doc))) pollDocument(jobId);
    }
  }
  function documentStatus(doc) {
    const versionState = String(
      doc.latest_version?.state ??
        doc.latest_status ??
        doc.version_state ??
        doc.status ??
        doc.state ??
        doc.active_version?.state ??
        "queued",
    ).toLowerCase();
    const jobStatus = String(doc.job_status || "").toLowerCase();
    if (pendingStates.has(jobStatus)) return jobStatus;
    if (
      ["failed", "cancelled"].includes(jobStatus) &&
      !["needs_ocr", "needs_review"].includes(versionState)
    )
      return jobStatus;
    return versionState;
  }
  function documentVersion(doc) {
    return (
      doc.version_id ??
      doc.latest_version_id ??
      doc.latest_version?.id ??
      doc.active_version_id ??
      doc.active_version?.id
    );
  }
  function renderDocuments() {
    const target = $("documents-table-body");
    clear(target);
    const uploadRows = $("upload-status").querySelectorAll("[data-job-id]");
    let ready = 0;
    let processing = 0;
    let attention = 0;
    for (const doc of state.documents) {
      const status = documentStatus(doc);
      const currentJobStatus = String(doc.job_status || "").toLowerCase();
      if (
        pendingStates.has(currentJobStatus) ||
        positiveStates.has(currentJobStatus)
      ) {
        for (const item of uploadRows) {
          if (item.dataset.jobId === (doc.job_id || doc.latest_job_id)) {
            item.classList.remove("error");
            item.lastElementChild.textContent =
              statusLabels[currentJobStatus] || readable(currentJobStatus);
          }
        }
      }
      if (status === "ready" || doc.active_version_id) ready++;
      if (pendingStates.has(status)) processing++;
      if (!positiveStates.has(status) && !pendingStates.has(status))
        attention++;
      const name = doc.name ?? doc.filename ?? doc.title ?? "Untitled document";
      const row = node("tr");
      const nameCell = node("td");
      const fileCell = node("div", "document-cell");
      const type = String(name).split(".").pop().toUpperCase();
      fileCell.append(
        node("span", "file-icon", type.length < 6 ? type : "DOC"),
      );
      const desc = node("div");
      desc.append(node("div", "file-name", name));
      const details = [];
      const chunks =
        doc.chunk_count ?? doc.chunks_count ?? doc.active_version?.chunk_count;
      if (chunks !== undefined && chunks !== null)
        details.push(`${chunks} chunk${chunks === 1 ? "" : "s"}`);
      const versionCount = doc.version_count ?? doc.latest_version_no;
      if (versionCount)
        details.push(`${versionCount} version${versionCount === 1 ? "" : "s"}`);
      if (
        doc.active_version_id &&
        doc.active_version_id !== documentVersion(doc)
      )
        details.push("Previous version searchable");
      if (!details.length)
        details.push(`Version ${shortId(documentVersion(doc))}`);
      desc.append(node("div", "file-subtitle", details.join(" · ")));
      fileCell.append(desc);
      nameCell.append(fileCell);
      row.append(nameCell);
      const statusCell = node("td");
      statusCell.append(pill(status));
      row.append(
        statusCell,
        node("td", "", dateText(doc.created_at ?? doc.uploaded_at)),
      );
      const actionCell = node("td");
      const actions = node("div", "document-actions");
      const version = documentVersion(doc);
      if (version) {
        const preview = node("button", "text-button", "Preview ↗");
        preview.type = "button";
        preview.addEventListener("click", () => previewSource(version, name));
        actions.append(preview);
      }
      if (doc.document_id || doc.id) {
        const update = node("button", "text-button", "Update");
        update.type = "button";
        update.setAttribute("aria-label", `Upload a new version of ${name}`);
        update.addEventListener("click", () => {
          state.updateDocumentId = doc.document_id || doc.id;
          state.updateCorpusId = doc.corpus_id || state.corpusId;
          $("version-input").click();
        });
        actions.append(update);
      }
      const jobId = doc.job_id || doc.latest_job_id;
      const recoverable =
        doc.retryable ??
        (["failed", "cancelled"].includes(doc.job_status || status) &&
          !["needs_ocr", "needs_review"].includes(status));
      if (jobId && recoverable) {
        const retry = node("button", "text-button", "Retry");
        retry.type = "button";
        retry.setAttribute("aria-label", `Retry ingestion of ${name}`);
        retry.addEventListener("click", () => retryIngestion(doc, retry));
        actions.append(retry);
      }
      actionCell.append(actions);
      row.append(actionCell);
      target.append(row);
    }
    $("stat-documents").textContent = state.documents.length;
    $("stat-ready").textContent = ready;
    $("stat-processing").textContent = processing;
    $("stat-attention").textContent = attention;
    $("nav-document-count").textContent = state.documents.length;
    $("document-list-count").textContent =
      `${state.documents.length} document${state.documents.length === 1 ? "" : "s"}`;
    $("question-scope").textContent = ready
      ? `Searches ${ready} ready document${ready === 1 ? "" : "s"}`
      : "Searches your indexed documents";
    show($("documents-empty"), !state.documents.length);
    show(document.querySelector(".documents-table"), !!state.documents.length);
  }

  async function retryIngestion(doc, button) {
    const jobId = doc.job_id || doc.latest_job_id;
    button.disabled = true;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, {
        method: "POST",
      });
      doc.job_status = job.status || "queued";
      renderDocuments();
      pollDocument(job.id || job.job_id || jobId);
      await refreshDocuments();
      toast("Ingestion retry queued.");
    } catch (error) {
      notifyError(error);
      button.disabled = false;
    }
  }

  async function uploadFiles(
    files,
    documentId = null,
    corpusId = state.corpusId,
  ) {
    const list = Array.from(files || []);
    if (!list.length) return;
    const limit =
      state.status?.limits?.max_file_bytes ??
      state.status?.limits?.max_upload_bytes ??
      state.status?.ingestion?.max_upload_bytes ??
      Infinity;
    if (state.uploading) {
      toast("Wait for the current upload to finish.");
      return;
    }
    state.uploading = true;
    $("upload-zone").classList.add("uploading");
    $("choose-files").disabled = true;
    for (const file of list) {
      const item = node("div", "upload-item");
      item.dataset.corpusId = corpusId;
      show(item, corpusId === state.corpusId);
      item.append(node("span", "", file.name), node("span", "", "Uploading…"));
      $("upload-status").prepend(item);
      try {
        if (file.size > limit)
          throw new Error(
            `Exceeds the ${Math.round(limit / 1048576)} MiB upload limit.`,
          );
        if (!/\.(txt|md|markdown|pdf)$/i.test(file.name))
          throw new Error("Use a text, Markdown, or PDF file.");
        const body = new FormData();
        body.append("file", file);
        body.append("corpus_id", corpusId);
        if (documentId) body.append("document_id", documentId);
        const result = await api("/api/documents", { method: "POST", body });
        if (result.job_id) item.dataset.jobId = result.job_id;
        item.lastElementChild.textContent = result.duplicate
          ? "Already uploaded"
          : "Queued for indexing";
        if (result.job_id) pollDocument(result.job_id, item);
        await refreshDocuments();
      } catch (error) {
        item.classList.add("error");
        item.lastElementChild.textContent = errorText(error);
      }
    }
    $("file-input").value = "";
    $("version-input").value = "";
    state.updateDocumentId = null;
    state.updateCorpusId = null;
    state.uploading = false;
    $("upload-zone").classList.remove("uploading");
    $("choose-files").disabled = false;
    refreshStatus().catch(() => {});
  }

  async function pollDocument(jobId, item) {
    if (state.uploadPolls.has(jobId)) {
      if (item) state.uploadPolls.set(jobId, item);
      return;
    }
    state.uploadPolls.set(jobId, item || null);
    let failures = 0;
    while (state.uploadPolls.has(jobId)) {
      await sleep(1400);
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        const status = statusOf(job);
        const row = state.uploadPolls.get(jobId);
        if (row)
          row.lastElementChild.textContent =
            statusLabels[status] || readable(status);
        failures = 0;
        if (!pendingStates.has(status)) {
          state.uploadPolls.delete(jobId);
          if (row && !positiveStates.has(status)) {
            row.classList.add("error");
            row.lastElementChild.textContent = textValue(
              job.error,
              statusLabels[status] || readable(status),
            );
          }
          await refreshDocuments();
          refreshStatus().catch(() => {});
          return;
        }
      } catch (error) {
        failures++;
        if (failures >= 4) {
          const row = state.uploadPolls.get(jobId);
          if (row) {
            row.classList.add("error");
            row.lastElementChild.textContent =
              "Status unavailable. Refresh to check again.";
          }
          state.uploadPolls.delete(jobId);
          return;
        }
        await sleep(2000);
      }
    }
  }

  async function previewSource(versionId, fallbackTitle = "Document preview") {
    $("source-dialog-title").textContent = fallbackTitle;
    $("source-dialog-meta").textContent = `Version ${versionId}`;
    $("source-dialog-body").replaceChildren(
      node("p", "source-notice", "Loading the source version…"),
    );
    $("source-download").href =
      `/api/source-versions/${encodeURIComponent(versionId)}/download`;
    $("source-download").dataset.versionId = versionId;
    if (!$("source-dialog").open) $("source-dialog").showModal();
    try {
      const raw = await api(
        `/api/source-versions/${encodeURIComponent(versionId)}`,
      );
      const source = raw.version || raw;
      $("source-dialog-title").textContent =
        source.name ?? source.filename ?? source.title ?? fallbackTitle;
      $("source-dialog-meta").textContent =
        `Version ${versionId} · ${readable(source.state || source.status || "source")} · ${dateText(source.created_at)}`;
      const target = $("source-dialog-body");
      clear(target);
      let pages = listOf(source, "pages", "extracted_pages");
      if (!pages.length && Array.isArray(source.chunks)) {
        pages = source.chunks.map((chunk) => ({
          page: chunk.page,
          text: chunk.text,
        }));
      }
      if (!pages.length && typeof source.text === "string")
        pages = [{ page: 1, text: source.text }];
      if (!pages.length)
        target.append(
          node(
            "p",
            "source-notice",
            "No extracted text is available yet. Check the document’s ingestion status, or download the original.",
          ),
        );
      for (let index = 0; index < pages.length; index++) {
        const page = pages[index];
        const section = node("section", "source-page");
        section.append(
          node("h3", "", `Page ${page.page ?? page.number ?? index + 1}`),
          node(
            "div",
            "source-page-text",
            page.text ??
              page.content ??
              "No text was extracted from this page.",
          ),
        );
        target.append(section);
      }
      const errors = listOf(source, "errors");
      if (errors.length)
        target.prepend(
          node(
            "p",
            "source-notice",
            `Extraction notes: ${errors.map((e) => textValue(e, JSON.stringify(e))).join(" · ")}`,
          ),
        );
    } catch (error) {
      $("source-dialog-body").replaceChildren(
        node("p", "source-notice", errorText(error)),
      );
    }
  }

  async function downloadSource(event) {
    if (!state.token) return;
    event.preventDefault();
    try {
      const response = await fetch(event.currentTarget.href, {
        headers: { Authorization: `Bearer ${state.token}` },
        credentials: "same-origin",
      });
      if (!response.ok)
        throw new Error(`Download failed (${response.status}).`);
      const blob = await response.blob();
      saveBlob(blob, $("source-dialog-title").textContent || "source");
    } catch (error) {
      toast(errorText(error));
    }
  }

  async function refreshRuns() {
    const corpusId = state.corpusId;
    const requestId = ++state.runsRequest;
    const result = await api(corpusPath("/api/runs", corpusId));
    if (corpusId !== state.corpusId || requestId !== state.runsRequest) return;
    state.runs = listOf(result, "runs", "items").filter(
      (run) => (run.corpus_id || "default") === corpusId,
    );
    renderRuns();
  }
  function renderRuns() {
    const target = $("recent-runs");
    clear(target);
    if (!state.runs.length) {
      target.append(
        node("p", "empty-inline", "Your questions will appear here."),
      );
      return;
    }
    for (const run of state.runs.slice(0, 12)) {
      const id = idOf(run);
      const button = node(
        "button",
        "list-row" + (id === state.runId ? " selected" : ""),
      );
      button.type = "button";
      const main = node("span", "list-row-main");
      main.append(
        node("span", "list-row-title", run.question || "Question"),
        node("span", "list-row-meta", dateText(run.created_at)),
      );
      const side = node("span", "list-row-side");
      side.append(pill(statusOf(run)), node("span", "list-row-arrow", "↗"));
      button.append(main, side);
      button.addEventListener("click", () => {
        $("question").value = run.question || "";
        openRun(id, run.job_id);
      });
      target.append(button);
    }
  }

  async function submitQuestion(event) {
    event.preventDefault();
    const question = $("question").value.trim();
    if (!question) {
      $("question").focus();
      return;
    }
    $("ask-submit").disabled = true;
    show($("global-notice"), false);
    const corpusId = state.corpusId;
    const selectionVersion = state.selectionVersion;
    try {
      const result = await api("/api/queries", {
        method: "POST",
        body: JSON.stringify({ question, corpus_id: corpusId }),
      });
      if (selectionVersion !== state.selectionVersion) return;
      const id = result.run_id ?? result.id;
      if (!id)
        throw new Error("The workspace did not return a run identifier.");
      state.run = { ...result, question, status: result.status || "queued" };
      state.runId = id;
      state.runJobId = result.job_id;
      renderRun();
      openRun(id, result.job_id);
      refreshRuns().catch(() => {});
    } catch (error) {
      if (selectionVersion !== state.selectionVersion) return;
      notifyError(error);
      $("ask-submit").disabled = false;
    }
  }

  async function openRun(id, jobId) {
    if (!id) return;
    const token = ++state.queryPoll;
    state.runId = id;
    state.runJobId = jobId || null;
    if (state.run?.id !== id && state.run?.run_id !== id) {
      state.run = { id, status: "queued", question: $("question").value };
      renderRun();
    }
    renderRuns();
    let failures = 0;
    while (token === state.queryPoll) {
      try {
        const result = await api(`/api/runs/${encodeURIComponent(id)}`);
        if (token !== state.queryPoll) return;
        state.run = result.run || result;
        state.runJobId = state.run.job_id || state.runJobId;
        renderRun();
        failures = 0;
        if (!pendingStates.has(statusOf(state.run))) {
          $("ask-submit").disabled = false;
          await Promise.allSettled([refreshRuns(), refreshStatus()]);
          return;
        }
      } catch (error) {
        failures++;
        if (failures >= 4 || error.status === 404 || error.status === 401) {
          state.run = {
            ...state.run,
            status: "status_unavailable",
            message:
              "The run may still be processing. Refresh recent questions to reconnect.",
          };
          renderRun();
          notifyError(error);
          $("ask-submit").disabled = false;
          return;
        }
      }
      await sleep(failures ? 2500 : 1200);
    }
  }

  function renderRun() {
    const run = state.run || {};
    const status = statusOf(run);
    const active = pendingStates.has(status);
    show($("run-panel"));
    show($("ask-welcome"), false);
    setPill($("run-status"), status);
    show($("run-progress"), active);
    show($("run-cancel"), active && !!state.runJobId);
    $("ask-submit").disabled = active;
    const stage = String(run.stage || run.progress?.stage || status);
    const progress = progressLabels[stage] || progressLabels.running;
    $("run-progress-title").textContent = progress[0];
    $("run-progress-text").textContent = progress[1];
    const answer = $("run-answer");
    clear(answer);
    clear($("run-meta"));
    show($("copy-answer"), false);
    show($("open-trace"), !!state.runId);
    const pack =
      run.evidence_pack ||
      run.evidence ||
      run.result?.evidence_pack ||
      run.result?.evidence;
    state.evidence = Array.isArray(pack)
      ? pack
      : listOf(pack, "items", "evidence");
    renderEvidence();
    // Only released answer fields are used here. Drafts and trace payloads are never public answer sources.
    const released = run.answer ?? run.released_answer ?? run.result?.answer;
    const blocks = Array.isArray(released)
      ? released
      : Array.isArray(released?.blocks)
        ? released.blocks
        : Array.isArray(run.blocks)
          ? run.blocks
          : Array.isArray(run.answer_blocks)
            ? run.answer_blocks
            : [];
    const releasedText =
      typeof released === "string"
        ? released
        : typeof released?.text === "string"
          ? released.text
          : null;
    const canDisplayAnswer =
      !active &&
      [
        "answered",
        "accepted",
        "released",
        "complete",
        "completed",
        "success",
        "succeeded",
        "done",
      ].includes(status);
    if (canDisplayAnswer && (blocks.length || releasedText)) {
      if (blocks.length)
        for (const block of blocks) {
          const paragraph = node("div", "answer-block");
          paragraph.append(node("span", "answer-text", block.text || ""));
          const citations = node("span", "citations");
          for (const citationId of block.citation_ids || []) {
            const index = state.evidence.findIndex(
              (item) => item.id === citationId,
            );
            if (index < 0) continue;
            const button = node("button", "citation-button", index + 1);
            button.type = "button";
            button.setAttribute(
              "aria-label",
              `View source ${index + 1}: ${state.evidence[index].title}`,
            );
            button.addEventListener("click", () => highlightEvidence(index));
            citations.append(button);
          }
          if (citations.childElementCount) paragraph.append(citations);
          answer.append(paragraph);
        }
      else answer.append(node("div", "answer-block", releasedText));
      show($("copy-answer"));
    } else if (!active) {
      const semantic = [
        "abstained",
        "abstention",
        "unsupported",
        "not_supported",
      ].includes(status);
      const cancelled = ["cancelled", "canceled"].includes(status);
      const shadow = ["shadow", "policy_not_ready"].includes(status);
      const title = semantic
        ? "The evidence did not support a releasable answer."
        : cancelled
          ? "This run was cancelled."
          : shadow
            ? "This policy is not ready to release answers."
            : status === "status_unavailable"
              ? "The run status is unavailable."
              : "The answer could not be verified.";
      let message = semantic
        ? "Try a more specific question or add a document with the missing evidence."
        : shadow
          ? "Inspect the operator trace to review this development run. No draft has been released as an answer."
          : cancelled
            ? "You can submit the question again when you are ready."
            : "A technical issue interrupted the checks. This does not mean the documents lack the answer.";
      // Abstentions can contain server-owned fixed messages, never a draft fallback.
      message = textValue(
        run.message,
        textValue(run.error, textValue(run.abstention, message)),
      );
      if (semantic && releasedText) message = releasedText;
      const box = node(
        "div",
        "answer-state" + (!semantic && !cancelled && !shadow ? " error" : ""),
      );
      const details = node("div");
      details.append(node("h3", "", title), node("p", "", message));
      box.append(
        node("span", "answer-state-symbol", semantic || shadow ? "↳" : "!"),
        details,
      );
      answer.append(box);
    }
    if (!active) {
      const metadata = [`Run ${shortId(state.runId)}`];
      const elapsed =
        run.elapsed_seconds ??
        run.duration_seconds ??
        run.timings?.worker_total_seconds ??
        run.metrics?.latency_seconds;
      if (Number.isFinite(elapsed)) metadata.push(`${elapsed.toFixed(1)} s`);
      if (
        run.repaired ||
        run.repair_count > 0 ||
        run.timings?.repair_generation_seconds !== undefined
      )
        metadata.push("One repair, rechecked");
      if (
        run.qualification === "fixture_only" ||
        run.mode === "mock" ||
        state.status?.mode === "mock"
      )
        metadata.push("Deterministic fixture");
      for (const value of metadata)
        $("run-meta").append(node("span", "", value));
    }
  }

  function renderEvidence() {
    const target = $("evidence-list");
    clear(target);
    $("evidence-count").textContent =
      `${state.evidence.length} passage${state.evidence.length === 1 ? "" : "s"}`;
    if (!state.evidence.length) {
      const box = node("div", "evidence-empty");
      box.append(
        node("span", "outline-doc", "▤"),
        node("h3", "", "The source, right here."),
        node(
          "p",
          "",
          "Supporting passages will appear when a run retrieves evidence.",
        ),
      );
      target.append(box);
      return;
    }
    for (let index = 0; index < state.evidence.length; index++) {
      const item = state.evidence[index];
      const card = node("article", "evidence-card");
      card.id = `evidence-${index}`;
      card.tabIndex = -1;
      const head = node("div", "evidence-card-header");
      head.append(
        node("span", "evidence-number", index + 1),
        node(
          "span",
          "evidence-title",
          item.title || item.name || "Source document",
        ),
      );
      card.append(head, node("p", "evidence-text", item.text));
      const meta = node("div", "evidence-meta");
      meta.append(
        node(
          "span",
          "",
          `Page ${item.page ?? 1} · v${shortId(item.version_id)}`,
        ),
      );
      if (item.version_id) {
        const preview = node("button", "text-button", "Open source ↗");
        preview.type = "button";
        preview.addEventListener("click", () =>
          previewSource(item.version_id, item.title),
        );
        meta.append(preview);
      }
      card.append(meta);
      target.append(card);
    }
  }
  function highlightEvidence(index) {
    const card = $(`evidence-${index}`);
    if (!card) return;
    document
      .querySelectorAll(".evidence-card.highlight")
      .forEach((el) => el.classList.remove("highlight"));
    card.classList.add("highlight");
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    card.focus({ preventScroll: true });
  }
  async function showTrace() {
    if (!state.runId) return;
    $("trace-dialog-title").textContent = "Run trace";
    $("trace-content").textContent = "Loading operator trace…";
    if (!$("trace-dialog").open) $("trace-dialog").showModal();
    try {
      const result = await api(
        `/api/runs/${encodeURIComponent(state.runId)}/trace`,
      );
      $("trace-content").textContent = JSON.stringify(result, null, 2);
    } catch (error) {
      $("trace-content").textContent = errorText(error);
    }
  }
  async function cancelRun() {
    if (!state.runJobId) return;
    $("run-cancel").disabled = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(state.runJobId)}/cancel`, {
        method: "POST",
      });
      toast("Cancellation requested.");
    } catch (error) {
      notifyError(error);
    } finally {
      $("run-cancel").disabled = false;
    }
  }

  async function refreshEvaluations() {
    const result = await api("/api/evaluations");
    state.evaluations = listOf(result, "evaluations", "jobs", "items");
    renderEvaluationHistory();
  }
  function renderEvaluationHistory() {
    const target = $("evaluation-history");
    clear(target);
    if (!state.evaluations.length) {
      target.append(node("p", "empty-inline", "No evaluation runs yet."));
      return;
    }
    for (const run of state.evaluations) {
      const id = idOf(run);
      const button = node(
        "button",
        "list-row" + (id === state.evaluationId ? " selected" : ""),
      );
      button.type = "button";
      const main = node("span", "list-row-main");
      main.append(
        node(
          "span",
          "list-row-title",
          run.name ||
            `${readable(run.dataset || run.payload?.dataset || "demo")} evaluation`,
        ),
        node(
          "span",
          "list-row-meta",
          `${dateText(run.created_at)} · ${shortId(id)}`,
        ),
      );
      const side = node("span", "list-row-side");
      side.append(pill(statusOf(run)), node("span", "list-row-arrow", "↗"));
      button.append(main, side);
      button.addEventListener("click", () => openEvaluation(id));
      target.append(button);
    }
  }
  async function startEvaluation() {
    $("start-evaluation").disabled = true;
    show($("global-notice"), false);
    try {
      const result = await api("/api/evaluations", {
        method: "POST",
        body: JSON.stringify({ dataset: "demo" }),
      });
      const id = result.job_id ?? result.id ?? result.evaluation_id;
      if (!id)
        throw new Error(
          "The workspace did not return an evaluation identifier.",
        );
      state.evaluation = { ...result, status: result.status || "queued" };
      state.evaluationId = id;
      renderEvaluation();
      openEvaluation(id);
      refreshEvaluations().catch(() => {});
    } catch (error) {
      notifyError(error);
      $("start-evaluation").disabled = false;
    }
  }
  async function openEvaluation(id) {
    if (!id) return;
    const token = ++state.evaluationPoll;
    state.evaluationId = id;
    state.evaluation = { id, status: "queued" };
    renderEvaluation();
    renderEvaluationHistory();
    let failures = 0;
    while (token === state.evaluationPoll) {
      try {
        const result = await api(`/api/evaluations/${encodeURIComponent(id)}`);
        if (token !== state.evaluationPoll) return;
        state.evaluation = result.evaluation || result;
        renderEvaluation();
        failures = 0;
        if (!pendingStates.has(statusOf(state.evaluation))) {
          $("start-evaluation").disabled = false;
          await Promise.allSettled([
            refreshEvaluations(),
            refreshDocuments(),
            refreshStatus(),
          ]);
          return;
        }
      } catch (error) {
        failures++;
        if (failures >= 4 || error.status === 404 || error.status === 401) {
          notifyError(error);
          $("start-evaluation").disabled = false;
          return;
        }
      }
      await sleep(failures ? 2500 : 1700);
    }
  }

  function renderEvaluation() {
    const run = state.evaluation || {};
    const status = statusOf(run);
    const active = pendingStates.has(status);
    const result = run.result || run.report || run;
    show($("evaluation-current"));
    setPill($("evaluation-status"), status);
    $("evaluation-title").textContent = active
      ? "Evaluation in progress"
      : "Evaluation results";
    const target = $("evaluation-body");
    clear(target);
    show($("evaluation-cancel"), active);
    show($("evaluation-export"), !active && !!state.evaluation);
    $("start-evaluation").disabled = active;
    if (active) {
      const progress = node("div", "run-progress");
      progress.append(node("span", "spinner"));
      const detail = node("div");
      detail.append(
        node(
          "strong",
          "",
          status === "queued"
            ? "Waiting for the evaluation worker"
            : "Comparing the answer policies",
        ),
        node(
          "p",
          "",
          "The run and its results remain available in the history.",
        ),
      );
      progress.append(detail);
      target.append(progress);
      return;
    }
    if (!positiveStates.has(status)) {
      const box = node("div", "evaluation-results-intro");
      box.append(
        node(
          "p",
          "",
          textValue(
            run.error,
            textValue(
              run.message,
              "The evaluation did not finish. Its partial results are available in the export when recorded.",
            ),
          ),
        ),
      );
      target.append(box);
    }
    const note =
      result.note || result.qualification?.reason || result.disclaimer;
    const fixture =
      result.fixture_only ||
      result.runtime_mode === "mock" ||
      result.mode === "mock";
    target.append(
      node(
        "p",
        "evaluation-results-intro",
        typeof note === "string"
          ? note
          : fixture
            ? "Synthetic demo results describe the fixture workflow. They do not qualify any model or release policy."
            : "Results apply to this evaluation and its recorded review coverage. Inspect the qualification requirements before using a release policy.",
      ),
    );
    if (result.qualification && typeof result.qualification === "object") {
      const qualification = node("div", "evaluation-qualification");
      const qualified = result.qualification.qualified === true;
      qualification.append(
        node(
          "span",
          "pill " + (qualified ? "positive" : "warning"),
          qualified ? "Quality gates passed" : "Quality not qualified",
        ),
      );
      qualification.append(
        node(
          "span",
          "",
          result.qualification.human_reviewed
            ? "Human review recorded"
            : "Human review incomplete",
        ),
      );
      target.append(qualification);
    }
    const metrics = result.metrics || result.summary || result;
    const variants =
      metrics.variants || result.variants || metrics.variant_metrics;
    if (variants && typeof variants === "object")
      renderVariantMetrics(target, variants);
    const controlled = metrics.controlled;
    if (controlled && typeof controlled === "object") {
      const rows = [];
      for (const [key, label] of [
        ["false_acceptance", "Unsupported claims accepted"],
        ["supported_retention", "Supported claims retained"],
        ["operational_completion", "Completed claim checks"],
      ])
        if (controlled[key]) rows.push([label, formatMetric(controlled[key])]);
      if (rows.length) {
        target.append(
          node("h3", "metric-section-title", "Controlled claim challenge"),
        );
        target.append(
          node(
            "p",
            "evaluation-results-intro",
            "Supplied claims, repair disabled. These rates are separate from natural answer errors.",
          ),
        );
        appendMetricTable(target, ["Measure", "Count and interval"], rows);
      }
    }
    const overviewKeys = [
      "total_questions",
      "questions",
      "attempted",
      "completed",
      "technical_failures",
      "cost_usd",
      "estimated_cost_usd",
      "duration_seconds",
      "mode",
      "qualified",
      "qualification_status",
    ];
    const pairs = [];
    for (const key of overviewKeys) {
      const value = metrics[key] ?? result[key];
      if (value !== undefined && value !== null && typeof value !== "object")
        pairs.push([readable(key), formatMetric(value)]);
    }
    if (metrics.required_evidence_coverage_at_8)
      pairs.push([
        "Required evidence coverage",
        formatMetric(metrics.required_evidence_coverage_at_8),
      ]);
    if (result.runtime_mode)
      pairs.push(["Execution mode", readable(result.runtime_mode)]);
    if (result.ledger_summary) {
      pairs.push(["Recorded attempts", result.ledger_summary.attempts]);
      if (Number.isFinite(result.ledger_summary.known_actual_usd))
        pairs.push([
          "Known recorded cost",
          `$${result.ledger_summary.known_actual_usd.toFixed(4)}`,
        ]);
      if (result.ledger_summary.unknown_usage_or_cost_attempts)
        pairs.push([
          "Attempts with unknown usage or cost",
          result.ledger_summary.unknown_usage_or_cost_attempts,
        ]);
    }
    if (pairs.length)
      appendMetricTable(target, ["Run summary", "Value"], pairs);
    const details = node("div", "metrics-details");
    const button = node(
      "button",
      "text-button",
      "View evaluation operator trace ↗",
    );
    button.type = "button";
    button.addEventListener("click", () => {
      $("trace-dialog-title").textContent = "Evaluation trace";
      $("trace-content").textContent = JSON.stringify(result, null, 2);
      if (!$("trace-dialog").open) $("trace-dialog").showModal();
    });
    details.append(button);
    target.append(details);
  }
  function formatMetric(value) {
    if (value === null || value === undefined) return "Not measured";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "number")
      return Number.isInteger(value)
        ? String(value)
        : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    if (typeof value === "object") {
      if (String(value.status || "").startsWith("not_measured"))
        return "Not measured";
      if (value.status === "incomplete_review" || value.pending > 0)
        return `Awaiting review${Number.isFinite(value.pending) ? ` · ${value.pending} pending` : ""}`;
      if (value.numerator !== undefined && value.denominator !== undefined) {
        const rate = value.rate ?? value.value;
        let text = `${value.numerator} / ${value.denominator}${Number.isFinite(rate) ? ` (${(rate * 100).toFixed(1)}%)` : ""}`;
        if (Array.isArray(value.ci95_wilson) && value.ci95_wilson.length === 2)
          text += `\n95% CI: ${(value.ci95_wilson[0] * 100).toFixed(1)}–${(value.ci95_wilson[1] * 100).toFixed(1)}%`;
        return text;
      }
      if (value.responses !== undefined && value.reviewed !== undefined)
        return `${value.reviewed} / ${value.responses} reviewed`;
      if (value.value !== undefined && typeof value.value !== "object")
        return formatMetric(value.value);
      return JSON.stringify(value);
    }
    return String(value);
  }
  function appendMetricTable(target, headers, rows) {
    const wrap = node("div", "evaluation-metrics");
    const comparison = headers.length > 2;
    const table = node(
      "table",
      "metrics-table" + (comparison ? " comparison-table" : ""),
    );
    const head = node("thead");
    const heading = node("tr");
    for (const label of headers) {
      const th = node("th", "", label);
      th.scope = "col";
      heading.append(th);
    }
    head.append(heading);
    table.append(head);
    const body = node("tbody");
    for (const values of rows) {
      const row = node("tr");
      values.forEach((value) => row.append(node("td", "metrics-value", value)));
      body.append(row);
    }
    table.append(body);
    wrap.append(table);
    if (comparison) {
      const hint = node(
        "p",
        "table-scroll-hint",
        "Scroll to compare all four variants →",
      );
      target.append(hint);
      wrap.tabIndex = 0;
      wrap.setAttribute("role", "region");
      wrap.setAttribute(
        "aria-label",
        "Variant comparison table, scroll horizontally on a narrow screen",
      );
    }
    target.append(wrap);
  }
  function renderVariantMetrics(target, variants) {
    const entries = Array.isArray(variants)
      ? variants.map((value, index) => [
          value.variant || value.name || String.fromCharCode(65 + index),
          value,
        ])
      : Object.entries(variants);
    if (!entries.length) return;
    const keys = [
      ...new Set(
        entries.flatMap(([, value]) =>
          Object.keys(value?.metrics || value || {}),
        ),
      ),
    ].filter(
      (key) =>
        ![
          "name",
          "variant",
          "rows",
          "cases",
          "records",
          "results",
          "errors",
          "runs",
          "statuses",
          "zero_error_upper95_if_independent",
        ].includes(key),
    );
    const preferred = [
      "attempted",
      "live_completion",
      "released",
      "released_count",
      "substantive_released",
      "correct_complete",
      "correct_and_complete",
      "unsupported_released",
      "unsupported_release_rate",
      "false_acceptance_rate",
      "supported_retention",
      "abstained",
      "missing_evidence_handling",
      "conflict_handling",
      "final_response_audit",
      "reviewed_substantive_releases",
      "technical_failures",
      "latency_seconds",
      "cost_usd",
    ];
    keys.sort((a, b) => {
      const ia = preferred.indexOf(a),
        ib = preferred.indexOf(b);
      return (ia < 0 ? 100 : ia) - (ib < 0 ? 100 : ib) || a.localeCompare(b);
    });
    const labels = {
      live_completion: "Completed runs",
      released_count: "Answers released",
      correct_and_complete: "Correct and complete",
      unsupported_release_rate: "Unsupported answer rate",
      missing_evidence_handling: "Missing-evidence handling",
      conflict_handling: "Conflict handling",
      final_response_audit: "Final response audit",
      reviewed_substantive_releases: "Substantive answers reviewed",
    };
    const rows = keys
      .slice(0, 18)
      .map((key) => [
        labels[key] || readable(key),
        ...entries.map(([, value]) =>
          formatMetric((value.metrics || value)[key]),
        ),
      ]);
    appendMetricTable(
      target,
      ["Metric", ...entries.map(([key]) => readable(key))],
      rows,
    );
  }
  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const link = node("a");
    link.href = url;
    link.download = name.replace(/[\\/]/g, "_");
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  async function cancelEvaluation() {
    if (!state.evaluationId) return;
    $("evaluation-cancel").disabled = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(state.evaluationId)}/cancel`, {
        method: "POST",
      });
      toast("Cancellation requested.");
    } catch (error) {
      notifyError(error);
    } finally {
      $("evaluation-cancel").disabled = false;
    }
  }

  window.addEventListener("hashchange", switchView);
  $("question-form").addEventListener("submit", submitQuestion);
  $("question").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      if (!$("ask-submit").disabled) $("question-form").requestSubmit();
    }
  });
  document.querySelectorAll("[data-question]").forEach((button) =>
    button.addEventListener("click", () => {
      $("question").value = button.dataset.question;
      $("question").focus();
    }),
  );
  $("choose-files").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (event) =>
    uploadFiles(event.target.files),
  );
  $("version-input").addEventListener("change", (event) =>
    uploadFiles(
      event.target.files,
      state.updateDocumentId,
      state.updateCorpusId || state.corpusId,
    ),
  );
  const zone = $("upload-zone");
  for (const type of ["dragenter", "dragover"])
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.add("drag-over");
    });
  for (const type of ["dragleave", "drop"])
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.remove("drag-over");
    });
  zone.addEventListener("drop", (event) =>
    uploadFiles(event.dataTransfer.files),
  );
  $("documents-refresh").addEventListener("click", () =>
    refreshDocuments()
      .then(() => toast("Documents refreshed."))
      .catch(notifyError),
  );
  $("runs-refresh").addEventListener("click", () =>
    refreshRuns()
      .then(() => toast("Questions refreshed."))
      .catch(notifyError),
  );
  $("evaluations-refresh").addEventListener("click", () =>
    refreshEvaluations()
      .then(() => toast("Evaluations refreshed."))
      .catch(notifyError),
  );
  $("open-trace").addEventListener("click", showTrace);
  $("run-cancel").addEventListener("click", cancelRun);
  $("source-download").addEventListener("click", downloadSource);
  $("copy-answer").addEventListener("click", async () => {
    const text = Array.from(
      $("run-answer").querySelectorAll(".answer-text,.answer-block"),
    )
      .filter(
        (el) =>
          el.classList.contains("answer-text") ||
          !el.querySelector(".answer-text"),
      )
      .map((el) => el.textContent)
      .join("\n\n");
    try {
      await navigator.clipboard.writeText(text);
      toast("Answer copied.");
    } catch {
      toast(
        "Copy is unavailable in this browser. Select the answer text to copy it.",
      );
    }
  });
  $("start-evaluation").addEventListener("click", startEvaluation);
  $("evaluation-cancel").addEventListener("click", cancelEvaluation);
  $("evaluation-export").addEventListener("click", () => {
    if (state.evaluation)
      saveBlob(
        new Blob([JSON.stringify(state.evaluation, null, 2) + "\n"], {
          type: "application/json",
        }),
        `evaluation-${shortId(state.evaluationId)}.json`,
      );
  });
  $("connection-open").addEventListener("click", () => {
    renderConnection();
    $("connection-dialog").showModal();
  });
  // The runtime badge is also an access entry point on narrow screens.
  $("runtime-badge").tabIndex = 0;
  $("runtime-badge").setAttribute("role", "button");
  $("runtime-badge").setAttribute("aria-label", "Open workspace connection");
  $("runtime-badge").addEventListener("click", () => {
    renderConnection();
    $("connection-dialog").showModal();
  });
  $("runtime-badge").addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      $("runtime-badge").click();
    }
  });
  $("token-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    state.token = $("operator-token").value.trim();
    try {
      await refreshStatus();
      await Promise.all([
        refreshCorpora(),
        refreshDocuments(),
        refreshRuns(),
        refreshEvaluations(),
      ]);
      $("connection-dialog").close();
      show($("global-notice"), false);
      toast("Workspace connected.");
    } catch (error) {
      notifyError(error);
    }
  });
  $("corpus-select").addEventListener("change", (event) =>
    selectCorpus(event.target.value),
  );
  $("new-corpus-open").addEventListener("click", () => {
    show($("new-corpus-error"), false);
    $("new-corpus-dialog").showModal();
    $("new-corpus-id").focus();
  });
  $("new-corpus-form").addEventListener("submit", createCorpus);
  document
    .querySelectorAll("[data-close-dialog]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        $(button.dataset.closeDialog).close(),
      ),
    );
  document.querySelectorAll("dialog").forEach((dialog) =>
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      )
        dialog.close();
    }),
  );
  switchView();
  Promise.allSettled([
    refreshCorpora(),
    refreshStatus(),
    refreshDocuments(),
  ]).then((results) => {
    const error = results.find((result) => result.status === "rejected");
    if (error) notifyError(error.reason);
  });
})();
