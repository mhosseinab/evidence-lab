# RAG PoC implementation

Read docs/implementation-plan.md. The user authorized implementation. All model inference is external or explicitly deterministic mock fixtures; never download or host models. No paid calls without configured keys, limits and budget.

Use Python 3.12+, FastAPI, Pydantic v2, httpx, psycopg 3, Alembic, pypdf and PostgreSQL/pgvector. No SQLite or in-memory production storage fallback. Static UI is served by FastAPI. Keep provider details in adapters and configuration. Direct key values in private YAML must work. Never expose secrets in API errors/logs/traces. No unverified draft in the normal answer field.

Shared contracts are in src/rag_poc/domain.py and docs/contracts.md. Coordinate changes rather than editing another contributor's owned files. Tests must cover meaningful failure modes. The mock demo is a plumbing fixture, never evidence of model quality. Human-reviewed held-out gold and real endpoint performance remain unmeasured until actually run.

The project root is the directory containing this file. Use its .venv/bin/python and .venv/bin/pytest. Do not modify the approved plan or claim production readiness. Include an accurate verification report and runnable commands.
