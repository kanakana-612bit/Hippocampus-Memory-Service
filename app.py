"""Canonical Ver0.2 entry point; old APIs and implicit migration are not loaded."""
from v02.api import app, create_app

__all__ = ["app", "create_app"]
