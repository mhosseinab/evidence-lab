---
name: evidence-lab-verification
description: Verify Evidence Lab engineering changes and report real test/build/runtime evidence without overstating model quality.
---

# Evidence Lab verification

Read `AGENTS.md`, the root `Taskfile.yml`, and the latest `docs/verification-report.md`. Identify changed behavior and its required checks before running broad verification.

Run `task check` from the repository root. Run `task build` for packaging or dashboard integration changes. Use `task test:offline` when native PostgreSQL is unavailable and report those exclusions; an offline pass does not prove the entire suite passed.

For integration/native tests, use dedicated `EVIDENCE_LAB_TEST_DSN` and `EVIDENCE_LAB_TEST_NATIVE_ADMIN_DSN` databases. Respect the existing private-schema fixture isolation. Never reuse a running application database. Exercise changed dashboard flows against deterministic mock configuration; distinguish browser plumbing checks from model quality or endpoint performance.

For serving changes, compare generated Vite asset URLs with FastAPI responses and test an isolated Compose stack when Docker is available. Preserve existing deployment containers and volumes. Track temporary fixture resources and remove only resources created for this verification after capturing results.

Record exact commands, pass/failure/skip counts, environment prerequisites, and material limits in `docs/verification-report.md`. Git supplies source history; do not recreate BUILD-MANIFEST.json or the removed verification runner. Runtime measurements and external qualification fault-study artifacts remain separate evidence. Run `task clean` at the end when requested, retaining dependencies and runtime data.
