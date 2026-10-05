---
name: evidence-lab-dashboard
description: Build or change the Evidence Lab Vue dashboard, including typed API boundaries, composables, components, and user-facing behavior tests.
---

# Evidence Lab dashboard

Read `AGENTS.md`, `apps/dashboard/package.json`, `apps/dashboard/Taskfile.yml`, and the nearest existing view or component. Verify current Vue/Vite/test APIs with Context7 before changing third-party integration.

Use Vue single-file components with `<script setup lang="ts">` and the Composition API. `src/main.ts` mounts `App.vue`; views own screens, components own reusable presentation, composables own state/actions/lifecycle, `api/client.ts` owns transport, `types/api.ts` describes contracts, `utils` holds pure transformations, and `assets` holds source styles. Reuse the typed dashboard context for shared workspace state. Add a router or external state store only when required behavior calls for one.

Keep API calls and polling outside presentation components. Cancel or dispose pending work on unmount, expose actionable loading/error states, and retain the original response's evidence/verification status. Render provider/model text through Vue interpolation rather than raw HTML. Preserve keyboard navigation, dialog focus, upload validation, and fixture-mode labels.

Vite builds `apps/dashboard/dist`; FastAPI serves that directory from the repository root. Do not copy assets into the Python package or edit generated output. Use `task dashboard:dev` for Vite hot reload (API proxy on port 8000), or `task dashboard:serve` for the compiled same-origin application.

Run `task dashboard:typecheck`, `task dashboard:lint`, `task dashboard:test`, and `task dashboard:build`. Vitest and Vue Test Utils should prove user-visible success/failure behavior and lifecycle cleanup; avoid tests that merely match source strings. After build changes, verify the FastAPI root and generated asset URLs as well as the Docker build path.
