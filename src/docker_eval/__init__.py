"""
Docker Evaluation Package

Provides guaranteed cleanup for Docker-based student evaluations using Testcontainers.
Replaces manual docker compose and docker run commands with Python wrappers that
ensure containers are always cleaned up, even on crashes or timeouts.
"""

__version__ = "1.0.0"

from .compose_runner import ComposeRunner
from .bentoml_runner import BentoMLRunner

__all__ = ["ComposeRunner", "BentoMLRunner"]
