# Evidence Lab API and worker

Python workspace member for the FastAPI API, durable worker, PostgreSQL migrations
and the `evidence-lab` operator CLI (`evidence_lab` Python package). From the repository root run `task setup`, `task check`,
`task app:serve` and `task app:worker`. Full setup and configuration instructions
are in the root README.md and docs/.

The dashboard is authored in apps/dashboard/src/ and apps/dashboard/public/.
`task dashboard:build` compiles into apps/dashboard/dist/, which FastAPI serves
directly at `/` and `/static`. `task dashboard:serve` and `task app:serve` both
build the dashboard and start the API from the repository root. The Python wheel
ships backend code and Alembic migrations; browser assets remain the separate
dashboard build. Serving an installed wheel requires this built dashboard and
the repository root as its working directory. Docker packages the frontend
stage output directly at /app/apps/dashboard/dist/.

Model inference is external or explicitly deterministic fixtures. The fixture
demo establishes plumbing behavior, not semantic model quality.
