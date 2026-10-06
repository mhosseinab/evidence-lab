# Agent-facing RAG interface decision

Status: private operator-authenticated MCP interface implemented. Scoped OAuth
delegation remains a separate integration prerequisite for untrusted clients.
Date: 6 October 2026.

## Decision

Expose Evidence Lab through a small, read-only MCP server using Streamable HTTP.
Use the official Python MCP SDK (pinned to 2.3.0) and the existing Python application/retrieval
service. Local and remote clients use the same interface. Add a stdio adapter
only for a concrete client that requires it; it must use the same tools and
authorization policy.

Evidence Lab retrieves and identifies evidence; the consuming agent owns its
reasoning and answer generation. Retrieved text is untrusted source material,
not instructions. Retrieval results are not verified answers.

The existing REST interface remains useful for bespoke integrations. MCP adds
tool discovery and typed input/output interoperability without replacing the
retrieval engine, database, workers, provider adapters or spending ledger.

## Why this fits the repository

- `api.py:retrieval_preview` already returns structured evidence and performs no
  answer generation or verification calls.
- `retrieval.py:retrieve_evidence` freezes a corpus revision and embedding space,
  retrieves hybrid dense/lexical candidates and packs whole excerpts into a
  bounded context.
- `domain.py:EvidencePack` and `EvidenceItem` carry immutable source version IDs,
  titles, page/character coordinates and text hashes for citations.
- `integrations/tools.py` already demonstrates narrow, corpus-scoped retrieval
  and source-preview tools.
- `providers/transport.py` and `storage.py:reserve_call` already own inference
  retries, concurrency and persistent spending limits. MCP must reuse them.

## Implemented tool contract

| Tool | Input | Output | Boundary |
| --- | --- | --- | --- |
| `search_evidence` | Question, authorized corpus ID | Bounded excerpts, citation IDs/coordinates, corpus revision, embedding space, content hash, fixture labels | No generation; only configured query embedding and existing retrieval |
| `get_evidence_source` | `corpus_id`, `version_id`, `evidence_id` from search | Bounded source excerpt and citation metadata | Check corpus membership and source availability; no arbitrary paths or URLs |

Use typed structured results with explicit empty-result and error behavior.
Preserve mock/fixture labels and indicate that returned evidence is not a
verified answer. Include source identifiers and canonical source references;
URLs are not authorization grants. Do not send oversized originals by default.

Do not initially expose upload, delete, evaluation, job retry, raw traces,
configuration, credential management or arbitrary URL fetch tools. If a consumer
later needs Evidence Lab's verified answer workflow, add a separate explicit
tool using existing durable run submission/polling and release rules.

## Authorization and server credentials

Remote integration must not weaken the established requirement that server
credentials are usable only with operator authorization.

The implemented private interface uses a shared static operator token, not delegated
identities or OAuth infrastructure. Choosing the authorization provider and the
operator-to-corpus grant mapping is an implementation prerequisite for broadly
accessible remote integration. A private pilot can serve already trusted
operator clients with explicitly configured bearer authentication, but that
retains full operator trust and does not provide scoped delegation or automatic
OAuth login interoperability. It must be documented as such.

Every request authenticates that operator token, and each tool checks the global
corpus allowlist. Provider keys come from server configuration and never appear in
tool inputs/results. There are no per-user document permissions: files are shared
by all users with workspace access. [Workspace isolation](todo.md) is planned.
Dashboard BYOK settings are not accepted by MCP.

Future untrusted-client integration needs an established OAuth authorization
server, scoped audience-bound tokens, issuer/expiration/scope validation and
protected-resource metadata. Those capabilities are not implemented. The static
operator token has no per-client grants or automatic expiry.

MCP routes require their own tested authorization boundary: the current middleware
only protects paths beginning `/api/`. Mounting `/mcp` must not accidentally create
an unauthenticated path to server embeddings. Never forward an incoming MCP token
to a provider or the operator REST API; authenticate the caller and invoke the
shared application service with server-owned configuration.

## Transport and deployment

Use the Streamable HTTP endpoint `/api/mcp/`, with stateless request handling
and structured JSON responses where supported by the pinned SDK/client versions.
Requests must be independently authorized; session identifiers are not identity.

Local clients connect through loopback. Remote clients use HTTPS with Origin/Host
validation and request/response size limits appropriate to MCP. Keep the existing
provider ledger caps. Per-client rate limits are not implemented; a deployment
proxy may supply rate limits to bound embedding spend/shared-capacity use.
do not introduce another proxy when the deployed one can enforce them.

No additional vector store, model host, agent framework, broker or database is
needed. Use the official SDK to implement protocol lifecycle/validation and pin
its tested version. Keep transport code separate from retrieval/application code.

## Alternatives considered

| Approach | Assessment |
| --- | --- |
| REST-only tool wrappers | Lowest effort for known custom consumers, but each agent needs its own tool-discovery/schema wrapper. Retain as an integration option. |
| MCP Streamable HTTP | Selected: one discoverable contract supports local and remote clients and keeps deployment aligned with the existing web application. |
| Stdio-only MCP | Good for local subprocess clients; does not satisfy the remote requirement. Optional compatibility adapter. |
| Another agent or A2A service | Useful for delegating autonomous tasks; adds responsibilities beyond supplying RAG evidence. Not required here. |
| Full generated MCP exposure of the REST API | Exposes administrative operations and broad operator authority; use explicitly registered retrieval tools. |

## Engineering evidence

`apps/evidence-lab/tests/test_mcp_api.py` covers real SDK discovery/tool calls,
operator authentication, Host/Origin enforcement, corpus checks, bounded payloads,
safe errors, native pgvector citations, deletion and embedding-space mismatch.
Run the shared [CI checks](ci.md) and verify the deployed HTTP endpoint separately.
Fixture checks do not establish retrieval/model quality or deploy a public endpoint. OAuth tests are deferred
with OAuth itself. Remove the MCP mount to disable the interface; there is no
runtime enable/disable flag.

## Connect to the implemented interface

The endpoint is available whenever the API is running. MCP always requires a
non-empty configured `runtime.operator_token`, including in mock mode; no token
means HTTP 401. Each request must send `Authorization: Bearer <operator token>`.
The existing dashboard API retains its authentication behavior. No provider keys,
mode, endpoint, model or budget overrides are accepted by MCP tools.
Live `search_evidence` requires `runtime.credentials: server`, configured provider
credentials, limits and positive spending caps for any paid embeddings. A live
`browser` credential configuration returns `invalid_configuration` from search,
including with fixture embeddings; source lookup itself needs no provider key.

Local URL: `http://127.0.0.1:8000/api/mcp/`. Remote URL: the HTTPS backend origin
plus `/api/mcp/`. Prefer the backend URL. The bundled Pages Function forwards
`/api/*`, including MCP, but retains its own same-origin write boundary; it does
not support arbitrary cross-origin browser MCP clients. Use an MCP client that supports explicitly
configured HTTP bearer credentials. The default SDK client discovers the current
2026-07-28 protocol; the 2025-11-25 initialize flow is also tested.

Add remote hostnames and enabled corpora to private YAML, preserving the existing
operator token and provider/budget settings:

```yaml
agent_rag:
  allowed_corpora: [default, support]
  allowed_hosts:
    - localhost
    - localhost:*
    - 127.0.0.1
    - 127.0.0.1:*
    - rag.your-domain.test
  allowed_origins:
    - https://rag.your-domain.test
  max_request_bytes: 65536
  max_result_bytes: 65536
```

Replace the illustrative remote hostname with the actual backend hostname; these
are exact Host entries (add an explicit port entry if needed). Restart the API
after configuration changes. Default hosts/origins permit loopback only and the
default corpus grant is `default`. Keep TLS termination and forwarded-host/scheme
configuration correct at the backend proxy. Authenticated MCP requests use the
SDK's Origin allowlist; REST writes retain same-origin protection. Browser CORS
preflight/response headers are not configured by this interface.

`max_result_bytes` bounds the structured payload, and MCP also includes a textual
representation of that payload plus protocol overhead. Oversized packs fail
explicitly rather than silently truncating excerpts. Tool errors set `isError`;
clients must check it before using output. Empty retrieval is a successful result
with `evidence.items: []`. Both tools return `verified_answer: false`. Only search
results include `mode`, `embedding_mode` and `qualification`; fixture query
embeddings produce `qualification: fixture_only`, including live configurations
that reuse fixture vectors. Source results contain `corpus_id` and `item` instead.

The tools logically read corpus data. Search can incur a configured paid query
embedding and records its reservation in the existing ledger. It never invokes
generation or verification. Server-side total/phase budget, attempt and concurrency
caps apply. Source reads do not make provider calls.

### Official Python SDK client

Use the project's environment with the installed MCP 2.3.0 SDK. Supply the token
through the client's secret mechanism or environment; do not place it in model
prompts or command-line arguments. This example's environment variable is a
client credential input, not a new server configuration variable.

```python
import asyncio
import os

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main():
    url = os.environ.get("EVIDENCE_LAB_MCP_URL", "http://127.0.0.1:8000/api/mcp/")
    token = os.environ["EVIDENCE_LAB_OPERATOR_TOKEN"]
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(url, http_client=http)) as agent:
            result = await agent.call_tool("search_evidence", {
                "question": "What is the refund period?", "corpus_id": "default",
            })
            if result.is_error:
                raise RuntimeError("Evidence retrieval failed; inspect the safe tool error.")
            print(result.structured_content)


asyncio.run(main())
```

`get_evidence_source` arguments are `corpus_id`, `version_id`, and `evidence_id`,
using identifiers returned by search. The storage read checks corpus membership
and source deletion in one transaction. Old immutable source versions remain
readable within the authorized corpus until deleted; no remembered MCP session
or retrieval result grants continued access after deletion.

### Trust limitation

The corpus allowlist limits this MCP interface globally; it is not a per-client
authorization system. An operator-token holder can still use the administrative
REST API. Give this credential only to agents already trusted as operators.
OAuth audience/scope validation and per-client grants from the architecture above
are not implemented or advertised as available. Broad untrusted remote access
requires that integration first.

## Primary guidance checked

- [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk):
  typed tools, structured results and local/HTTP transports. Current documentation
  was also checked through Context7; pin the actual selected release before coding.
- [MCP authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization):
  HTTP authorization, resource metadata, least-privilege scopes and audience-bound
  tokens. Local stdio credentials follow a separate transport model.
- [MCP Streamable HTTP specification](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx):
  request transport and protocol compatibility requirements.
