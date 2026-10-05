# Evidence Lab implementation

Read docs/implementation-plan.md. The user authorized implementation. All model inference is external or explicitly deterministic mock fixtures; never download or host models. No paid calls without configured keys, limits and budget.

Use Python 3.12+, FastAPI, Pydantic v2, httpx, psycopg 3, Alembic, pypdf and PostgreSQL/pgvector. No SQLite or in-memory production storage fallback. Static UI is served by FastAPI. Dashboard uses Vue single-file components with the Composition API and `<script setup lang="ts">`. Sources are apps/dashboard/src/main.ts, App.vue, views, components, composables, api, types, utils and assets, plus apps/dashboard/public; Vite builds the frontend and vue-tsc checks component types; task dashboard:build generates apps/dashboard/dist, served directly by FastAPI from the repository root. task dashboard:serve starts the API with that dashboard build. Do not edit generated dashboard assets. Keep provider details in adapters and configuration. Direct key values in private YAML must work. Never expose secrets in API errors/logs/traces. No unverified draft in the normal answer field.

Shared contracts are in apps/evidence-lab/src/evidence_lab/domain.py and docs/contracts.md. Coordinate changes rather than editing another contributor's owned files. Tests must cover meaningful failure modes. The mock demo is a plumbing fixture, never evidence of model quality. Human-reviewed held-out gold and real endpoint performance remain unmeasured until actually run.

The project root is the directory containing this file; The application distribution and CLI are evidence-lab; its Python package is evidence_lab. Use the root uv workspace environment (.venv/bin/python and .venv/bin/pytest). Run task setup before development; use task lint, task test and task check from the repository root. Python application source and tests are in apps/evidence-lab; TypeScript dashboard source and tests are in apps/dashboard; shared operational helpers are in tooling. Use Node.js 24+, pnpm and Task for frontend work. Root task lint, task test and task build coordinate both apps. Do not modify the approved plan or claim production readiness. Verify engineering changes with task check and the applicable native pytest tests; record actual passes, failures and prerequisite skips in an accurate verification report with runnable commands. Git records source history; runtime/model measurements and externally collected qualification fault-study evidence remain separate requirements.


## Project coding rules

- Keep screen composition in dashboard views, reusable presentation in components, state/lifecycle in composables, HTTP transport in api/client.ts, and API contracts in types/api.ts. Use the existing typed dashboard context for shared workspace state; add routing or a separate state store only when required by the task.
- Render untrusted text through Vue interpolation, preserve fixture-mode and answer-verification labels, and clean up polling or pending requests on unmount. Use Vitest and Vue Test Utils for observable dashboard behavior.
- Run tasks from this repository root. task dashboard:dev starts Vite hot reload with an API proxy to port 8000; task dashboard:serve builds and serves the same-origin FastAPI application. Do not put dashboard output back into the Python package.
- Preserve rag_* database tables and RAG_* configuration variables unless an explicit migration is requested. Native tests must use dedicated test databases and the existing private-schema fixtures.
- Coordinate shared files before concurrent edits, and avoid parallel clean/build operations against the same generated directory. Scope cleanup to artifacts and fixture resources created for the current task; preserve dependencies, application data, and existing deployments.

## Project skills and subagents

Project skills live under .agents/skills: evidence-lab-development for backend/workspace changes, evidence-lab-dashboard for Vue work, evidence-lab-verification for engineering checks and reporting, and evidence-lab-security-review for explicitly requested security audits and security-fix reviews. Read the relevant SKILL.md when applying a workflow.

Native Codex roles are defined in .codex/agents/evidence_dashboard.toml, evidence_backend.toml, evidence_verifier.toml, and evidence_security_reviewer.toml. They inherit session permissions and model settings. Their file ownership is a coordination convention, not a filesystem restriction. Use them only when delegation is authorized by the user; ask before activating parallel work when authorization is absent. For runtimes that expose only generic subagents, pass the chosen role's instructions and an explicit file scope in the delegated task.
