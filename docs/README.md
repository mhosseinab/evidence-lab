# Evidence Lab documentation

Start with the [project README](../README.md) for quick setup and the complete
configuration reference. Use the operator guide for detailed workflows and commands.

| Guide | Purpose |
|---|---|
| [Operator and development guide](operator-guide.md) | Architecture, provider setup, commands, tests and recovery |
| [Qualification assurance](qualification-assurance.md) | Answer checks, publication modes, measured policy qualification and trust limits |
| [Contracts](contracts.md) | Provider, storage, HTTP and LangChain integration boundaries |
| [Dashboard BYOK](byok.md) | Browser keys, modes, workspace embeddings, operator sign-in and recovery |
| [Agent RAG interface](agent-rag-interface.md) | Read-only MCP tools, local/remote client setup and authorization limits |
| [TODO](todo.md) | Planned workspace isolation and shared-file access model |
| [Evaluation](evaluation.md) | Paired studies, human review and policy qualification |
| [Experiments](experiments.md) | Repeatability and API load measurement commands |
| [Approved plan](implementation-plan.md) | Original design and evaluation requirements; current behavior is documented in the guides above |
| [Container pins](container-images.json) | Recorded image digests and their sources |
| [Deployment](deployment.md) | Cloudflare Pages dashboard and GHCR-backed VPS deployment |
| [CI](ci.md) | Native checks, no-skip gate, build artifacts and deployment triggers |
| [Provider integrations](../apps/evidence-lab/src/evidence_lab/providers/README.md) | Guarded OpenAI SDK transport and the Clef verification tool |
| [Configurations](../configs/README.md) | Sample selection, credentials and Clef activation |

Generated logs, screenshots and historical fixture runs are not documentation.
Keep operator study outputs outside this directory; Git retains source history.

Files are shared by everyone with workspace access. Operator sign-in controls
server credentials and API access; BYOK does not provide user identity or document
isolation. The [TODO](todo.md) describes future isolation between workspaces,
with files remaining shared among members of each workspace.

The approved plan remains the original baseline; the guides describe current behavior.
