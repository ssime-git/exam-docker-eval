"""
Base runner class for Docker evaluations.

Defines the common interface that all runners must implement.
"""

import contextlib
import logging
import os
import re
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, Any


VERROU_IMAGES_DEFAUT = "/tmp/exam-docker-eval-images.lock"


@contextlib.contextmanager
def verrou_images():
    """Un seul chargement d'image à la fois sur la machine.

    15/10 : 459845 et 458990 livraient toutes deux
    `admission_prediction_service:latest`. Corrigées en même temps, le
    `docker load` de l'une réécrivait le tag de l'autre entre son chargement
    et son lancement. Sous ce verrou, « charger puis retaguer au nom de la
    copie » ne s'entrelace jamais avec une autre correction. Un verrou de
    fichier : indépendant du stockage d'images de Docker (overlay2 ou
    containerd), relâché par le noyau si le processus meurt.
    """
    import fcntl
    chemin = os.environ.get("EXAM_IMAGES_VERROU", VERROU_IMAGES_DEFAUT)
    with open(chemin, "a") as fichier:
        fcntl.flock(fichier, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fichier, fcntl.LOCK_UN)


def tag_propre_a_la_copie(student_name: str, image: str) -> str:
    """Le tag sous lequel cette copie lance son image : eval_<copie>/<nom>:<tag>."""
    copie = re.sub(r"[^a-z0-9_.-]", "_", str(student_name).lower())
    if not image or image.startswith("sha256:"):
        nom, tag = "image", "latest"
    else:
        nom, _, tag = image.rpartition(":") if ":" in image.split("/")[-1] else (image, "", "latest")
    nom = re.sub(r"[^a-z0-9_.-]", "_", nom.lower()) or "image"
    tag = re.sub(r"[^a-z0-9_.-]", "_", (tag or "latest").lower())
    return f"eval_{copie}/{nom}:{tag}"


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

    def _host_arch(self) -> str:
        """Architecture de la machine, dans le vocabulaire de Docker."""
        import platform as _platform

        return {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(
            _platform.machine(), _platform.machine()
        )

    def _qemu_available(self) -> bool:
        """binfmt_misc expose-t-il des gestionnaires QEMU ?

        Sans eux, une image d'une autre architecture ne peut pas s'executer,
        quel que soit le --platform demande.
        """
        try:
            entries = os.listdir("/proc/sys/fs/binfmt_misc")
        except OSError:
            return False
        return any(name.startswith("qemu-") for name in entries)

    def consigner_environnement(self, python_tests, runner_installe: bool = True) -> None:
        """Consigner les faits d'environnement connus avant de lancer quoi que
        ce soit : quel Python installe et teste la copie, lequel elle déclare,
        ses images de base, la machine. Une étape qui sort en 0 : ce sont des
        faits, pas un verdict (#322)."""
        from . import environnement
        self.record_step(
            "Environnement du harnais",
            command="lecture de la configuration du harnais et des déclarations de la copie",
            output="\n".join(environnement.lignes_de_depart(
                self.eval_dir, python_tests, self._host_arch(), self._qemu_available(),
                runner_installe=runner_installe)),
            exit_code=0,
            note=("Faits d'environnement qui peuvent décider d'une attribution : "
                  "un échec qui tient au Python ou à la plateforme du harnais n'est pas "
                  "une faute de la copie."),
        )
        self._etape_environnement = self.steps[-1]

    def completer_environnement(self, lignes: list) -> None:
        """Ajouter à l'étape d'environnement ce qu'on n'apprend qu'au démarrage
        (image exécutée, émulation, limites des conteneurs)."""
        etape = getattr(self, "_etape_environnement", None)
        if etape is not None and lignes:
            etape["output"] = etape["output"] + "\n" + "\n".join(lignes)

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

    def adopter_image(self, chargee: str) -> str:
        """Retaguer une image tout juste chargée au nom de la copie, et rendre
        ce tag : c'est lui qu'on lance et qu'on supprime. À appeler sous
        verrou_images(), dans le même bloc que le chargement.

        Le tag partagé (celui de l'archive) est noté avec l'image qu'il
        désigne : au ménage, il n'est retiré que s'il la désigne encore.
        """
        if not chargee:
            return chargee
        propre = tag_propre_a_la_copie(self.student_name, chargee)
        subprocess.run(["docker", "tag", chargee, propre], check=True, capture_output=True, timeout=60)
        identifiant = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", propre],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        if not chargee.startswith("sha256:"):
            self._tags_partages = getattr(self, "_tags_partages", {})
            self._tags_partages[chargee] = identifiant
        self.record_exam_image(propre)
        self.logger.info(f"Image de la copie : {propre} ({identifiant[:19]}), chargée sous {chargee}")
        return propre

    def remove_exam_images(self) -> list[str]:
        """Supprimer les images chargées ou construites pour cette correction.

        Sur une machine de correction partagée, chaque copie laisse sinon des
        centaines de Mo d'images derrière elle — les conteneurs sont nettoyés,
        pas ce qui a servi à les lancer.
        """
        import docker
        import docker.errors
        images = getattr(self, "_exam_images", set())
        if not images:
            return []
        client = docker.from_env()
        errors = []
        for image in sorted(images):  # copie triée : on retire de l'ensemble en route
            try:
                client.images.remove(image, force=True)
                self.logger.info(f"✓ Image d'examen supprimée : {image}")
                images.discard(image)
            except docker.errors.ImageNotFound:
                # Déjà partie (ménage appelé deux fois, autre processus) : le
                # but est atteint. Un warning ici finissait en dernière ligne
                # de la sortie et le correcteur le citait comme cause d'échec.
                self.logger.info(f"Image d'examen déjà absente : {image}")
                images.discard(image)
            except Exception as erreur:
                self.logger.warning(f"Image {image} non supprimée : {erreur}")
                errors.append(f"image {image}: {erreur}")
        # Le tag partagé n'est retiré que s'il désigne encore notre image :
        # une autre copie a pu le recharger depuis.
        with verrou_images():
            for tag, identifiant in sorted(getattr(self, "_tags_partages", {}).items()):
                actuel = subprocess.run(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                    capture_output=True, text=True, timeout=60,
                ).stdout.strip()
                if actuel and actuel == identifiant:
                    subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=120)
                    self.logger.info(f"✓ Tag partagé retiré : {tag}")
                elif actuel:
                    self.logger.info(f"Tag partagé {tag} laissé : il désigne l'image d'une autre copie")
        return errors

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
