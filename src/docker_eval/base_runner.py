"""
Base runner class for Docker evaluations.

Defines the common interface that all runners must implement.
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Any


class BaseRunner(ABC):
    """
    Abstract base class for Docker evaluation runners.

    All evaluation runners (Compose, BentoML) must inherit from this class
    and implement the required methods.
    """

    def __init__(self, student_name: str, eval_dir: str, timeout: int, logger: logging.Logger):
        """
        Initialize base runner.

        Args:
            student_name: Student identifier
            eval_dir: Directory containing student's extracted submission
            timeout: Maximum execution time in seconds
            logger: Logger instance
        """
        self.student_name = student_name
        self.eval_dir = eval_dir
        self.timeout = timeout
        self.logger = logger
        self.resources = []  # Track created resources for cleanup
        # Trace pas a pas de l'evaluation. Chaque entree porte ce qui a ete
        # tente, ce que ca a produit, et combien de temps ca a pris. C'est la
        # matiere du scratchpad : sans elle, un relecteur doit recouper trois
        # fichiers pour reconstituer une execution.
        self.steps: list = []

    def record_step(
        self,
        title: str,
        command: str = "",
        output: str = "",
        exit_code=None,
        note: str = "",
        duration=None,
    ) -> None:
        """Consigner une etape de l'evaluation.

        `title` est ce qu'on cherchait a faire, en une ligne lisible.
        `command` est ce qui a reellement ete lance, `output` ce que ca a rendu.
        `note` est la remarque a afficher au relecteur avant la commande.
        """
        self.steps.append(
            {
                "title": title,
                "command": command,
                "output": output,
                "exit_code": exit_code,
                "note": note,
                "duration_seconds": round(duration, 2) if duration is not None else None,
            }
        )

    @abstractmethod
    def run_evaluation(self) -> Dict[str, Any]:
        """
        Execute the evaluation.

        Returns:
            Dictionary with evaluation results:
            {
                "success": bool,
                "exit_code": int,
                "logs": str (optional),
                "error": str (optional)
            }
        """
        pass

    @abstractmethod
    def cleanup(self, force: bool = False):
        """
        Clean up all Docker resources.

        Args:
            force: If True, ignore errors and force cleanup
        """
        pass

    def _log_execution_start(self):
        """Log evaluation start with context."""
        self.logger.info("=" * 80)
        self.logger.info(f"Starting evaluation for {self.student_name}")
        self.logger.info(f"Evaluation directory: {self.eval_dir}")
        self.logger.info(f"Timeout: {self.timeout} seconds")
        self.logger.info("=" * 80)

    def _log_execution_end(self, result: Dict[str, Any]):
        """Log evaluation end with results."""
        self.logger.info("=" * 80)
        self.logger.info(f"Evaluation completed for {self.student_name}")
        self.logger.info(f"Success: {result.get('success', False)}")
        if "exit_code" in result:
            self.logger.info(f"Exit code: {result['exit_code']}")
        if "error" in result:
            self.logger.error(f"Error: {result['error']}")
        self.logger.info("=" * 80)
