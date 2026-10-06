# Repeatability and API load experiments

These commands produce separate, predeclared measurements for verifier repeatability and real API/worker latency. They do not qualify a policy. The paired A/B/C/D evaluation and human review remain in [evaluation.md](evaluation.md).

Use reviewed server configuration and operator authentication for unattended live
load runs. Dashboard BYOK jobs require browser credentials to be resupplied after
an API restart; they are not ordinary worker recovery jobs. MCP evidence retrieval
does not run the verified-answer workflow and is outside these answer-load studies.
CI uses deterministic engineering fixtures, not live model-performance measurement.

Run commands from the repository root after `task setup`. The examples use the application task and its forwarded experiment CLI:

```bash
task app:cli -- experiments -- --help
```

`CONFIG=PATH` selects the application configuration; optional `--verifier-profile NAME` comes **before** the experiment subcommand, after the forwarding separator. For example:

```bash
task app:cli CONFIG=configs/private.yaml -- experiments -- --help
```

Task’s first `--` forwards arguments to the application; the second `--`, after `experiments`, forwards options to the experiment parser. For example, put `--verifier-profile NAME` after that separator and before `prepare-repeatability` or `run-repeatability`.

| Command | Work performed |
| --- | --- |
| `prepare-repeatability` | Read the frozen dataset/report, freeze selected initial drafts and evidence, and create a plan. No model calls. |
| `prepare-load` | Read the dataset and current PostgreSQL corpus identities, then create a load plan. No inference or query submissions. |
| `run-repeatability` | Validate the frozen plan and print an estimate. Verifier calls require `--execute`. |
| `run-load` | Validate the frozen plan and print an estimate. HTTP query submissions require `--execute`. |
| `merge-load` | Combine compatible saved load artifacts offline. No API or model calls. |

Plans and execution artifacts use exclusive creation: an existing output file is refused. Executed runs require a new explicit `--output` path. Use a new descriptive path for each declared campaign; keep prior plans and results. An output path is not a spending reservation, and an estimate is not a bill.

## Freeze inputs and budgets first

For real experiments, use an operator evaluation dataset with the held-out split, the actual evaluation report, and a complete private live configuration. `configs/private.yaml`, `data/operator-dataset.json`, report paths and uppercase IDs below are placeholders. Replace them with existing files and IDs before running anything.

The plan binds the dataset hash, configuration hash, semantic policy fingerprint, implementation fingerprint, selected verifier/model, publication mode and predeclaration time. Load plans also bind active corpus versions, embedding spaces and revisions. Changing one of these inputs after preparation causes rejection. Set the intended budgets and profile selection before creating plans.

| Experiment | Persistent budget phase | Profiles used |
| --- | --- | --- |
| Verifier repeatability | `evaluation` | Selected verifier only |
| API/worker load | `queries` | Embeddings, generation, verification and the configured repair generator |

Live execution requires positive total and matching phase budgets plus dated pricing for every called profile. Provider requests still enforce their own input, output, retry, timeout and concurrency limits. Failures, retries and unknown usage stay in the persistent ledger. A live run cannot use the zero-cost mock setting as evidence of real performance.

Keep the API, worker and experiment runner on the same effective configuration. Selecting `--verifier-profile` changes that configuration; for a load experiment, the running server must already use the same role selection. A native Clef override also requires the private configuration's explicit `require_openai_compatible: false`; the option does not bypass that rule.

## Verifier repeatability

The live protocol is **exactly twenty distinct held-out question IDs, repeated three times per selected verifier profile**. The original initial draft and evidence pack are copied from the source evaluation report and checked against their hashes. The same IDs and initial inputs should be used for each predeclared candidate comparison.

Execution calls only the verifier. It performs no retrieval, embedding, generation, content repair or input repacking. It does not retune prompts, thresholds or provider settings. Agreement on the input answer hash confirms that the input was held fixed; it does not measure generation determinism.

A selected case whose source report lacks a complete initial draft/evidence pack remains a planned failure. It is not replaced by another question. Prepare the twenty-ID sample before observing repeatability results and retain disagreements and technical failures.

### Prepare the twenty-question plan

The following is Bash syntax. Every `HELDOUT_...` value is a placeholder for an actual ID in your frozen dataset. It lists all twenty arguments without an omitted middle section:

```bash
repeat_question_args=(
  --question-id HELDOUT_001 --question-id HELDOUT_002
  --question-id HELDOUT_003 --question-id HELDOUT_004
  --question-id HELDOUT_005 --question-id HELDOUT_006
  --question-id HELDOUT_007 --question-id HELDOUT_008
  --question-id HELDOUT_009 --question-id HELDOUT_010
  --question-id HELDOUT_011 --question-id HELDOUT_012
  --question-id HELDOUT_013 --question-id HELDOUT_014
  --question-id HELDOUT_015 --question-id HELDOUT_016
  --question-id HELDOUT_017 --question-id HELDOUT_018
  --question-id HELDOUT_019 --question-id HELDOUT_020
)

task app:cli CONFIG=configs/private.yaml -- experiments -- \
  prepare-repeatability \
  data/operator-dataset.json artifacts/reviewed-evaluation/report.json \
  "${repeat_question_args[@]}" \
  --output artifacts/repeatability/selected-verifier.plan.json
```

To select another already configured candidate, insert `--verifier-profile CANDIDATE_PROFILE_NAME` after `experiments --` and before the experiment subcommand. Use that same selection when executing its plan, and choose separate output filenames. Do not change generation or evidence to make the candidate's inputs easier to fit.

### Inspect the estimate, then execute explicitly

```bash
task app:cli CONFIG=configs/private.yaml -- experiments -- \
  run-repeatability data/operator-dataset.json \
  artifacts/repeatability/selected-verifier.plan.json
```

This validates the plan and prints the sixty logical verifier requests, allowed attempt ceiling, preflight failures and price estimate. Each request can contain multiple block/global checks. Retries can make the number of actual outbound attempts larger than sixty. The study cap still applies.

The next command makes the configured verifier calls:

```bash
task app:cli CONFIG=configs/private.yaml -- experiments -- \
  run-repeatability data/operator-dataset.json \
  artifacts/repeatability/selected-verifier.plan.json \
  --execute --output artifacts/repeatability/selected-verifier.report.json
```

Repetitions run sequentially. Results retain each question/repetition, exact input hashes, check labels, decision, latency, attempts and safe call metadata. The summary separates final decision disagreements from per-check disagreements. Technical failures do not count as agreement, and incomplete input or exhausted budgets make the artifact incomplete. No post-hoc disagreement threshold is selected.

A repeatability plan with recorded attempts is refused on a later execution. An interrupted plan cannot silently repeat paid work under the same identity. Inspect the stored ledger and incomplete result before deciding whether a new explicitly declared study is warranted.

## Real API and worker load

Load execution submits ordinary HTTP queries to the running application and polls durable run IDs. It measures **four concurrent query journeys**, with at least four distinct predeclared held-out questions. It uses real worker retrieval, generation, verification and any permitted content repair.

Start the API and worker against the same private configuration in separate terminals:

```bash
task app:serve CONFIG=configs/private.yaml
```

```bash
task app:worker CONFIG=configs/private.yaml
```

Use `verification.mode: shadow` during prequalification. A terminal `shadow` result means the candidate pipeline completed without releasing the answer. It counts as pipeline completion for latency measurement, while the artifact remains unqualified. The operator token, if configured, is read from the configuration and sent as the authorization header; it is not an extra command-line secret.

### Declare cold and warm procedures

The tool records a cache label and the operator's actual preparation procedure. It does not flush caches, restart services, warm providers or independently verify that the chosen label is true.

Define which layers the label covers: application/worker processes, PostgreSQL data/index pages, and any hosted-provider cache behavior you can observe. A worker restart alone does not prove a cold database or cold provider cache. Perform the stated procedure on the dedicated test deployment and record uncontrolled layers. `prepare-load` and server identity checks read database metadata; account for that in the procedure.

For a warm campaign, describe the exact unmeasured warmup and when it finished. Any live warmup requests have their own normal cost and attempt accounting. Warmup is not performed by this command and must not be relabeled as a measured sample after seeing its result.

The placeholders in the following examples must be replaced with the real evaluation ID, real held-out IDs and a truthful description of the preparation already performed:

```bash
load_question_args=(
  --question-id HELDOUT_001 --question-id HELDOUT_002
  --question-id HELDOUT_003 --question-id HELDOUT_004
)

task app:cli CONFIG=configs/private.yaml -- experiments -- \
  prepare-load data/operator-dataset.json \
  --evaluation-id EVALUATION_ID_FROM_REVIEWED_REPORT \
  "${load_question_args[@]}" \
  --cache-state cold \
  --cache-procedure 'REPLACE with the actual cold-cache preparation and uncontrolled layers' \
  --output artifacts/load/cold.plan.json

task app:cli CONFIG=configs/private.yaml -- experiments -- \
  run-load data/operator-dataset.json artifacts/load/cold.plan.json \
  --base-url http://127.0.0.1:8000
```

Four questions are the minimum scheduling example, not a precise p95 sample. Predeclare an appropriate larger sample for the intended latency claim, using additional repeated `--question-id` arguments. The complete query attempt ceiling must fit `evaluation.max_remote_attempts`; actual model costs remain charged to the `queries` phase.

After inspecting the estimate, execute the campaign:

```bash
task app:cli CONFIG=configs/private.yaml -- experiments -- \
  run-load data/operator-dataset.json artifacts/load/cold.plan.json \
  --base-url http://127.0.0.1:8000 \
  --execute --output artifacts/load/cold.report.json
```

Prepare the warm plan after performing the predeclared warmup, while retaining the same dataset, configuration, selected profile and implementation:

```bash
task app:cli CONFIG=configs/private.yaml -- experiments -- \
  prepare-load data/operator-dataset.json \
  --evaluation-id EVALUATION_ID_FROM_REVIEWED_REPORT \
  "${load_question_args[@]}" \
  --cache-state warm \
  --cache-procedure 'REPLACE with the actual warmup, its completion time and uncontrolled layers' \
  --output artifacts/load/warm.plan.json

task app:cli CONFIG=configs/private.yaml -- experiments -- \
  run-load data/operator-dataset.json artifacts/load/warm.plan.json \
  --base-url http://127.0.0.1:8000

task app:cli CONFIG=configs/private.yaml -- experiments -- \
  run-load data/operator-dataset.json artifacts/load/warm.plan.json \
  --base-url http://127.0.0.1:8000 \
  --execute --output artifacts/load/warm.report.json
```

The client checks the server configuration and active corpus identities before and after the campaign. Successful rows must match the planned corpus revision and embedding space. Ordinary source updates during the campaign invalidate that fixed-corpus measurement.

### Timing, failure and repair accounting

Client timing uses a monotonic clock from HTTP submission until a terminal run is observed. It includes server queueing and polling delay, and excludes the later diagnostic trace fetch. The report also retains the server's queue and total execution timing. Reported `total_ms` is the conservative maximum of the client-observed duration and server queue-inclusive duration; both original measurements remain available.

Every planned question retains a row, including failed submissions, timeouts, cancellation, missing diagnostics and budget failures. Attempts come from the run's persistent call ledger. Unknown counts/timings remain unknown. A failed POST is not automatically retried, because the server may already have accepted it. When a known unfinished job times out, the client attempts cancellation and records whether that request succeeded.

The report identifies whether content repair was attempted from actual run timings/events. It does not force an unsupported draft to create a favorable cohort or treat every repaired attempt as a successful repair. Technical failures stay in the total denominator even when their repair stratum cannot be determined.

## Merge load campaigns

Merge the saved cold and warm artifacts without new inference:

```bash
task app:cli CONFIG=configs/private.yaml -- experiments -- \
  merge-load artifacts/load/cold.report.json artifacts/load/warm.report.json \
  --output artifacts/load/combined-load.json
```

The merger requires matching frozen evaluation, dataset, configuration, policy, implementation, verifier, runtime and publication identities. A durable run ID cannot count twice. Different candidate profiles require separate combined reports. Source artifacts and their plan/content hashes remain referenced.

The summary reports four strata:

| Stratum | Proposed p95 target |
| --- | --- |
| Cold, no repair attempted | At most 20 seconds |
| Cold, repair attempted | At most 40 seconds |
| Warm, no repair attempted | At most 20 seconds |
| Warm, repair attempted | At most 40 seconds |

Each stratum includes its count, failures and timing distribution. Percentiles use linear interpolation between sorted measurements at index `(n - 1) × p`, with `p=0.5` for p50 and `p=0.95` for p95. Empty or missing-timing strata establish no latency target. A merge does not manufacture missing repair cases, discard failed rows or make a small cohort a precise tail estimate. An artifact can record complete campaign execution without meeting every qualification criterion.

## Fixture walkthrough and qualification boundary

For a mock repeatability walkthrough, first create the demo evaluation report as described in [evaluation.md](evaluation.md). Then use real bundled IDs and explicitly label the plan as fixture-only:

```bash
task app:cli -- experiments -- \
  prepare-repeatability data/demo/dataset.json artifacts/demo-evaluation/report.json \
  --question-id q-return-window --question-id q-repair \
  --fixture-only --output artifacts/demo-repeatability.plan.json

task app:cli -- experiments -- \
  run-repeatability data/demo/dataset.json artifacts/demo-repeatability.plan.json

task app:cli -- experiments -- \
  run-repeatability data/demo/dataset.json artifacts/demo-repeatability.plan.json \
  --execute --output artifacts/demo-repeatability.report.json
```

Explicit fixture-only repeatability plans allow fewer than twenty questions so plumbing can be demonstrated. The bundled synthetic dataset runs only in mock mode, uses deterministic fixtures, and every result remains `fixture_only` and `quality_qualified:false`. Mock load still requires a running API/worker and at least four distinct questions; pass `--fixture-only` when preparing its load plan. Inline evidence in the demo evaluation does not automatically populate the application corpus: run `task app:cli -- --config configs/mock.yaml seed --wait` before an API-load walkthrough.

Import real experiment artifacts only through the separate `qualify` command, together with the reviewed paired evaluation and an externally collected fault-study artifact matching the current implementation. No dedicated fault-artifact runner is included; historical artifacts from prior code do not qualify the current implementation. See [evaluation.md](evaluation.md#qualification-is-an-evidence-check) for the full command and required gates. These experiment commands never set a policy to qualified, never retune on held-out results, and never convert mock outcomes into hosted-model evidence.

Execution returns exit code `0` for a complete artifact, `1` for an executed incomplete artifact, and `2` for a configuration/input/output contract error. Preparation and dry-run success return `0`. Preserve incomplete artifacts and inspect their explicit failures; a successful process exit alone does not establish model quality or policy eligibility.
