-- Wallia 0001 — schéma initial (PostgreSQL 17 + pgvector)
-- Toutes les identités sont des UUID opaques ; contraintes FK et statuts explicites.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL,
    password_hash text NOT NULL,
    is_admin boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    password_changed_at timestamptz
);
CREATE UNIQUE INDEX users_email_lower_uq ON users (lower(email));

CREATE TABLE sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    csrf_token text NOT NULL,
    user_agent text,
    ip text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX sessions_user_idx ON sessions (user_id);

CREATE TABLE login_attempts (
    id bigserial PRIMARY KEY,
    ip text NOT NULL,
    email text,
    success boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_attempts_ip_time_idx ON login_attempts (ip, created_at DESC);
CREATE INDEX login_attempts_email_time_idx ON login_attempts (email, created_at DESC);

CREATE TABLE conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title text NOT NULL DEFAULT 'Nouvelle conversation',
    case_state jsonb NOT NULL DEFAULT '{"product": null, "version": null, "symptom": null, "facts": [], "hypotheses": [], "proposed_checks": [], "performed_checks": [], "results": [], "missing_info": []}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_user_idx ON conversations (user_id, updated_at DESC);

CREATE TABLE messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq integer NOT NULL,
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'complete' CHECK (status IN ('streaming', 'complete', 'cancelled', 'error', 'interrupted')),
    error text,
    model text,
    demo boolean NOT NULL DEFAULT false,
    sources jsonb,
    stop_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT messages_conv_seq_uq UNIQUE (conversation_id, seq)
);
CREATE INDEX messages_conv_idx ON messages (conversation_id, seq);
CREATE INDEX messages_streaming_idx ON messages (status) WHERE status = 'streaming';

CREATE TABLE attachments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
    filename_original text NOT NULL,
    stored_relpath text NOT NULL,
    content_type text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('pdf', 'text', 'image')),
    size_bytes bigint NOT NULL,
    sha256 text NOT NULL,
    pages integer,
    width integer,
    height integer,
    extracted_text text,
    uploaded_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX attachments_conv_idx ON attachments (conversation_id);

CREATE TABLE documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    origin text NOT NULL DEFAULT 'demo',
    product text,
    versions text[] NOT NULL DEFAULT '{}',
    language text NOT NULL DEFAULT 'fr',
    document_date date,
    checksum_sha256 text NOT NULL,
    demo boolean NOT NULL DEFAULT true,
    scope text NOT NULL DEFAULT 'demo' CHECK (scope IN ('demo', 'official')),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'ready', 'failed', 'deleting')),
    current_generation integer NOT NULL DEFAULT 0,
    embedding_model text,
    embedding_revision text,
    embedding_dim integer,
    stored_relpath text NOT NULL,
    original_filename text NOT NULL,
    content_type text NOT NULL DEFAULT 'application/pdf',
    size_bytes bigint,
    page_count integer,
    error text,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX documents_checksum_scope_uq ON documents (checksum_sha256, scope);
CREATE INDEX documents_status_idx ON documents (status);

CREATE TABLE chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    generation integer NOT NULL,
    seq integer NOT NULL,
    text text NOT NULL,
    page_start integer,
    page_end integer,
    section text,
    kind text NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'table')),
    token_count integer,
    embedding vector(384) NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chunks_doc_gen_idx ON chunks (document_id, generation);
CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE TABLE ingestion_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid REFERENCES documents(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('ingest', 'reindex')),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    manual_retries integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    locked_by text,
    progress jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE INDEX ingestion_jobs_queue_idx ON ingestion_jobs (status, available_at) WHERE status = 'queued';
CREATE INDEX ingestion_jobs_doc_idx ON ingestion_jobs (document_id);

CREATE TABLE settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
