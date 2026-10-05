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

CREATE TABLE rag_embedding_spaces (
    id text PRIMARY KEY,
    dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 16000),
    model text NOT NULL,
    fingerprint text NOT NULL,
    manifest jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, dimensions)
);

CREATE TABLE rag_corpora (
    id text PRIMARY KEY,
    space_id text NOT NULL REFERENCES rag_embedding_spaces(id),
    revision bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE rag_documents (
    id text PRIMARY KEY,
    corpus_id text NOT NULL REFERENCES rag_corpora(id),
    name text NOT NULL,
    media_type text NOT NULL,
    active_version_id text,
    latest_version_no bigint NOT NULL DEFAULT 0,
    active_version_no bigint NOT NULL DEFAULT 0,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX rag_documents_corpus_idx ON rag_documents(corpus_id);

CREATE TABLE rag_document_versions (
    id text PRIMARY KEY,
    document_id text NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    version_no bigint NOT NULL,
    space_id text NOT NULL REFERENCES rag_embedding_spaces(id),
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
ALTER TABLE rag_documents ADD CONSTRAINT rag_documents_active_version_fk
    FOREIGN KEY (active_version_id) REFERENCES rag_document_versions(id) ON DELETE SET NULL;
CREATE INDEX rag_versions_hash_idx ON rag_document_versions(content_hash, pipeline_revision, space_id);

CREATE TABLE rag_chunks (
    id text PRIMARY KEY,
    version_id text NOT NULL REFERENCES rag_document_versions(id) ON DELETE CASCADE,
    text text NOT NULL CHECK (length(text) > 0),
    text_hash text NOT NULL,
    page integer NOT NULL CHECK (page >= 1),
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset >= start_offset),
    lexical tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, text)) STORED,
    UNIQUE (version_id, page, start_offset, end_offset)
);
CREATE INDEX rag_chunks_version_idx ON rag_chunks(version_id);
CREATE INDEX rag_chunks_lexical_idx ON rag_chunks USING gin(lexical);

CREATE TABLE rag_chunk_embeddings (
    chunk_id text NOT NULL REFERENCES rag_chunks(id) ON DELETE CASCADE,
    space_id text NOT NULL REFERENCES rag_embedding_spaces(id),
    embedding vector NOT NULL CHECK (vector_norm(embedding) > 0),
    dimensions integer GENERATED ALWAYS AS (vector_dims(embedding)) STORED,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (chunk_id, space_id),
    FOREIGN KEY (space_id, dimensions) REFERENCES rag_embedding_spaces(id, dimensions)
);
CREATE INDEX rag_chunk_embeddings_space_idx ON rag_chunk_embeddings(space_id);

CREATE TABLE rag_embedding_cache (
    space_id text NOT NULL REFERENCES rag_embedding_spaces(id),
    text_hash text NOT NULL,
    embedding vector NOT NULL CHECK (vector_norm(embedding) > 0),
    dimensions integer GENERATED ALWAYS AS (vector_dims(embedding)) STORED,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (space_id, text_hash),
    FOREIGN KEY (space_id, dimensions) REFERENCES rag_embedding_spaces(id, dimensions)
);

CREATE TABLE rag_jobs (
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
CREATE INDEX rag_jobs_claim_idx ON rag_jobs(status, created_at);

CREATE TABLE rag_runs (
    id text PRIMARY KEY,
    job_id text NOT NULL REFERENCES rag_jobs(id),
    corpus_id text NOT NULL REFERENCES rag_corpora(id),
    space_id text NOT NULL REFERENCES rag_embedding_spaces(id),
    question text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    settings jsonb NOT NULL DEFAULT '{}',
    data jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE INDEX rag_runs_created_idx ON rag_runs(created_at DESC);

CREATE TABLE rag_run_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text NOT NULL REFERENCES rag_runs(id) ON DELETE CASCADE,
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE rag_calls (
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
CREATE INDEX rag_calls_run_idx ON rag_calls(run_id);
CREATE INDEX rag_calls_phase_idx ON rag_calls(phase, mock);
CREATE INDEX rag_calls_active_idx ON rag_calls(status, active_until);
"""


def upgrade():
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    op.execute("ALTER TABLE rag_documents DROP CONSTRAINT rag_documents_active_version_fk")
    for table in (
        "rag_calls", "rag_run_events", "rag_runs", "rag_jobs", "rag_embedding_cache",
        "rag_chunk_embeddings", "rag_chunks", "rag_document_versions", "rag_documents",
        "rag_corpora", "rag_embedding_spaces",
    ):
        op.execute(f"DROP TABLE {table}")
