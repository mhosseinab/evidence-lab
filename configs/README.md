# Evidence Lab configuration

Run commands from the repository root. See the root [configuration tables](../README.md#configuration-reference) for every field, default and environment variable.

| File | Purpose |
|---|---|
| [mock.yaml](mock.yaml) | Local deterministic fixtures; no external inference |
| [mock.compose.yaml](mock.compose.yaml) | Same fixture setup with Compose database host `db` |
| [live.example.yaml](live.example.yaml) | Generic live-provider template; complete endpoints, models, limits and pricing |
| [clef.example.yaml](clef.example.yaml) | Complete OpenAI embeddings/generation + native Cloudflare Clef verifier sample; supply credentials/account ID |

## Activate Cloudflare Clef

```bash
cp configs/clef.example.yaml configs/private.yaml
```

Edit the private copy:

| Setting | Action |
|---|---|
| Both OpenAI `api_key` values | Replace `REPLACE_OPENAI_API_KEY` with your OpenAI API key |
| Clef `endpoint` | Replace `REPLACE_ACCOUNT_ID` with your Cloudflare account ID |
| Clef `api_key` | Replace `REPLACE_CLOUDFLARE_API_TOKEN` with a Workers AI API token |
| `database.dsn` | Select your PostgreSQL/pgvector database; use hostname `db` inside Compose |
| `budgets` | Review the $5 total cap and positive phase caps; set unwanted phases to zero |
| `pricing.*.checked_on` | Recheck prices and update the date/rates before spending |

The sample already sets `runtime.mode: live`, `runtime.require_openai_compatible: false`, `roles.verifier: clef_full` and `verification.mode: shadow`. Its 1536-dimensional live embedding space requires a new corpus when switching from mock data. No public answer is released in shadow mode; inspect checked drafts in the operator trace. Gated release requires a matching qualified policy.

```bash
# Validate without inference; placeholders deliberately fail until replaced.
task app:cli CONFIG=configs/private.yaml -- config-check
# Prepare PostgreSQL first; task dev does not bootstrap custom configurations.
task app:migrate CONFIG=configs/private.yaml
# Dry run, then optional budgeted endpoint calls:
task app:cli CONFIG=configs/private.yaml -- smoke
task app:cli CONFIG=configs/private.yaml -- smoke --execute
# Start API, worker and Vue hot reload:
task dev CONFIG=configs/private.yaml
```

Open `http://127.0.0.1:5173/static/`, create a new corpus, then upload documents and ask questions. Processing uploads/questions makes live calls within the configured budgets. Evaluation execution also requires a real CLI-selected domain dataset; the HTTP demo remains mock-only.

For Compose, change the private DSN hostname to `db`, ensure its YAML is readable by container UID `10001`, and run:

```bash
RAG_CONFIG=./configs/private.yaml task compose:up
```

Open the compiled dashboard at `http://127.0.0.1:8000/`. Restart API/worker after changing configuration. Avoid port conflicts with another running stack.

### Provider contracts and prices

| Role | Sample provider | Contract / rates per million tokens |
|---|---|---|
| Embeddings | [OpenAI text-embedding-3-small](https://developers.openai.com/api/docs/models/text-embedding-3-small) | `/v1/embeddings`, 1536 dimensions; $0.02 input |
| Generation/repair | [OpenAI GPT-4.1 mini snapshot](https://developers.openai.com/api/docs/models/gpt-4.1-mini) | `/v1/chat/completions`, structured JSON; $0.40 input / $1.60 output |
| Verification | [Cloudflare Clef](https://developers.cloudflare.com/workers-ai/models/clef/) | Native `/ai/run/@cf/cloudflare/clef`, body model `clef`; $0.24 input |

Rates were checked on 6 October 2026. The sample uses conservative application input allowances. See [Workers AI authentication](https://developers.cloudflare.com/workers-ai/get-started/rest-api/) for token setup. Configuration/adapter checks do not establish live endpoint availability or model quality; run the budgeted smoke check with your own accounts.

## Credentials and spending

Choose exactly one credential source per active profile: direct `api_key`, `api_key_env`, or `api_key_file`. Relative secret files resolve from the YAML directory. Compose must explicitly mount secret files or forward selected environment variables. Private YAML and `configs/secrets/` are ignored by Git; example files must contain placeholders only.

Live calls require positive total and matching phase caps. Every attempt reserves cost, attempts and concurrency in PostgreSQL before transport. Unknown usage retains conservative reservations; the ledger is not a provider invoice. Mock mode never resolves credentials or makes model calls.

## Decoding and verification

Generation and evaluation share strict JSON decoding and answer-size limits. A format failure may retry once within the two-attempt ceiling; interactive draft-schema failure is terminal. Evaluation retains the shared initial candidate for A–D.

Clef uses native choice distributions for every answer block and three global checks. The application validates coverage and binds verdicts to answer/evidence hashes and round IDs. A semantic rejection may trigger one complete repair against frozen evidence. Malformed responses, timeouts and budget exhaustion remain technical failures. Probability thresholds require a frozen reviewed policy; no automatic fallback or model download is provided.
