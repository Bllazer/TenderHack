"""Initial STE catalog and analysis storage.

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("categories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("categories.id")),
        sa.Column("ktru_code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("is_leaf", sa.Boolean(), nullable=False),
        sa.Column("source_version", sa.String(128)))
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"])
    op.create_index("ix_categories_ktru_code", "categories", ["ktru_code"])
    op.create_table("attributes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False, unique=True),
        sa.Column("value_type", sa.String(32), nullable=False),
        sa.Column("unit", sa.String(64)),
        sa.Column("aliases", sa.JSON(), nullable=False))
    op.create_table("category_attributes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("category_id", sa.String(36), sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attribute_id", sa.String(36), sa.ForeignKey("attributes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("allowed_values", sa.JSON(), nullable=False),
        sa.UniqueConstraint("category_id", "attribute_id"))
    op.create_index("ix_category_attributes_category_id", "category_attributes", ["category_id"])
    op.create_index("ix_category_attributes_attribute_id", "category_attributes", ["attribute_id"])
    op.create_table("ste",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("category_id", sa.String(36), sa.ForeignKey("categories.id")),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "external_id"))
    op.create_table("analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ste_id", sa.String(36), sa.ForeignKey("ste.id")),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("input_attributes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("corrected_ktru_code", sa.String(64)),
        sa.Column("corrected_attributes", sa.JSON(), nullable=False),
        sa.Column("comment", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_feedback_analysis_created", "feedback", ["analysis_id", "created_at"])


def downgrade() -> None:
    op.drop_table("feedback")
    op.drop_table("analyses")
    op.drop_table("ste")
    op.drop_table("category_attributes")
    op.drop_table("attributes")
    op.drop_table("categories")
