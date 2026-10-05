import { isRecord, parsePayload } from "../api/client";
import type { Payload } from "../types/api";
export const pendingStates = new Set([
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
export const positiveStates = new Set([
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
export const warningStates = new Set([
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
export const statusLabels: Record<string, string> = {
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
export const progressLabels: Record<string, [string, string]> = {
  queued: ["Waiting to start", "The worker will pick up this question shortly."],
  retrieving: ["Finding supporting evidence", "Searching the indexed documents."],
  generating: ["Drafting an answer", "The draft stays private while it is checked."],
  verifying: ["Checking the answer", "Checking each answer block and the complete response."],
  repairing: ["Rechecking a revised answer", "One repair is allowed, followed by a complete check."],
  running: ["Working on your question", "Finding evidence, drafting, and checking the response."],
  processing: ["Working on your question", "Finding evidence, drafting, and checking the response."],
};

export function readable(value: unknown) {
  return String(value ?? "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (x) => x.toUpperCase());
}
export function statusOf(value: Payload | null) {
  return String(value?.status ?? value?.state ?? "queued").toLowerCase();
}
export function idOf(value: Payload | null) {
  return value?.id ?? value?.run_id ?? value?.job_id ?? null;
}
export function listOf(value: unknown, ...keys: string[]): Payload[] {
  if (Array.isArray(value)) return value.map(parsePayload);
  if (isRecord(value))
    for (const key of keys) if (Array.isArray(value[key])) return value[key].map(parsePayload);
  return [];
}
export function dateText(value: string | number | undefined) {
  if (!value) return "Just now";
  const date = typeof value === "number" ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
export function shortId(value: unknown) {
  return value ? String(value).slice(0, 8) : "—";
}
export function textValue(value: unknown, fallback = "") {
  if (typeof value === "string") return value;
  if (isRecord(value) && typeof value.message === "string") return value.message;
  if (isRecord(value) && typeof value.detail === "string") return value.detail;
  return fallback;
}
export function errorText(error: unknown) {
  return error instanceof Error ? error.message : "The request could not be completed.";
}
export function documentStatus(doc: Payload) {
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
  if (["failed", "cancelled"].includes(jobStatus) && !["needs_ocr", "needs_review"].includes(versionState))
    return jobStatus;
  return versionState;
}
export function documentVersion(doc: Payload) {
  return (
    doc.version_id ??
    doc.latest_version_id ??
    doc.latest_version?.id ??
    doc.active_version_id ??
    doc.active_version?.id
  );
}
export function formatMetric(value: unknown) {
  if (value === null || value === undefined) return "Not measured";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number")
    return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  if (isRecord(value)) {
    if (String(value.status || "").startsWith("not_measured")) return "Not measured";
    if (value.status === "incomplete_review" || (typeof value.pending === "number" && value.pending > 0))
      return `Awaiting review${Number.isFinite(value.pending) ? ` · ${value.pending} pending` : ""}`;
    if (value.numerator !== undefined && value.denominator !== undefined) {
      const rate = value.rate ?? value.value;
      let text = `${value.numerator} / ${value.denominator}${typeof rate === "number" && Number.isFinite(rate) ? ` (${(rate * 100).toFixed(1)}%)` : ""}`;
      if (
        Array.isArray(value.ci95_wilson) &&
        value.ci95_wilson.length === 2 &&
        typeof value.ci95_wilson[0] === "number" &&
        typeof value.ci95_wilson[1] === "number"
      )
        text += `\n95% CI: ${(value.ci95_wilson[0] * 100).toFixed(1)}–${(value.ci95_wilson[1] * 100).toFixed(1)}%`;
      return text;
    }
    if (value.responses !== undefined && value.reviewed !== undefined)
      return `${value.reviewed} / ${value.responses} reviewed`;
    if (value.value !== undefined && typeof value.value !== "object") return formatMetric(value.value);
    return JSON.stringify(value);
  }
  return String(value);
}
