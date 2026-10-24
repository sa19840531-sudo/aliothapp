"""Create page navigation alias vector tables.

Revision ID: 20260724_0017
Revises: 20260723_0017
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op

revision = "20260724_0017"
down_revision = "20260723_0017"
branch_labels = None
depends_on = None

_TERM_UPDATED_AT_TRIGGER = "trg_page_nav_alias_term_updated_at"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS page_nav_alias_term (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            canonical_code text NOT NULL,
            page_subtype_code text,
            label text NOT NULL,
            description text,
            normalized_text text NOT NULL,
            embedding_text text NOT NULL,
            source_category text,
            source_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
            tab_position_stats jsonb NOT NULL DEFAULT '{}'::jsonb,
            sample_count integer NOT NULL DEFAULT 0,
            product_count integer NOT NULL DEFAULT 0,
            confidence numeric(5, 4) NOT NULL DEFAULT 0.0000,
            review_status text NOT NULL DEFAULT 'candidate',
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_page_nav_alias_term_review_status
                CHECK (review_status IN ('candidate', 'confirmed', 'rejected')),
            CONSTRAINT ck_page_nav_alias_term_confidence
                CHECK (confidence >= 0 AND confidence <= 1)
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE page_nav_alias_term IS
        'Navigation alias terms used by page detection and page exploration'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN page_nav_alias_term.description IS
        'Readable alias purpose, source, and review note'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN page_nav_alias_term.embedding_text IS
        'Stable local embedding input text for the alias term'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN page_nav_alias_term.review_status IS
        'Alias review status: candidate, confirmed, or rejected'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_page_nav_alias_term_normalized
        ON page_nav_alias_term (
            normalized_text,
            canonical_code,
            COALESCE(page_subtype_code, '')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_page_nav_alias_term_canonical_enabled
        ON page_nav_alias_term (canonical_code, enabled)
        """
    )
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_TERM_UPDATED_AT_TRIGGER} ON page_nav_alias_term
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TERM_UPDATED_AT_TRIGGER}
        BEFORE UPDATE ON page_nav_alias_term
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS page_nav_alias_embedding (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            alias_term_id uuid NOT NULL
                REFERENCES page_nav_alias_term(id) ON DELETE CASCADE,
            embedding_model text NOT NULL,
            embedding vector(1536) NOT NULL,
            content_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (alias_term_id, embedding_model, content_hash)
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE page_nav_alias_embedding IS
        'Local embedding vectors for page navigation alias recall'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN page_nav_alias_embedding.embedding IS
        '1536-dimensional local hash embedding of page_nav_alias_term.embedding_text'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN page_nav_alias_embedding.content_hash IS
        'SHA-256 hash of the embedding input text for idempotent writes'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_page_nav_alias_embedding_alias_term
        ON page_nav_alias_embedding (alias_term_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_page_nav_alias_embedding_vector
        ON page_nav_alias_embedding USING hnsw (embedding vector_cosine_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS page_nav_alias_embedding")
    op.execute(f"DROP TRIGGER IF EXISTS {_TERM_UPDATED_AT_TRIGGER} ON page_nav_alias_term")
    op.execute("DROP TABLE IF EXISTS page_nav_alias_term")