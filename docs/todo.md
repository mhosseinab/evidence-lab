# TODO

## Workspace isolation

Status: planned; not implemented.

Currently, the operator token grants access to the shared application workspace.
Documents in the database have no per-user access isolation. BYOK credentials
control provider usage, not document ownership or access permissions.

Target behavior: **files are shared with all users in the same workspace**.
Workspace members can retrieve and use those files; users outside the workspace
must not access its files, evidence, queries or conversation history. Private
files for individual users within a workspace are outside this feature's scope.

- [ ] Add authenticated user identities and workspace membership.
- [ ] Associate corpora and their documents with a workspace; migrate existing
      data into an explicit workspace.
- [ ] Enforce workspace access on API and MCP reads/writes, retrieval, source
      lookups, query results, conversation history and background jobs.
- [ ] Scope agent credentials to workspace permissions and keep operator
      credentials for administration.
- [ ] Test that members can share files within a workspace and that cross-workspace
      reads, writes and indirect retrieval are denied.

See [Agent RAG authorization](agent-rag-interface.md) for the current MCP boundary.
