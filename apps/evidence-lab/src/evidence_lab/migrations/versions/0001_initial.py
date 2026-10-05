"""Initial transactional corpus, exact retrieval, jobs, and cost ledger.

Revision ID: 0001_initial
Revises:
"""
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE evidence_embedding_spaces (
    id text PRIMARY KEY,
    dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 16000),
    model text NOT NULL,
    fingerprint text NOT NULL,
    manifest jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, dimensions)
);

CREATE TABLE evidence_corpora (
    id text PRIMARY KEY,
    space_id text NOT NULL REFERENCES evidence_embedding_spaces(id),
    revision bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE evidence_documents (
    id text PRIMARY KEY,
    corpus_id text NOT NULL REFERENCES evidence_corpora(id),
    name text NOT NULL,
    media_type text NOT NULL,
    active_version_id text,
    latest_version_no bigint NOT NULL DEFAULT 0,
    active_version_no bigint NOT NULL DEFAULT 0,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX evidence_documents_corpus_idx ON evidence_documents(corpus_id);

CREATE TABLE evidence_document_versions (
    id text PRIMARY KEY,
    document_id text NOT NULL REFERENCES evidence_documents(id) ON DELETE CASCADE,
    version_no bigint NOT NULL,
    space_id text NOT NULL REFERENCES evidence_embedding_spaces(id),
    name text NOT NULL,
    media_type text NOT NULL,
    content_hash text NOT NULL,
    pipeline_revision text NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    raw bytea NOT NULL,
    pages jsonb NOT NULL DEFAULT '[]',
    job_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    activated_at timestamptz,
    UNIQUE (document_id, version_no)
);
ALTER TABLE evidence_documents ADD CONSTRAINT evidence_documents_active_version_fk
    FOREIGN KEY (active_version_id) REFERENCES evidence_document_versions(id) ON DELETE SET NULL;
CREATE INDEX evidence_versions_hash_idx ON evidence_document_versions(content_hash, pipeline_revision, space_id);

CREATE TABLE evidence_chunks (
    id text PRIMARY KEY,
    version_id text NOT NULL REFERENCES evidence_document_versions(id) ON DELETE CASCADE,
    text text NOT NULL CHECK (length(text) > 0),
    text_hash text NOT NULL,
    page integer NOT NULL CHECK (page >= 1),
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset >= start_offset),
    lexical tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, text)) STORED,
    UNIQUE (version_id, page, start_offset, end_offset)
);
CREATE INDEX evidence_chunks_version_idx ON evidence_chunks(version_id);
CREATE INDEX evidence_chunks_lexical_idx ON evidence_chunks USING gin(lexical);

CREATE TABLE evidence_chunk_embeddings (
    chunk_id text NOT NULL REFERENCES evidence_chunks(id) ON DELETE CASCADE,
    space_id text NOT NULL REFERENCES evidence_embedding_spaces(id),
    embedding vector NOT NULL CHECK (vector_norm(embedding) > 0),
    dimensions integer GENERATED ALWAYS AS (vector_dims(embedding)) STORED,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (chunk_id, space_id),
    FOREIGN KEY (space_id, dimensions) REFERENCES evidence_embedding_spaces(id, dimensions)
);
CREATE INDEX evidence_chunk_embeddings_space_idx ON evidence_chunk_embeddings(space_id);

CREATE TABLE evidence_embedding_cache (
    space_id text NOT NULL REFERENCES evidence_embedding_spaces(id),
    text_hash text NOT NULL,
    embedding vector NOT NULL CHECK (vector_norm(embedding) > 0),
    dimensions integer GENERATED ALWAYS AS (vector_dims(embedding)) STORED,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (space_id, text_hash),
    FOREIGN KEY (space_id, dimensions) REFERENCES evidence_embedding_spaces(id, dimensions)
);

CREATE TABLE evidence_jobs (
    id text PRIMARY KEY,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    worker_id text,
    token text,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    result jsonb,
    error jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE INDEX evidence_jobs_claim_idx ON evidence_jobs(status, created_at);

CREATE TABLE evidence_conversations (
    id uuid PRIMARY KEY,
    corpus_id text NOT NULL REFERENCES evidence_corpora(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, corpus_id)
);
CREATE INDEX evidence_conversations_corpus_idx ON evidence_conversations(corpus_id);

CREATE TABLE evidence_runs (
    id text PRIMARY KEY,
    job_id text NOT NULL REFERENCES evidence_jobs(id),
    corpus_id text NOT NULL REFERENCES evidence_corpora(id),
    space_id text NOT NULL REFERENCES evidence_embedding_spaces(id),
    conversation_id uuid NOT NULL,
    question text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    settings jsonb NOT NULL DEFAULT '{}',
    data jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    FOREIGN KEY (conversation_id, corpus_id) REFERENCES evidence_conversations(id, corpus_id)
);
CREATE INDEX evidence_runs_conversation_idx ON evidence_runs(conversation_id, finished_at DESC);
CREATE INDEX evidence_runs_created_idx ON evidence_runs(created_at DESC);

CREATE TABLE evidence_run_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text NOT NULL REFERENCES evidence_runs(id) ON DELETE CASCADE,
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE evidence_calls (
    id text PRIMARY KEY,
    run_id text NOT NULL,
    phase text NOT NULL,
    profile text NOT NULL,
    mock boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'reserved',
    estimated_cost numeric(24, 12) NOT NULL CHECK (estimated_cost >= 0),
    charged_cost numeric(24, 12) NOT NULL CHECK (charged_cost >= 0),
    actual_cost numeric(24, 12),
    cost_known boolean NOT NULL DEFAULT false,
    usage jsonb,
    detail jsonb,
    active_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE INDEX evidence_calls_run_idx ON evidence_calls(run_id);
CREATE INDEX evidence_calls_phase_idx ON evidence_calls(phase, mock);
CREATE INDEX evidence_calls_active_idx ON evidence_calls(status, active_until);
"""


def upgrade():
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    op.execute("ALTER TABLE evidence_documents DROP CONSTRAINT evidence_documents_active_version_fk")
    for table in (
        "evidence_calls", "evidence_run_events", "evidence_runs", "evidence_conversations", "evidence_jobs", "evidence_embedding_cache",
        "evidence_chunk_embeddings", "evidence_chunks", "evidence_document_versions", "evidence_documents",
        "evidence_corpora", "evidence_embedding_spaces",
    ):
        op.execute(f"DROP TABLE {table}")
