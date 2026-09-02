"""Pydantic request/response schemas for the HTTP API.

Kept separate from the CLI's argparse definitions (see cli.py) because the
API needs JSON schema/validation, but both ultimately build inputs for the
same services/ functions - see approach.md for why services/ is the shared
boundary rather than either of these calling crews directly.
"""
