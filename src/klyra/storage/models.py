"""ORM models for users, chat sessions, and generated-file ownership."""
from __future__ import annotations

from sqlalchemy import Boolean, Column, String, Text, JSON
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.postgresql import JSONB

Base = declarative_base()

class JSONBType(JSON):
    """Platform-independent JSONB type. Uses JSONB on PostgreSQL, JSON on others."""
    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(JSONB())
        else:
            return dialect.type_descriptor(JSON())


class UserRecord(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)  # uuid4 hex
    username = Column(String, unique=True, nullable=False, index=True)
    full_name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    password_hash = Column(String, nullable=False)  # pbkdf2: "salt$hash"
    # "admin" | "instructor"
    role = Column(String, nullable=False, default="instructor")
    # JSON list of subject slugs the user may work with (admin: ignored)
    courses_json = Column("courses", JSONBType, nullable=False, default=list)
    created_at = Column(String, nullable=False)
    modified_at = Column(String, nullable=True)


class ChatSessionRecord(Base):
    __tablename__ = "chat_sessions"

    session_id = Column(String, primary_key=True)
    owner_id = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False, default="New Chat")
    pinned = Column(Boolean, nullable=False, default=False)
    created_at = Column(String, nullable=False)
    modified_at = Column(String, nullable=True)
    ui_history_json = Column(JSONBType, nullable=False, default=list)
    gemini_history_json = Column(JSONBType, nullable=False, default=dict)


class FileOwnershipRecord(Base):
    """Who generated a file under docs/{subject}/generated/...

    Keyed by path relative to the docs/ root, e.g.
    "dbms/generated/midterm_qp.md". Source materials (uploads) are gated by
    course assignment; this table gates *generated outputs* per user.
    """

    __tablename__ = "file_ownership"

    rel_path = Column(String, primary_key=True)
    owner_id = Column(String, nullable=False, index=True)
    subject = Column(String, nullable=False, index=True)
    created_at = Column(String, nullable=False)
    modified_at = Column(String, nullable=True)