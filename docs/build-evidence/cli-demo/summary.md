# Evaluation report

Study: `0bd573a7-8582-4fa4-ac0c-5f4ebc029697`. Dataset hash: `ccd32b8428881d30ec30f07c6fced73a99e35907941ef90f1770a4f3ae2e2cc6`.

Execution: **completed**. Runtime: **mock**. Quality qualification: **unqualified**.

This report contains experimental diagnostics. A/B answers and rejected drafts are not verified user answers.

**Synthetic/mock results check software behavior only. They do not measure real model quality.**

## Paired answer metrics

Each case shares its initial evidence and decoded draft across A/B/C/D. Only D can perform one repair.

| Variant | Correct and complete | Missing-evidence handling | Conflict handling | Operational completion |
| --- | --- | --- | --- | --- |
| A | 0/4; incomplete_review; pending 4 | 0/2; incomplete_review; pending 2 | 0/1; incomplete_review; pending 1 | 7/7 (100.0%); 95% Wilson [64.6%, 100.0%] |
| B | 0/4; incomplete_review; pending 4 | 0/2; incomplete_review; pending 2 | 0/1; incomplete_review; pending 1 | 7/7 (100.0%); 95% Wilson [64.6%, 100.0%] |
| C | 0/4; incomplete_review; pending 3 | 0/2; incomplete_review; pending 2 | 0/1; incomplete_review; pending 1 | 7/7 (100.0%); 95% Wilson [64.6%, 100.0%] |
| D | 0/4; incomplete_review; pending 4 | 0/2; incomplete_review; pending 2 | 0/1; incomplete_review; pending 1 | 7/7 (100.0%); 95% Wilson [64.6%, 100.0%] |

## Retrieval and controlled claims

All-required-evidence coverage at eight: 0/4; not_measured_inline_fixture; pending 4.

Controlled false acceptance: 0/8 (0.0%); 95% Wilson [0.0%, 32.4%].

Controlled supported retention: 8/8 (100.0%); 95% Wilson [67.6%, 100.0%].

Controlled claims are verified as originally supplied; repair is disabled. Failed attempts remain in their declared denominators.

Wilson intervals assume independent cases. The JSON also reports paired differences using source-family bootstrap; a small number of families and zero observed events limit what those intervals establish.

## Unmet qualification requirements

- **live_mode**: Mock results are deterministic plumbing fixtures.
- **real_evaluation_dataset**: Synthetic demo labels are not independent human-reviewed gold.
- **frozen_primary_profile**: Freeze and preselect the verifier profile on development data before held-out execution.
- **frozen_before_execution**: The held-out dataset and selected policy must be frozen before the first study call.
- **frozen_corpus_snapshots**: Every question for a corpus must observe the same frozen embedding space and active-source revision.
- **held_out_sample**: Require exactly 140 answerable, 40 missing-evidence, and 20 conflict questions from 20 held-out families.
- **multi_evidence_cases**: At least 40 held-out answerable cases need a multi-evidence label and at least two required facts with distinct gold spans.
- **controlled_sample**: Require a one-to-one match of 200 supported originals and 200 reviewed unsupported mutations, 25 per error category.
- **question_and_controlled_gold_reviewed**: Two independent reviewers must adjudicate question/evidence gold and controlled labels.
- **all_outputs_independently_reviewed**: Independently review all distinct initial, repaired, and final answers, blind to verdict and variant.
- **retrieval_coverage**: At least 126/140 cases need all required evidence in the packed top eight.
- **controlled_false_acceptance**: At most 10/200 unsupported original challenge claims accepted; no repair.
- **controlled_retention**: At least 180/200 supported originals accepted.
- **correct_and_complete**: D needs at least 105/140 correct-complete answers, at most seven fewer than A.
- **missing_evidence_handling**: Require at least 38/40 appropriate final responses.
- **conflict_handling**: Require at least 19/20 appropriate final responses.
- **release_audit**: Audit every final response, including conflict and abstention text; zero observed material unsupported responses.
- **live_completion**: At least 198/200 complete with an answer or valid abstention; technical failures stay in the denominator.
- **mandatory_fault_suite**: A reviewed, policy-bound mandatory fault-suite artifact is not supplied by this evaluation runner.
- **live_concurrent_latency**: Paired replay does not establish live p95 at four concurrent requests; a separate live load result is required.
- **repeatability**: A predeclared twenty-question, three-repetition live artifact is required.

## Unmeasured

- four-concurrent-request live SLO
- predeclared held-out repeatability study
- mandatory fault-suite qualification artifact
- real model quality
- natural error prevalence
- retrieval quality for inline fixture questions
