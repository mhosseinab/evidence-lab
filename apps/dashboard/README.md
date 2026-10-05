# Evidence Lab dashboard

Vue 3 with TypeScript, Composition API and single-file components. Vite compiles the dashboard; FastAPI serves the production build and API on one origin.

```text
index.html               HTML entry
vite.config.ts           Vue compiler, build base, API proxy and Vitest
src/main.ts              Application mount and global styles
src/App.vue              Workspace shell and lifecycle
src/views/               Ask, Documents and Evaluations screens
src/components/          Shared layout, answers, evidence and dialogs
src/composables/          Workspace state/actions and typed injection context
src/api/                 Validated HTTP client
src/types/               HTTP payload contracts
src/utils/               Pure presentation and status helpers
src/assets/              Existing shared visual styles
public/                  Favicon copied into the build
tests/                  API, component and workflow regression tests
```

Keep HTTP validation at the boundary. Treat responses as unknown until validated. Render source/answer text with Vue interpolation, never `v-html`. The operator token stays in memory. Each mounted workspace owns its state; polling and request races must respect corpus selection and component disposal. Typed context shares state/actions without global singletons. The three hash views do not require a routing library or external state store.

Use component props/events for presentation and composables for asynchronous workflows. Preserve the answer release guard: running, failed, shadow and unqualified runs must not display answer content even if it appears in a payload. Evaluation metrics and mock limitations must remain visible.

`WorkflowPanel.vue` displays validated LangGraph stages, execution counts,
timings and LangSmith status. The Ask workspace retains the server-issued
conversation ID for follow-ups. New conversation and corpus changes clear it;
stored conversation history stays private to the backend.

From the repository root:

```bash
task setup
task dashboard:typecheck
task dashboard:lint
task dashboard:test
task dashboard:build
task dashboard:serve
```

For hot reload, run `task dev` to start the API, worker and Vite together. Ctrl+C stops the API, worker and Vite; the development database remains running. The default mock configuration starts/waits for PostgreSQL and migrates it automatically. Custom configurations require an available, explicitly migrated database. The root command sets Vite’s `/api` and `/health` proxy from the selected configuration; standalone `task dashboard:dev` defaults to localhost port 8000; production remains served by FastAPI. Generated `dist/` assets have content hashes and are ignored by Git. `task clean` removes them; startup and packaging tasks rebuild them.

`vue-tsc` checks templates and TypeScript, Biome checks both SFC markup and embedded scripts, and Vitest with Vue Test Utils exercises components and state. TypeScript 6 is pinned because this version of `vue-tsc` requires the JavaScript compiler API that TypeScript 7 no longer exports.
