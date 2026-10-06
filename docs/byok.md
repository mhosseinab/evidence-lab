# Dashboard BYOK and operator sessions

Use **Workspace connection** from the runtime badge or sidebar to choose fixture
or live mode. Files are shared with everyone who has workspace access; BYOK
selects inference credentials and does not create a private document account.
The dashboard keeps this shared-workspace notice visible on every screen.

## Configure live mode

1. Enter the **LLM provider key** for OpenAI-compatible chat and any external
   embeddings, and the **Cloudflare key** for the native Clef verifier.
2. In **Live endpoint setup**, enter the full chat operation URL, model ID and
   Cloudflare account ID. The application does not append endpoint paths.
3. Choose an embedding source:
   - **Workspace embeddings:** retain the base embedding profile and compatible
     PostgreSQL/pgvector vectors. Queries still need matching query embeddings;
     stored vectors alone cannot embed a new question. Fixture embeddings remain
     deterministic and visibly labeled, even with live chat/Clef.
   - **Custom embedding endpoint:** supply its complete URL, model, dimensions,
     input limit and input price. A changed embedding profile requires a new
     corpus and re-upload; existing vectors cannot be mixed with a new space.
4. Set chat input/output limits, supported JSON output format, output token
   parameter, input/output prices and a spending budget. Fields marked `*` are
   required. Dimensions, model limits, output-limit parameter and provider prices
   start unset; use values supported by the selected provider/model. Plain JSON
   text is the initial output format. Zero budget blocks live calls; a free
   endpoint may have an explicitly entered zero price.
5. Save setup and select **Live mode**. Saving keys/setup or switching mode does
   not call models. Mode changes clear the selected answer and conversation.

The built-in Clef profile currently declares a 65,536-token context and $0.24 per
million input tokens in `runtime_settings.py`; these are application defaults,
not automatic price discovery. Verify provider contracts and prices before live
use. Workspace embeddings reuse their configured limits and pricing. If that
profile is external, it uses the browser LLM key after stripping server credential
references; mixed browser/server provider credentials are not supported.

Browser live setup uses **shadow verification**. Checked candidates remain in
operator diagnostics; no unqualified candidate enters the normal answer field.
For qualified gated release, use reviewed server configuration and the existing
[qualification workflow](evaluation.md).

## Keys and recovery

Keys, mode and setup are stored in browser local storage, scoped to the server's
base configuration fingerprint and browser origin. Moving from localhost to a
Pages/custom domain requires entering them again. Use the form to remove keys or
saved setup; switch to fixture mode to edit setup.

Inference requests send keys in `X-Evidence-Lab-Provider-Keys`. The API holds
them in request-scoped configuration for its background task, then discards that
scope. Keys are not saved in YAML, encrypted database records, jobs/results, logs
or traces. Saving a key makes no provider call. Browser storage is readable by
scripts on the application's origin; use HTTPS for remote access. With Pages,
inference headers and uploaded files pass through the Pages Function to the API.

Browser-owned jobs execute in the API process; ordinary workers skip them. An API
restart interrupts these tasks. Resupply browser credentials to retry supported
jobs, re-upload the same document to resume ingestion, or cancel and resubmit a
query. An operator can retry failed ingestion/evaluation using server credentials,
which transfers job ownership to ordinary workers. Unattended recovery requires
server credentials. Provider budgets, attempt caps, leases and verification rules
still apply.

## Sign in as an operator

Use **Sign in** in the header, separately from provider setup. Enter the nonempty
`runtime.operator_token` configured privately on the server. A successful sign-in
adopts server mode, provider keys, models, limits and budget; browser overrides
are hidden and omitted. The token stays in page memory. **Sign out** clears it
and restores browser preferences where API access permits them.

| Server configuration | Access behavior |
|---|---|
| No operator token | Local/shared REST access remains open; browser BYOK may use its own keys. Server-funded API inference and MCP are denied. |
| Nonempty operator token | Every `/api/` request requires that bearer token, including browser BYOK requests. Health and dashboard assets remain public. |
| Blank operator token | Configuration validation fails. |

Sign-in is shared operator authentication, not individual user identity. Token
holders have administrative REST access to the workspace. CLI and ordinary
workers are trusted server processes and use their configured keys independently
of dashboard sign-in. Workspace isolation is tracked in [TODO](todo.md).

See [contracts](contracts.md#browser-credentials-byok) for headers and validation,
[private configuration](../configs/README.md) for server-key setup, and
[CI](ci.md) for engineering check commands.
