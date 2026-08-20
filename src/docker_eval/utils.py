"""
Utility functions for Docker evaluations.

Provides cleanup guards, logging setup, disk space monitoring, and other helpers.
"""

import atexit
import logging
import os
import shutil
import signal
import sys
from typing import Optional

from .config import LOG_FORMAT, LOG_DATE_FORMAT, MIN_DISK_SPACE_GB


class CleanupGuard:
    """
    Ensures cleanup happens even on crashes, SIGTERM, or SIGINT.

    Registers atexit hooks and signal handlers to guarantee cleanup
    of Docker resources when the process exits or receives signals.
    """

    def __init__(self, runner):
        """
        Initialize cleanup guard.

        Args:
            runner: Runner instance with a cleanup() method
        """
        self.runner = runner
        self.cleanup_called = False

    def register(self):
        """Register cleanup hooks for normal exit and signals."""
        # Cleanup on normal exit
        atexit.register(self._cleanup)

        # Cleanup on signals (Ctrl+C, kill)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _cleanup(self):
        """Execute cleanup if not already called."""
        if not self.cleanup_called:
            self.cleanup_called = True
            try:
                self.runner.cleanup(force=True)
            except Exception as e:
                # Log but don't fail - we're already exiting
                logging.error(f"Cleanup error in guard: {e}")

    def _signal_handler(self, sig, frame):
        """Handle signals by cleaning up and exiting."""
        logging.warning(f"Received signal {sig}, cleaning up...")
        self._cleanup()
        # Exit with standard signal exit code
        sys.exit(128 + sig)


def setup_logging(output_file: Optional[str] = None, level: int = logging.INFO) -> logging.Logger:
    """
    Configure logging for evaluation.

    Args:
        output_file: Optional path to log file (also logs to console)
        level: Logging level (default: INFO)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger("docker_eval")
    logger.setLevel(level)

    # Clear existing handlers
    logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    logger.addHandler(console_handler)

    # File handler (if specified)
    if output_file:
        file_handler = logging.FileHandler(output_file, mode='a')
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        logger.addHandler(file_handler)

    return logger


def check_disk_space(path: str = "/tmp") -> tuple[bool, float]:
    """
    Check if sufficient disk space is available.

    Args:
        path: Path to check disk space for (default: /tmp)

    Returns:
        Tuple of (sufficient: bool, available_gb: float)
    """
    stat = shutil.disk_usage(path)
    available_gb = stat.free / (1024 ** 3)  # Convert bytes to GB
    sufficient = available_gb >= MIN_DISK_SPACE_GB

    return sufficient, available_gb


def verify_cleanup(student_name: str, logger: logging.Logger) -> bool:
    """
    Verify that all Docker resources for a student have been cleaned up.

    Args:
        student_name: Student identifier
        logger: Logger instance

    Returns:
        True if cleanup was complete, False otherwise
    """
    import docker

    try:
        client = docker.from_env()

        # Check for containers with student name
        containers = client.containers.list(
            all=True,
            filters={"name": f"eval_{student_name}"}
        )

        if containers:
            logger.warning(f"Found {len(containers)} orphaned containers for {student_name}")
            for container in containers:
                logger.warning(f"  - {container.name} ({container.status})")
            return False

        # Check for networks
        networks = client.networks.list(filters={"name": f"eval_{student_name}"})
        if networks:
            logger.warning(f"Found {len(networks)} orphaned networks for {student_name}")
            return False

        logger.info(f"✓ Cleanup verified for {student_name}")
        return True

    except Exception as e:
        logger.error(f"Error verifying cleanup: {e}")
        return False


def format_exit_code(exit_code: int) -> str:
    """
    Format exit code with human-readable description.

    Args:
        exit_code: Numeric exit code

    Returns:
        Formatted string with code and description
    """
    from .config import (
        EXIT_SUCCESS, EXIT_PARTIAL_FAIL, EXIT_CRITICAL,
        EXIT_TIMEOUT, EXIT_CLEANUP_ERROR
    )

    descriptions = {
        EXIT_SUCCESS: "Success",
        EXIT_PARTIAL_FAIL: "Partial failure",
        EXIT_CRITICAL: "Critical failure (build/startup)",
        EXIT_TIMEOUT: "Timeout exceeded",
        EXIT_CLEANUP_ERROR: "Cleanup error"
    }

    desc = descriptions.get(exit_code, "Unknown")
    return f"{exit_code} ({desc})"


# --- identifiants ecrits par l'apprenant -------------------------------------
# Les apprenants declarent rarement leurs identifiants proprement. Ils les
# mettent dans le service, mais aussi -- et souvent -- en dur dans leur fichier
# de test, avec l'URL et parfois un jeton. Les lire la evite de refuser une
# copie qui fonctionne, et l'endroit ou on les trouve devient un constat a
# rendre a l'apprenant.

CREDENTIAL_PATTERNS = (
    (r"""USERS\s*=\s*\{[^}]*?["']([^"']+)["']\s*:\s*["']([^"']+)["']""", "USERS"),
    (r"""VALID_USERNAME\s*=\s*["']([^"']+)["'][\s\S]{0,400}?VALID_PASSWORD\s*=\s*["']([^"']+)["']""", "VALID_*"),
    (r"""USERNAME\s*=\s*["']([^"']+)["'][\s\S]{0,400}?PASSWORD\s*=\s*["']([^"']+)["']""", "USERNAME/PASSWORD"),
    (r"""auth\s*=\s*\(\s*["']([^"']+)["']\s*,\s*["']([^"']+)["']\s*\)""", "auth=()"),
    (r"""["']username["']\s*:\s*["']([^"']+)["'][\s\S]{0,200}?["']password["']\s*:\s*["']([^"']+)["']""", "payload json"),
    # APP_USERNAME = os.getenv("APP_USERNAME", "user123") -- declaration propre
    # par variable d'environnement, avec un defaut. Frequent, et jusqu'ici rate.
    (r"""(?:APP_|)USERNAME\s*=\s*os\.getenv\([^,]+,\s*["']([^"']+)["']\)[\s\S]{0,400}?(?:APP_|)PASSWORD\s*=\s*os\.getenv\([^,]+,\s*["']([^"']+)["']\)""", "os.getenv"),
    (r"""(?:APP_|)USERNAME\s*=\s*os\.environ\.get\([^,]+,\s*["']([^"']+)["']\)[\s\S]{0,400}?(?:APP_|)PASSWORD\s*=\s*os\.environ\.get\([^,]+,\s*["']([^"']+)["']\)""", "os.environ.get"),
)


def looks_like_test_file(path: str) -> bool:
    """Le chemin designe-t-il un fichier de test ?"""
    lowered = path.replace("\\", "/").lower()
    return "test" in os.path.basename(lowered) or "/tests/" in f"/{lowered}"


def find_credentials_in_texts(texts):
    """Premiers identifiants trouves dans une suite de (chemin, contenu).

    Les fichiers sont examines dans l'ordre recu : appeler avec le service en
    premier pour qu'il prime sur les tests.

    Rend un dict `username`, `password`, `source`, `in_tests`, ou None.
    """
    import re as _re

    for path, content in texts:
        if not content:
            continue
        for pattern, label in CREDENTIAL_PATTERNS:
            match = _re.search(pattern, content)
            if not match:
                continue
            return {
                "username": match.group(1),
                "password": match.group(2),
                "source": f"{path} ({label})",
                "in_tests": looks_like_test_file(path),
            }
    return None
