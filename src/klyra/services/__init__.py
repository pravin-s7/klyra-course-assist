"""Business-logic layer: one function per crew capability.

This is the single place that turns plain arguments into crew `inputs` and
calls `kickoff()`. Both `cli.py` and `api/routes/*.py` call *only* these
functions - neither talks to a `crews.*` module directly - so the two entry
points can never drift apart (see approach.md).
"""
