"""First-run bootstrap: create tables, seed the admin account, and migrate
legacy .db/sessions/*.json chat files into the database.

Call once at web-app startup (see klyra.web.server).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

from klyra.auth.passwords import hash_password
from klyra.storage import repo
from klyra.storage.db import init_db

logger = logging.getLogger(__name__)


def ensure_bootstrap(legacy_sessions_dir: Path | None = None) -> None:
    init_db()
    admin = _ensure_admin()
    if legacy_sessions_dir is not None:
        _migrate_legacy_sessions(legacy_sessions_dir, admin.id)


def _ensure_admin():
    password = os.environ.get("KLYRA_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
    admin_user = repo.get_user_by_username("admin")
    
    if admin_user:
        # Sync password if explicitly set in .env
        if os.environ.get("KLYRA_ADMIN_PASSWORD"):
            repo.update_user(admin_user.id, password_hash=hash_password(password))
        return admin_user

    admin = repo.create_user(
        username="admin",
        password_hash=hash_password(password),
        role="admin",
        courses=[],
    )
    if os.environ.get("KLYRA_ADMIN_PASSWORD"):
        logger.info("Bootstrap admin account 'admin' created (password from KLYRA_ADMIN_PASSWORD).")
    else:
        # Print prominently — this is the only time the generated password is shown.
        print("=" * 60)
        print("  Created bootstrap admin account")
        print(f"    username: admin")
        print(f"    password: {password}")
        print("  (Set KLYRA_ADMIN_PASSWORD to control this explicitly.)")
        print("=" * 60)
    return admin


def _migrate_legacy_sessions(sessions_dir: Path, owner_id: str) -> None:
    """Import .db/sessions/*.json (pre-RBAC format) into chat_sessions,
    assigning them to the bootstrap admin. Non-destructive: files are kept."""
    if not sessions_dir.exists():
        return
    migrated = 0
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = data.get("session_id") or path.stem
        if repo.get_chat_session(sid) is not None:
            continue
        repo.save_chat_session(
            session_id=sid,
            owner_id=owner_id,
            title=data.get("title", "New Chat"),
            ui_history=data.get("ui_history", []),
            gemini_history=data.get("gemini_history", {}),
        )
        if data.get("pinned"):
            repo.set_chat_session_pinned(sid, True)
        migrated += 1
    if migrated:
        logger.info("Migrated %d legacy JSON chat session(s) into the database (owner=admin).", migrated)