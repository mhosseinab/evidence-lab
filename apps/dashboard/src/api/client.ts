import type { GraphStep, Orchestration, Payload } from "../types/api";
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
  }
}
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
export function parsePayload(input: unknown): Payload {
  if (!isRecord(input)) throw new Error("The workspace returned an invalid object response.");
  const value = Object.fromEntries(Object.entries(input).filter(([, field]) => field !== null));
  for (const key of [
    "id",
    "run_id",
    "job_id",
    "evaluation_id",
    "corpus_id",
    "conversation_id",
    "document_id",
    "version_id",
    "latest_version_id",
    "active_version_id",
    "latest_job_id",
    "status",
    "state",
    "stage",
    "mode",
    "runtime_mode",
    "policy_state",
    "name",
    "filename",
    "title",
    "text",
    "content",
    "question",
    "dataset",
    "model",
    "latest_status",
    "version_state",
    "job_status",
    "created_at",
    "uploaded_at",
    "note",
    "disclaimer",
    "abstention",
    "code",
    "variant",
  ]) {
    const field = value[key];
    if (field != null && typeof field !== "string")
      throw new Error(`The workspace returned an invalid ${key} field.`);
  }
  for (const key of [
    "active_calls",
    "chunk_count",
    "chunks_count",
    "version_count",
    "latest_version_no",
    "page",
    "number",
    "max_file_bytes",
    "max_upload_bytes",
    "live_charged_cost",
    "duration_seconds",
    "elapsed_seconds",
    "repair_count",
    "numerator",
    "denominator",
    "pending",
    "responses",
    "reviewed",
    "rate",
    "value",
    "worker_total_seconds",
    "repair_generation_seconds",
    "latency_seconds",
    "attempts",
    "known_actual_usd",
    "unknown_usage_or_cost_attempts",
  ]) {
    const field = value[key];
    if (field != null && typeof field !== "number")
      throw new Error(`The workspace returned an invalid ${key} field.`);
  }
  for (const key of [
    "duplicate",
    "retryable",
    "embedding_space_matches",
    "fixture_only",
    "repaired",
    "qualified",
    "human_reviewed",
  ]) {
    const field = value[key];
    if (field != null && typeof field !== "boolean")
      throw new Error(`The workspace returned an invalid ${key} field.`);
  }
  for (const key of [
    "runtime",
    "policy",
    "limits",
    "ingestion",
    "budgets",
    "latest_version",
    "active_version",
    "progress",
    "run",
    "evaluation",
    "result",
    "report",
    "summary",
    "metrics",
    "payload",
    "timings",
    "ledger_summary",
  ]) {
    if (value[key] != null) value[key] = parsePayload(value[key]);
  }
  if (value.version != null && typeof value.version !== "string") parsePayload(value.version);
  const pageCount = value.pages;
  if (pageCount != null && !Array.isArray(pageCount) && typeof pageCount !== "number")
    throw new Error("The workspace returned an invalid pages field.");
  if (Array.isArray(pageCount)) value.pages = pageCount.map(parsePayload);
  const errors = value.errors;
  if (errors != null) {
    if (!Array.isArray(errors)) throw new Error("The workspace returned an invalid errors list.");
    value.errors = errors.map((error: unknown) => (typeof error === "string" ? error : parsePayload(error)));
  }
  const controlled = value.controlled;
  if (controlled != null) {
    value.controlled = Array.isArray(controlled) ? controlled.map(parsePayload) : parsePayload(controlled);
  }
  for (const key of [
    "blocks",
    "answer_blocks",
    "chunks",
    "extracted_pages",
    "items",
    "documents",
    "corpora",
    "runs",
    "evaluations",
    "jobs",
  ]) {
    const field = value[key];
    if (field != null) {
      if (!Array.isArray(field)) throw new Error(`The workspace returned an invalid ${key} list.`);
      value[key] = field.map(parsePayload);
    }
  }
  for (const [key, type] of [
    ["citation_ids", "string"],
    ["ci95_wilson", "number"],
  ]) {
    const field = value[key ?? ""];
    if (field != null && (!Array.isArray(field) || field.some((item: unknown) => typeof item !== type)))
      throw new Error(`The workspace returned an invalid ${key} list.`);
  }
  for (const key of [
    "answer",
    "released_answer",
    "evidence",
    "evidence_pack",
    "qualification",
    "message",
    "detail",
    "error",
  ]) {
    const field = value[key];
    if (field == null || typeof field === "string") continue;
    value[key] = Array.isArray(field) ? field.map(parsePayload) : parsePayload(field);
  }
  for (const key of ["profiles", "variants", "variant_metrics"]) {
    const field = value[key];
    if (field == null) continue;
    if (!isRecord(field) && !Array.isArray(field))
      throw new Error(`The workspace returned an invalid ${key} field.`);
    value[key] = Array.isArray(field)
      ? field.map(parsePayload)
      : Object.fromEntries(
          Object.entries(field).map(([name, entry]) => [
            name,
            key === "profiles" && typeof entry === "string" ? entry : parsePayload(entry),
          ]),
        );
  }
  if (value.orchestration != null) value.orchestration = parseOrchestration(value.orchestration);
  if (value.graph_steps != null) {
    if (!Array.isArray(value.graph_steps))
      throw new Error("The workspace returned an invalid graph steps list.");
    value.graph_steps = value.graph_steps.map(parseGraphStep);
  }
  return value as Payload;
}

function invalidWorkflow(): never {
  throw new Error("The workspace returned invalid workflow metadata.");
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}
function parseGraphStep(value: unknown): GraphStep {
  if (
    !isRecord(value) ||
    typeof value.node !== "string" ||
    !["completed", "failed"].includes(String(value.status)) ||
    typeof value.elapsed_seconds !== "number" ||
    !Number.isFinite(value.elapsed_seconds) ||
    value.elapsed_seconds < 0
  )
    invalidWorkflow();
  return {
    node: value.node,
    status: value.status as GraphStep["status"],
    elapsed_seconds: value.elapsed_seconds,
  };
}
function parseOrchestration(value: unknown): Orchestration {
  if (
    !isRecord(value) ||
    value.engine !== "langgraph" ||
    !strings(value.nodes) ||
    !strings(value.tools) ||
    !Array.isArray(value.edges) ||
    !value.edges.every((edge) => strings(edge) && edge.length === 2) ||
    !isRecord(value.memory) ||
    typeof value.memory.enabled !== "boolean" ||
    typeof value.memory.max_turns !== "number" ||
    !Number.isInteger(value.memory.max_turns) ||
    value.memory.max_turns < 0 ||
    !isRecord(value.tracing) ||
    value.tracing.provider !== "langsmith" ||
    typeof value.tracing.enabled !== "boolean" ||
    value.tracing.content !== "metadata_only"
  )
    invalidWorkflow();
  return value as unknown as Orchestration;
}

export function createApiClient(getToken: () => string, onAccessRequired: () => void = () => {}) {
  async function request(path: string, options: RequestInit = {}): Promise<Payload> {
    const response = await fetchResponse(path, options);
    let data: Payload;
    if (response.status === 204) data = {};
    else {
      let raw: unknown;
      try {
        raw = await response.json();
      } catch {
        throw new Error(`The workspace returned an unreadable response (${response.status}).`);
      }
      data = Array.isArray(raw) ? { items: raw.map(parsePayload) } : parsePayload(raw);
    }
    if (!response.ok) {
      const field = data.detail ?? data.error;
      const message =
        typeof field === "string"
          ? field
          : isRecord(field) && typeof field.message === "string"
            ? field.message
            : response.status === 401
              ? "An operator token is required. Open workspace connection to enter it."
              : `Request failed (${response.status}).`;
      throw new ApiError(message, response.status, data.code);
    }
    return data;
  }
  async function fetchResponse(path: string, options: RequestInit = {}): Promise<Response> {
    const headers = new Headers(options.headers);
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    try {
      const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
      if (response.status === 401 || response.status === 403) onAccessRequired();
      return response;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new Error("Cannot reach the workspace. Check that the application is running, then refresh.");
    }
  }
  async function download(path: string, options: RequestInit = {}) {
    const response = await fetchResponse(path, options);
    if (!response.ok) throw new ApiError(`Download failed (${response.status}).`, response.status);
    return response.blob();
  }
  return { request, download };
}
