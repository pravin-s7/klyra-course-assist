"""Auth + admin user-management routes for the Klyra web app.

Mounted by klyra.web.server:
- POST /api/auth/login      → set session cookie
- POST /api/auth/logout     → clear cookie
- GET  /api/auth/me         → current user (401 if not logged in)
- GET  /api/admin/users     → list users (admin)
- POST /api/admin/users     → create user (admin)
- PUT  /api/admin/users/{id} → update role/courses/password (admin)
- DELETE /api/admin/users/{id} → delete user (admin)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from klyra.auth.dependencies import get_current_user, require_admin
from klyra.auth.passwords import hash_password, verify_password
from klyra.auth.sessions import SESSION_COOKIE, encode_session_token
from klyra.storage import repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

VALID_ROLES = ("admin", "instructor")


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6)
    full_name: str | None = None
    email: str | None = None
    role: str = "instructor"
    courses: list[str] = []


class UpdateUserRequest(BaseModel):
    username: str | None = Field(default=None, min_length=2, max_length=64)
    role: str | None = None
    courses: list[str] | None = None
    password: str | None = Field(default=None, min_length=6)
    full_name: str | None = None
    email: str | None = None


def _user_payload(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role,
        "courses": repo.user_courses(user),
        "created_at": getattr(user, "created_at", None),
    }


@router.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    user = repo.get_user_by_username(req.username)
    if user is None or not verify_password(req.password, user.password_hash):
        logger.warning("Failed login attempt for username='%s'", req.username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    response.set_cookie(
        SESSION_COOKIE,
        encode_session_token(user.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )
    logger.info("User '@%s' (%s) signed in", user.username, user.role)
    return {"status": "ok", "user": _user_payload(user)}


@router.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


@router.get("/api/auth/me")
def me(user=Depends(get_current_user)):
    return _user_payload(user)


# ── Admin: user management ─────────────────────────────────────────

@router.get("/api/admin/users")
def list_all_users(user=Depends(get_current_user)):
    require_admin(user)
    return {"users": [_user_payload(u) for u in repo.list_users()]}


@router.post("/api/admin/users", status_code=201)
def create_new_user(req: CreateUserRequest, user=Depends(get_current_user)):
    require_admin(user)
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {VALID_ROLES}")
    clean_username = req.username.strip()
    if repo.get_user_by_username(clean_username):
        raise HTTPException(status_code=409, detail=f"Username '{clean_username}' already exists")
    clean_email = (req.email or "").strip() or None
    if clean_email and repo.get_user_by_email(clean_email):
        raise HTTPException(status_code=409, detail=f"Email '{clean_email}' is already registered to another user")
    created = repo.create_user(
        username=clean_username,
        password_hash=hash_password(req.password),
        role=req.role,
        courses=[c.lower() for c in req.courses],
        full_name=(req.full_name or "").strip() or None,
        email=clean_email,
    )
    logger.info("Admin '@%s' created user '@%s' (role=%s)", user.username, created.username, created.role)
    return _user_payload(created)


@router.put("/api/admin/users/{user_id}")
def update_existing_user(user_id: str, req: UpdateUserRequest, user=Depends(get_current_user)):
    require_admin(user)
    if req.role is not None and req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {VALID_ROLES}")
    fields = {}
    if req.username is not None:
        clean_username = req.username.strip()
        existing_u = repo.get_user_by_username(clean_username)
        if existing_u is not None and existing_u.id != user_id:
            raise HTTPException(status_code=409, detail=f"Username '{clean_username}' already exists")
        fields["username"] = clean_username
    if req.role is not None:
        fields["role"] = req.role
    if req.courses is not None:
        fields["courses"] = [c.lower() for c in req.courses]
    if req.password is not None:
        fields["password_hash"] = hash_password(req.password)
    if req.full_name is not None or "full_name" in req.model_fields_set:
        fields["full_name"] = (req.full_name or "").strip() or None
    if req.email is not None or "email" in req.model_fields_set:
        clean_email = (req.email or "").strip() or None
        if clean_email:
            existing_e = repo.get_user_by_email(clean_email)
            if existing_e is not None and existing_e.id != user_id:
                raise HTTPException(status_code=409, detail=f"Email '{clean_email}' is already registered to another user")
        fields["email"] = clean_email
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    updated = repo.update_user(user_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("Admin '@%s' updated user '@%s'", user.username, updated.username)
    return _user_payload(updated)


@router.delete("/api/admin/users/{user_id}")
def delete_existing_user(user_id: str, user=Depends(get_current_user)):
    require_admin(user)
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    target = repo.get_user_by_id(user_id)
    if not repo.delete_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("Admin '@%s' deleted user '@%s'", user.username, target.username if target else user_id)
    return {"status": "deleted"}