# Evaluation, review, and policy qualification

The evaluator produces a paired experiment with explicit denominators and unmeasured fields. It can record a negative or incomplete result. A completed job is not automatically a qualified model policy.

The bundled demo is deliberately small: **four fictional documents, seven questions, and sixteen controlled claims**. Its exact-text mock behavior checks software plumbing. It establishes neither retrieval quality nor the semantic accuracy of any hosted model. No human-reviewed development or held-out benchmark is bundled.

## Run the mock demonstration

From the project root, with the virtual environment installed:

```bash
.venv/bin/python -m rag_poc.evaluation inspect data/demo/dataset.json
.venv/bin/rag-poc --config configs/mock.yaml evaluate --dataset data/demo/dataset.json
```

The second command prints a dry-run estimate. Execution is explicit and uses the configured PostgreSQL database and durable worker job:

```bash
.venv/bin/rag-poc --config configs/mock.yaml migrate
.venv/bin/rag-poc --config configs/mock.yaml evaluate \
  --dataset data/demo/dataset.json --execute --output artifacts/demo-evaluation
```

Exported files are `report.json`, `questions.csv`, `controlled.csv`, and `summary.md`. The JSON contains the complete experiment, including unreleased drafts and baseline A/B text. Treat it as an operator review artifact. Those diagnostic fields must not be presented as the normal verified answer to an interactive user.

The browser evaluation API accepts exactly `{"dataset":"demo"}` in mock mode. It never accepts a server-side path from an HTTP client. Operator CLI evaluation can select a manifest path. The worker has a distinct internal path-based entry point for that explicitly initiated operator work.

The demo's evaluation questions use inline evidence, so retrieval coverage is `not_measured_inline_fixture`. To exercise ordinary ingestion and retrieval separately:

```bash
.venv/bin/rag-poc --config configs/mock.yaml seed --wait
.venv/bin/rag-poc --config configs/mock.yaml query \
  "When can premium members return an unopened item?" --wait
```

## Paired experiment

Each question performs one initial retrieval and one initial generation. These exact inputs are shared by the variants; there is no independent, more favorable sample for one gate.

| Variant | Input and release rule |
| --- | --- |
| A | Decodable generated block text, before the strict draft and citation gate. Used only as an experimental baseline. |
| B | The same candidate must satisfy the draft schema, citation references, and deterministic structural checks. |
| C | B plus one complete semantic verification batch, bound to the exact answer, evidence, and round. |
| D | C plus at most one content repair after a valid semantic rejection. The repaired draft must pass the structural gate and an entirely new verification batch. |

The provider's evaluation-only `generate_candidate` boundary retains malformed IDs, citations, or extra fields after universal JSON decoding. This allows A/B to measure the effect of the schema gate. Universal invalid/missing JSON can use the same bounded format retry in both the candidate and interactive generation paths. Once a candidate is universally decoded, a strict draft-schema violation is nonretryable: A keeps the candidate, while B and the interactive path reject it. This prevents D from being qualified on a different initial-candidate selection rule. Exhausted format retries, refusal, an over-limit response, or unrecoverable answer text are execution failures even for A. A test integration exposing only the typed `generate` method is supported, but its case record explicitly says that the schema effect is unmeasured.

The candidate's text and raw decoded object are review diagnostics. A valid candidate receives the same `Draft.content_hash` used by B/C/D. An invalid candidate still has an exact content hash for its A adjudication, and its independently reviewed material claims remain in natural-claim accounting even though B prevents their acceptance.

For corpus retrieval, dense-only, lexical-only, and fused evidence packs come from one query embedding and one database retrieval snapshot. Each branch uses the same packing budget, overlap deduplication, and maximum eight chunks. Those branches measure retrieval coverage; generation uses the fused pack. A source family cannot be split across development and held-out questions. A qualified study also requires every query for a given corpus to observe the same corpus revision and embedding space.

Repair receives only the original draft and canonical failed-check IDs. It receives no gold facts, expected response type, reviewer annotations, or hidden answer key. The evidence pack stays unchanged. Duplicate failure IDs and threshold suffixes are removed before constructing the bounded repair request. Even an unchanged repaired answer is verified again with `round_id="repair"`.

Missing, foreign, duplicate, stale, or incorrectly bound checks are technical failures. They cannot authorize content repair. C/D return an explicit technical outcome; the earlier A/B diagnostics remain available for the paired analysis. Empty evidence produces an evidence-based abstention without generating a draft.

Controlled cases have a separate path. Each supplied original claim or mutation is verified verbatim in one block with its declared evidence. **No generation or content repair occurs.** A repair that removes a false claim cannot improve the false-acceptance result of that originally supplied claim.

## Prepare a real dataset

Start from `data/templates/operator-dataset.json` and the generated JSON schemas beside it. The scaffold is unfilled, unreviewed, and intentionally far below qualification sample sizes. Replace placeholders with an actual document domain; do not duplicate synthetic fixtures to manufacture the target sample.

The frozen protocol is:

| Component | Required composition |
| --- | --- |
| Development | 100 questions from ten source families |
| Held-out questions | 200 from twenty different source families: 140 answerable, 40 missing evidence, and 20 unresolved conflicts |
| Multiple evidence spans | At least 40 of the 140 answerable test questions |
| Development controlled cases | 100 supported originals and 100 matched unsupported mutations |
| Held-out controlled cases | 200 supported originals and 200 matched unsupported mutations |
| Mutation categories | Exactly 25 unsupported held-out cases each: `negation`, `number_unit`, `entity`, `date`, `condition`, `scope_quantifier`, `unsupported_addition`, `misleading_combination` |

The planned corpus is about ninety documents from thirty source families. The exact number of documents is descriptive; family independence, gold completeness, and the frozen question composition determine whether the study can support the planned comparison.

Keep document revisions, near duplicates, and related templates in one family. The loader rejects identical normalized evidence assigned to different families and inconsistent family/split references. It cannot automatically detect every near duplicate or establish that two templates are substantively independent; the reviewers must assess this.

Each question records its response type, required facts, sufficient evidence alternatives, necessary qualifiers, and prohibited conclusions. An evidence alternative contains a verbatim quote and can constrain document and version IDs. Coverage requires at least one valid alternative for **every** required fact in the packed first eight evidence chunks. A short relevant fragment is insufficient if another material qualification or evidence span is absent.

Conflicts need explicit applicability: versions, dates, scope, and whether one source supersedes another. Two statements with different applicable dates are not automatically an unresolved conflict. Missing-evidence cases should specify what is absent and which plausible inference the answer must avoid.

Controlled claims reference inline evidence spans in the manifest even in an operator study. This freezes their inputs independently of retrieval performance. A mutation must reference a supported original in the same family and split, use exactly the same evidence IDs, and change the text. Review every proposed mutation for natural wording and an actually changed support label.

### Independent review rubric

Two domain-appropriate reviewers label independently, then resolve disagreements with a recorded adjudication. Record stable reviewer IDs, the adjudicator ID, review date, and notes. Reviewer identities in the JSON are operator assertions; this application does not authenticate professional qualifications or prove that two people worked independently.

Question/evidence gold and controlled labels must be adjudicated before the held-out run. Freeze the dataset version and content hash, source versions, extraction/chunking, retrieval settings, provider/model identifiers, prompts, schema, threshold, and repair policy. Set `frozen_at` and the development-selected verifier profile in `preselected_profile` before execution. Never choose the best candidate retrospectively from the held-out results.

A native Clef policy additionally records `preselected_score_threshold` and `development_selection` with `report_id`, `dataset_hash`, and `selected_at`. The selected time must precede the freeze. Its score is a native decision score; threshold selection does not turn it into a calibrated probability. A chat verifier uses labels and requires a null threshold.

After execution, independently review **every distinct initial, repaired, and final answer**, blind to variant and verifier verdict. Reuse a review when the same question and exact answer hash occur in multiple variants. Include fixed abstention and conflict text in the final-response audit.

For each output, record:

- Whether it is substantive, rather than an empty or boilerplate response.
- Whether all material assertions are supported by the applicable supplied evidence.
- Whether it completely answers an answerable question, including required conditions, qualifiers, and all requested parts.
- Whether its final text appropriately handles missing evidence or unresolved conflict.
- Every material claim as a verbatim span, with its support label and block ID where a valid block exists.

Decompose compound claims so one supported clause cannot conceal an unsupported number, condition, entity, or extra assertion. Count paraphrases against their meaning, not word overlap. A claim about a source disagreement must accurately attribute both applicable positions. A grounded excerpt that does not answer the question is not a correct-and-complete answer.

Set `all_material_facts_reviewed=true` only when the reviewer has audited the whole output. A fully reviewed substantive answer requires material-claim annotations. An answer marked correct and complete must also be substantive and fully supported. Annotation text must be an exact span in the bound answer, and hashes must match an actual output of the supplied frozen report. These checks protect accounting; they cannot independently validate the human semantic judgment.

Generate an unfilled, blinded packet:

```bash
.venv/bin/python -m rag_poc.evaluation annotation-template \
  artifacts/demo-evaluation/report.json --output artifacts/answer-review-template.json
```

Null labels in this packet are deliberate. It is not valid completed gold and cannot silently score as perfect. Fill actual reviews and adjudicate them before applying:

```bash
.venv/bin/rag-poc --config configs/mock.yaml eval-review \
  artifacts/demo-evaluation/report.json \
  --annotations artifacts/answer-reviews.json --output artifacts/reviewed-evaluation
```

Offline scoring does not call models. A single reviewer yields preliminary measurements and cannot satisfy the independent-review qualification gate. A hosted judge can assist triage, but its labels are not human gold.

### Propose one controlled mutation

```bash
.venv/bin/python -m rag_poc.evaluation propose-mutation \
  data/demo/dataset.json s-number_unit \
  --old '€5' --new '€50' --category number_unit --id proposed-postage-mutation \
  --output artifacts/proposed-mutation.json
```

This literal helper requires exactly one matching span. It writes `supported:null` and `review.status:"unreviewed"`. A person must decide whether the wording is natural and whether the support label actually changed. The command does not append the proposal to the frozen dataset or assign invented reviewer identities.

## Metric definitions and uncertainty

Every report includes the selected frozen manifest. Scoring rejects missing or duplicated case rows, altered family/type/controlled-label metadata, a changed manifest hash, or an incomplete A/B/C/D set. Each declared case has an output row even if budget, cancellation, or restart prevents execution.

| Metric | Denominator and treatment of failure |
| --- | --- |
| All-required-evidence coverage at eight | All answerable questions. Retrieval failures count as uncovered. Missing gold spans remain unmeasured. Inline demo packs do not measure retrieval. |
| Controlled false acceptance | All originally supplied unsupported controlled claims. A technical failure cannot be silently removed; it is an unaccepted claim and is also counted in operational failure reporting. |
| Controlled supported retention | All supported controlled originals. Technical failures and rejection count against retention. |
| Natural claim false acceptance/retention | Human-reviewed material claims in initial and repaired outputs, reported separately. Acceptance means the associated valid block check passed, not that the whole answer was released. Incomplete review makes the headline rate and interval unavailable. |
| Correct and complete | All 140 answerable held-out questions for each variant, including abstentions and technical failures. Those outcomes score zero. Unreviewed released outputs remain pending, not automatically wrong or correct. |
| Missing-evidence/conflict handling | All forty missing-evidence or twenty conflict cases. Success depends on reviewed final text, not merely an abstention status. |
| Unsupported release rate | All released answers for that variant. Every release must be reviewed before claiming a measured rate. |
| Operational completion | All attempted/declaration rows, including failures and cancellations. It reports successful answer/valid-abstention completion separately from content quality. |

Proportions include numerator, denominator, pending count, and a two-sided Wilson 95% interval. No observations produces null, never a zero-width certainty interval. Pending annotation suppresses the headline estimate and interval while retaining known counts.

Paired A→B, B→C, and C→D correct-completeness differences use a deterministic-seed percentile bootstrap that resamples whole source families and keeps each paired case together. The controlled table also reports the matched original-to-mutation acceptance difference using the same family bootstrap; it measures challenge sensitivity, not a provider comparison or natural error prevalence. Fewer than two families has no family interval. Few families remain a substantive limitation even when the bootstrap returns a numerical interval.

For zero observed unsupported releases, the report gives the one-sided exact binomial 95% upper bound only after all releases have been reviewed. With 100 independent observations, zero events gives an upper bound of about 2.95%, not a guarantee of zero future risk. Source-family correlation weakens that independence interpretation; a zero-event bootstrap does not eliminate uncertainty.

Paired execution shares generation and verification work. Its timing rows are diagnostic and do not claim independently measured live latency for A, B, C, and D. A separate real API/worker load artifact must establish queue-inclusive latency at four concurrent requests. Report cold/warm and repair/no-repair cohorts, p50/p95, and their counts. Small cohorts do not establish a precise tail estimate.

## Budgets, failure, and restart

All inference goes through the configured provider hub. The evaluator does not host or download models. Before live calls, the operator needs valid external profiles, known dated pricing, and positive total and evaluation phase budgets. Dry-run estimates use profile input/output ceilings, retries, an optional repair for every question, controlled verification calls, and query embeddings. They are conservative planning bounds and exclude ingestion, the separate repeatability experiment, and API load campaigns.

Each case shares one configured deadline and one attempt counter across retrieval, generation, verification, and repair. Defaults are sixty seconds and ten remote attempts. The persistent provider ledger enforces costs and concurrency. The evaluation runner executes cases sequentially so its study attempt cap cannot race across cases. The default study cap is 3,000 attempts. Failed requests, retries, and unknown usage remain accounted for; unknown actual usage retains its conservative reservation.

Cost metrics separate query work from controlled verification. They report conservatively accounted USD per declared question, per actually initiated question, and per D correct-and-complete answer once those labels are known. Provider token-field aliases remain separate, so `input_tokens` and `prompt_tokens` are not accidentally added twice. Missing usage or a mismatched ledger makes the relevant measurement unknown instead of free.

Budget exhaustion or cancellation stops further cases and emits explicit unattempted rows for the remaining manifest. The report is incomplete and cannot qualify. An expired/reclaimed evaluation job has no per-case checkpoint replay facility: `attempts > 1` terminates as `interrupted_evaluation` without silently repeating paid work. The operator can inspect the old job and ledger, then explicitly initiate a new study if appropriate. Normal job leases still fence final publication.

## Qualification is an evidence check

The paired report alone remains unqualified. `qualify_policy` recomputes metrics from the raw report and retained, hash-bound human adjudications, then combines actual qualification artifacts. It never treats a convenient cached summary as the source of truth.

Required empirical gates include:

- Exactly 200 held-out questions in the 140/40/20 composition, twenty source families, and at least forty multi-evidence answerable questions.
- Exactly 200 supported controlled originals and 200 reviewed unsupported mutations with the eight 25-case categories.
- Independently adjudicated question/control gold and every distinct output.
- At least 126/140 answerable questions with all required retrieved evidence.
- No more than 10/200 unsupported controlled claims accepted, and at least 180/200 supported originals retained.
- D has at least 105/140 correct-and-complete answers and no more than seven fewer than A.
- Appropriate final text for at least 38/40 missing-evidence and 19/20 conflict cases.
- At least 100 reviewed substantive releases, an audit of all 200 D final responses, and zero observed material unsupported final responses.
- At least 198/200 D attempts ending in an answer or valid abstention; technical failures remain failures.

Three additional artifacts must exist, match the evaluation ID, dataset hash, semantic policy fingerprint and implementation fingerprint, and report complete execution:

| Artifact | Required evidence |
| --- | --- |
| `fault_suite` | All sixteen named fault areas pass, named pytest node IDs, no failures/errors/skips, actual native PostgreSQL verification and restore verification, migration/PostgreSQL/pgvector versions. Offline protocol fixtures are valid fault tests; they do not establish semantic model accuracy. |
| `live_load` | Actual external endpoint work through the API/worker at four concurrent requests, unique durable run IDs, queue-inclusive timings, all cold/warm × repair/no-repair strata, p95 at most twenty seconds without repair and forty with repair, sixty-second and ten-attempt constraints. Failed rows remain present and prevent a passing load artifact. |
| `repeatability` | `measurement:"verifier_repeatability"`, twenty predeclared held-out question IDs × three attempts, each using the exact frozen initial draft and evidence from the evaluation report. Report decision disagreement and failures; there is no retrospectively chosen disagreement threshold. |

Prequalification API load may run in shadow/evaluation mode: the full generation, verification, and repair pipeline executes while candidate answers remain unpublished. The importer accepts terminal `shadow` only when that mode is explicitly recorded. The semantic policy fingerprint excludes publication mode, avoiding a circular requirement for a qualified policy before measuring the candidate pipeline.

Repeatability varies verifier execution, not retrieval or generation. The repeated `answer_hash` therefore identifies the pinned input draft; agreement on that input hash is a binding check, not evidence that generation is deterministic. A planned case with missing frozen input stays an explicit failure and cannot be replaced with an easier case.

The artifact import verifies structure, counts, temporal/sample bindings, and hashes. Artifacts are trusted operator records, not cryptographic attestations of human identity or remote execution. Altering or fabricating their provenance invalidates the experiment even if a JSON file can be made to parse.

Use the actual artifact paths printed by the verification and experiment commands; the fault runner returns an `artifact_path` named `fault-suite-<UUID>.json`. Replace the placeholder below. See `docs/experiments.md` for preparing and running the predeclared live campaigns.

```bash
.venv/bin/rag-poc --config configs/private.yaml qualify \
  artifacts/reviewed-evaluation/report.json \
  --fault-artifact artifacts/verification/fault-suite-REPLACE_WITH_ARTIFACT_ID.json \
  --load-artifact artifacts/load/combined-load.json \
  --repeatability-artifact artifacts/repeatability/report.json \
  --policy-id reviewed-candidate-v1 --output policies/reviewed-candidate-v1.json
```

Only an all-pass result has `qualified:true`. Missing inputs, mock runs, incomplete gold, source/policy changes, or failed goals produce an explicit list of unmet requirements. Configure the matching policy ID and path only after inspecting that concrete result. Qualification applies to the frozen measured PoC; source truth and production readiness are separate questions.

## Tests and module interfaces

```bash
.venv/bin/pytest -q tests/test_evaluation.py
.venv/bin/ruff check src/rag_poc/evaluation.py tests/test_evaluation.py
```

The tests use explicitly named unit-test stores and scripted or mock provider responses. Production uses PostgreSQL. Tests cover paired input sharing, schema versus semantic gates, stale/malformed verification, repair feedback, controlled immutability, missing review, confidence intervals, family grouping, dataset leakage checks, fixed failure denominators, budget/cancel/restart behavior, API path restrictions, and artifact binding.

Stable Python entry points are `load_dataset`, `dry_run_estimate`, `evaluate_dataset`, `run_evaluation`, `annotation_template`, `load_answer_reviews`, `regrade_report`, `apply_annotations`, `export_report`, and `qualify_policy`. The worker owns durable job leases and result publication; these functions return JSON-safe reports and never publish interactive answers themselves.
