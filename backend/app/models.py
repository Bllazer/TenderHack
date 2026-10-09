from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("ktru_code"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)
    ktru_code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(512))
    is_leaf: Mapped[bool] = mapped_column(Boolean, default=False)
    source_version: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Attribute(Base):
    __tablename__ = "attributes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    value_type: Mapped[str] = mapped_column(String(32))  # text, number, boolean
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)


class CategoryAttribute(Base):
    __tablename__ = "category_attributes"
    __table_args__ = (UniqueConstraint("category_id", "attribute_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    attribute_id: Mapped[str] = mapped_column(ForeignKey("attributes.id", ondelete="CASCADE"), index=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_values: Mapped[list] = mapped_column(JSON, default=list)


class STE(Base):
    __tablename__ = "ste"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(1024))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Analysis(Base):
    __tablename__ = "analyses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ste_id: Mapped[str | None] = mapped_column(ForeignKey("ste.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(1024))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_attributes: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    engine_version: Mapped[str] = mapped_column(String(64), default="catalog-baseline-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_analysis_created", "analysis_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"))
    verdict: Mapped[str] = mapped_column(String(32))
    corrected_ktru_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    corrected_attributes: Mapped[list] = mapped_column(JSON, default=list)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
