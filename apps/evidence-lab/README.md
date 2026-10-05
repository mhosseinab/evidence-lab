# Evidence Lab API and worker

Python workspace member for the FastAPI API, durable worker, PostgreSQL migrations
and the `evidence-lab` operator CLI (`evidence_lab` Python package). From the repository root run `task setup`, `task check`,
`task app:serve` and `task app:worker`. Full setup and configuration instructions
are in the root README.md and docs/.

The dashboard is authored in apps/dashboard/src/ and apps/dashboard/public/.
`task dashboard:build` compiles into apps/dashboard/dist/. FastAPI serves the
dashboard at `/` and its assets under `/static/`. `task dashboard:serve` and `task app:serve` both
build the dashboard and start the API from the repository root. The Python wheel
ships backend code and Alembic migrations; browser assets remain the separate
dashboard build. Serving an installed wheel requires this built dashboard and
the repository root as its working directory. Docker packages the frontend
stage output directly at /app/apps/dashboard/dist/.

Model inference is external or explicitly deterministic fixtures. The fixture
demo establishes plumbing behavior, not semantic model quality.

`graph.py` defines the LangGraph retrieval, generation, structural checks,
verification, bounded repair and release workflow. `integrations/` provides
LangChain model, embedding and corpus-bound tool interfaces through the existing
provider budget and attempt ledger. PostgreSQL stores bounded conversation
snapshots containing released answers; interrupted jobs restart rather than
resume graph checkpoints. Optional `langsmith_trace.py` exports stage metadata
without prompts, answers, evidence or conversation content.

Use `task dev` for API, worker and Vue hot reload together. Configure `memory`
and `langsmith` in private YAML; all fields are documented in the root README
and `configs/README.md`.

`task app:typecheck` checks backend source, tests and tooling with pinned
Basedpyright. Root `task lint`, `task check` and `task typecheck` include it.
The repository's `pyproject.toml` binds checking to Python 3.12 and the root
`.venv`, using standard mode without per-rule diagnostic suppression.
