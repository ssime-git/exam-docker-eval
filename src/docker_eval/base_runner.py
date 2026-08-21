"""
Base runner class for Docker evaluations.

Defines the common interface that all runners must implement.
"""

import logging
import os
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

    _TREE_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}

    def describe_submission(self, max_entries: int = 80) -> dict:
        """Ce que l'apprenant a rendu : l'arborescence, et son README.

        Un relecteur doit pouvoir juger le rendu sans le telecharger. C'est
        souvent la premiere chose qu'il regarde -- ce qui a ete livre, et ce
        que l'apprenant en dit.
        """
        entries = []
        truncated = False
        root = self.eval_dir

        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in self._TREE_SKIP_DIRS)
            relative_dir = os.path.relpath(current, root)
            depth = 0 if relative_dir == "." else relative_dir.count(os.sep) + 1
            if depth > 3:
                dirs[:] = []
                continue
            for name in sorted(files):
                if len(entries) >= max_entries:
                    truncated = True
                    break
                path = os.path.join(relative_dir, name) if relative_dir != "." else name
                try:
                    size = os.path.getsize(os.path.join(current, name))
                except OSError:
                    size = None
                entries.append((path, size))
            if truncated:
                break

        readme = ""
        readme_name = ""
        for candidate in ("README.md", "README.txt", "README", "readme.md"):
            for current, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in self._TREE_SKIP_DIRS]
                if candidate in files:
                    full = os.path.join(current, candidate)
                    try:
                        readme = open(full, encoding="utf-8", errors="ignore").read(6000)
                        readme_name = os.path.relpath(full, root)
                    except OSError:
                        pass
                    break
            if readme:
                break

        return {
            "entries": entries,
            "truncated": truncated,
            "readme_name": readme_name,
            "readme": readme,
        }

    def record_submission_step(self) -> None:
        """Consigner le contenu du rendu comme premiere etape du scratchpad."""
        described = self.describe_submission()
        lines = [
            f"{path}" + (f"  ({size} o)" if size is not None else "")
            for path, size in described["entries"]
        ]
        if described["truncated"]:
            lines.append("... (liste tronquee)")
        output = "\n".join(lines) or "(rendu vide)"
        if described["readme"]:
            output += (
                f"\n\n--- {described['readme_name']} ---\n{described['readme'].strip()}"
            )
        self.record_step(
            "Ce que l'apprenant a rendu",
            command=f"find . -type f   # dans {os.path.basename(self.eval_dir)}",
            output=output,
            note=(
                "Arborescence du rendu et contenu du README, pour juger sur pieces "
                "sans telecharger l'archive."
            ),
        )

    def echec(self, message: str, exit_code: int = 2, **extra) -> dict:
        """Résultat d'échec qui n'oublie pas la trace.

        Les retours anticipés du runner rendaient un dict sans `steps`. Le
        scratchpad se retrouvait vide, et un relecteur ne voyait ni ce que
        l'apprenant avait rendu ni pourquoi on s'était arrêté — au moment
        précis où il en a le plus besoin.
        """
        self.record_step("L'évaluation s'interrompt", output=message, exit_code=exit_code)
        resultat = {
            "success": False,
            "error": message,
            "exit_code": exit_code,
            "steps": self.steps,
        }
        resultat.update(extra)
        return resultat

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

    def record_exam_image(self, image: str) -> None:
        """Marquer une image comme venant de la copie : à supprimer au ménage."""
        if image:
            self._exam_images = getattr(self, "_exam_images", set())
            self._exam_images.add(image)

    def remove_exam_images(self) -> None:
        """Supprimer les images chargées ou construites pour cette correction.

        Sur une machine de correction partagée, chaque copie laisse sinon des
        centaines de Mo d'images derrière elle — les conteneurs sont nettoyés,
        pas ce qui a servi à les lancer.
        """
        import docker
        images = getattr(self, "_exam_images", set())
        if not images:
            return
        client = docker.from_env()
        for image in sorted(images):
            try:
                client.images.remove(image, force=True)
                self.logger.info(f"✓ Image d'examen supprimée : {image}")
            except Exception as erreur:
                self.logger.warning(f"Image {image} non supprimée : {erreur}")

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
