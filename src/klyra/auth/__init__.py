"""Auth + RBAC for the Klyra web app.

- PBKDF2 password hashing (hashlib, no new dependencies)
- HMAC-signed session cookies (itsdangerous-free, stdlib only)
- Roles: "admin" (everything) and "instructor" (assigned courses only)
- Bootstrap: first startup creates the admin account
"""
from klyra.auth.passwords import hash_password, verify_password  # noqa: F401
from klyra.auth.sessions import (  # noqa: F401
    SESSION_COOKIE,
    decode_session_token,
    encode_session_token,
)