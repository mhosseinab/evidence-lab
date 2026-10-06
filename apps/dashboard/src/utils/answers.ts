import type { OperatorReleaseAction, Payload } from "../types/api";
import { pendingStates, statusOf, textValue } from "./presentation";

const releasedStates = new Set([
  "answered",
  "accepted",
  "released",
  "complete",
  "completed",
  "success",
  "succeeded",
  "done",
]);

/** Only public answer fields of a successfully completed run may reach the answer panel. */
export function releasedAnswer(run: Payload | null): { blocks: Payload[]; text: string | null } {
  if (!run || !releasedStates.has(statusOf(run)) || pendingStates.has(statusOf(run)))
    return { blocks: [], text: null };
  const answer = run.answer ?? run.released_answer ?? run.result?.answer;
  const blocks = Array.isArray(answer)
    ? answer
    : typeof answer === "object" && answer !== null && Array.isArray(answer.blocks)
      ? answer.blocks
      : (run.blocks ?? run.answer_blocks ?? []);
  const text =
    typeof answer === "string"
      ? answer
      : typeof answer === "object" &&
          answer !== null &&
          !Array.isArray(answer) &&
          typeof answer.text === "string"
        ? answer.text
        : null;
  return { blocks, text };
}

export function answerState(run: Payload): {
  title: string;
  message: string;
  symbol: string;
  error: boolean;
} {
  const status = statusOf(run);
  const semantic = ["abstained", "abstention", "unsupported", "not_supported"].includes(status);
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
  const fallback = semantic
    ? "Try a more specific question or add a document with the missing evidence."
    : shadow
      ? "Inspect the operator trace to review this development run. No draft has been released as an answer."
      : cancelled
        ? "You can submit the question again when you are ready."
        : "A technical issue interrupted the checks. This does not mean the documents lack the answer.";
  // An abstention message is separate from the public answer fields; never use a draft fallback.
  const message = textValue(run.message, textValue(run.error, textValue(run.abstention, fallback)));
  return {
    title,
    message,
    symbol: semantic || shadow ? "↳" : "!",
    error: !semantic && !cancelled && !shadow,
  };
}

/** Availability only; the server revalidates checks and active sources before publishing. */
export function operatorReleaseAction(run: Payload | null): OperatorReleaseAction | null {
  if (run?.status === "shadow" && run.code === "policy_not_qualified") return "release";
  if (run?.status === "answered" && run.qualification === "operator_approved") return "revoke";
  return null;
}
