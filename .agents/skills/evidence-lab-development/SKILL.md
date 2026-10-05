---
name: evidence-lab-development
description: Implement Evidence Lab backend, contract, or workspace changes using its existing Python and Task conventions.
---

# Evidence Lab development

Read `AGENTS.md`, `docs/contracts.md`, and the relevant section of `docs/implementation-plan.md`; do not modify the approved plan.

Choose the nearest existing module in `apps/evidence-lab/src/evidence_lab` before adding a pattern. Keep API/domain contracts in `domain.py`, provider behavior in adapters/configuration, and durable state in PostgreSQL/pgvector. Preserve `rag_*` table names and the existing `RAG_*` environment variables unless a separate persistence/configuration migration is explicitly requested.

Coordinate shared contract edits with dashboard contributors. Keep deterministic fixtures distinguishable from real endpoint measurements and prevent unverified drafts from entering the normal answer field. Private YAML accepts direct key values; redact secrets from exceptions, logs and traces.

Use the root environment and tasks: `task setup`, `task app:lint`, `task app:test`, and `task check`. Tests marked integration/native PostgreSQL need dedicated test DSNs; never point them at application data. Update runnable commands and affected docs with behavioral changes. For dashboard-only work, use `$evidence-lab-dashboard` instead.
