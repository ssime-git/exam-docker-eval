"""
BentoML runner for evaluations.

Handles BentoML evaluations that use docker load + docker run pattern
with pre-built Docker images.
"""

import json
import logging
import os
import re
import shutil
import socket
import tarfile
from urllib.parse import urlparse
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import time
from typing import Dict, Any, Optional

import requests
from testcontainers.core.container import DockerContainer

from .base_runner import BaseRunner
from .utils import find_credentials_in_texts, looks_like_test_file
from .config import BENTOML_PORT, BENTOML_READY_PATTERN, API_STARTUP_TIMEOUT, EMULATION_TIMEOUT_FACTOR


class BentoMLRunner(BaseRunner):
    """
    Runner for BentoML evaluations.

    Manages BentoML Docker image evaluation with guaranteed cleanup
    using testcontainers' GenericContainer.
    """

    def __init__(
        self,
        student_name: str,
        eval_dir: str,
        timeout: int,
        logger: logging.Logger,
        image_tar: Optional[str] = None,
    ):
        """
        Initialize BentoML runner.

        Args:
            student_name: Student identifier
            eval_dir: Directory containing student's submission
            timeout: Maximum execution time in seconds
            logger: Logger instance
            image_tar: Path to Docker image tar file (optional)
        """
        super().__init__(student_name, eval_dir, timeout, logger)
        self.image_tar = image_tar
        self.container = None
        self.image_name = None
        self.cli_container_id: Optional[str] = None
        self.container_name = f"bentoml_eval_{student_name}".replace(" ", "_").lower()

    def run_evaluation(self) -> Dict[str, Any]:
        """
        Execute BentoML evaluation.

        Steps:
        1. Load Docker image from tar (if provided) OR auto-containerize .bento file
        2. Start container with port mapping
        3. Wait for API to be ready
        4. Run API tests (login, predict)
        5. Extract authentication token
        6. Clean up

        Returns:
            Dictionary with evaluation results
        """
        self._log_execution_start()

        run_started = time.time()
        self._emulating = False
        self.credentials_source = None
        self.credentials_in_tests = False
        # Ce que l'apprenant a rendu ouvre la trace : c'est la premiere chose
        # qu'un relecteur regarde.
        self.record_submission_step()
        emulated_platform = None
        auto_containerized = False
        image_loaded = False
        container_started = False
        api_ready = False

        try:
            if self._should_use_image_only_mode():
                self.logger.info(
                    "Detected image+tests only submission; using docker CLI fallback mode"
                )
                return self._run_image_only_evaluation()

            # Step 1: Load Docker image or auto-containerize .bento
            if self.image_tar:
                self.logger.info(f"Loading Docker image from {self.image_tar}")
                self.image_name = self._load_docker_image()
                self.record_exam_image(self.image_name)
                if not self.image_name:
                    return {
                        "success": False,
                        "error": "Failed to load Docker image",
                        "exit_code": 2,
                        "image_loaded": False,
                    }
                image_loaded = True
            else:
                # Prefer student-provided .bento in eval dir over any local cached image.
                bento_file = self._find_bento_file()
                if bento_file:
                    self.logger.info(
                        "No Docker image tar provided; auto-containerizing detected .bento file..."
                    )
                    bento_result = self._auto_containerize_bento()
                    if bento_result["success"]:
                        self.image_name = bento_result["image_name"]
                        auto_containerized = True
                        image_loaded = True
                        self.logger.info(
                            f"✓ Auto-containerized .bento file: {self.image_name}"
                        )
                    else:
                        return self.echec(
                            bento_result.get("error", "Failed to auto-containerize .bento file"),
                            image_loaded=False,
                        )
                else:
                    # Fallback to existing local BentoML image.
                    self.image_name = self._find_bentoml_image()
                    if not self.image_name:
                        source_root = self._find_bento_source()
                        if source_root:
                            construit = self._build_bento_from_source(source_root)
                            if construit.get("success"):
                                bento_result = self._auto_containerize_bento(
                                    bento_file_path=construit.get("bento")
                                )
                                if bento_result.get("success"):
                                    self.image_name = bento_result["image_name"]
                                    auto_containerized = True
                                    image_loaded = True
                                    self.logger.info(f"✓ Image construite depuis la source : {self.image_name}")
                            else:
                                self.logger.warning(construit.get("error", "construction en échec"))
                    if not self.image_name:
                        return self.echec(
                            "Aucune image Docker, aucun .bento, et aucune source constructible "
                            "dans le rendu",
                            image_loaded=False,
                        )
                    image_loaded = True

            # Step 2: Create and start container
            self.logger.info(f"Starting BentoML container: {self.image_name}")
            self.record_step(
                "Charger l'image de l'apprenant",
                command=f"docker load -i {os.path.basename(self.image_tar)}" if self.image_tar else "",
                output=f"Image chargee : {self.image_name}",
                note="L'archive contenait une image Docker deja construite.",
            )
            # Toujours publier sur un port ephemere. Tester la disponibilite du
            # port 3000 ne marchait pas : la sonde interrogeait le loopback du
            # processus, alors que le bind se fait sur l'hote Docker. En mode
            # dockerise les deux different, et le repli ne se declenchait jamais.
            port_mode = "ephemere"
            # Le port du service se lit dans l'image, pas dans une convention.
            # 459145 livrait un uvicorn sur 8000 : la sonde interrogeait le
            # 3000 de BentoML et l'API etait declaree muette alors qu'elle
            # tournait tres bien.
            service_port = self._service_port_from_image()
            self.container = (
                DockerContainer(self.image_name)
                .with_exposed_ports(service_port)
                .with_name(self.container_name)
            )
            platform = self._platform_for_image()
            if platform:
                self.logger.info(f"Image d'une autre architecture : execution en {platform}")
                self.container = self.container.with_kwargs(platform=platform)
                emulated_platform = platform
                self._emulating = True
                self.record_step(
                    "Adapter l'execution a l'architecture de l'image",
                    command=f"docker run --platform {platform} ...",
                    output=f"Image en {self._image_arch()}, machine en {self._host_arch()}",
                    note=(
                        "L'image ne cible pas l'architecture de la machine de correction. "
                        "Elle est executee sous emulation pour que la copie soit evaluee "
                        "dans les memes conditions que les autres."
                    ),
                )

            # Start container
            self.container.start()
            container_started = True
            self.logger.info("Container started successfully")

            host_port = self.container.get_exposed_port(service_port)
            published_host = self._resolve_published_host()
            base_url = f"http://{published_host}:{host_port}"
            self.logger.info(f"BentoML API available at {base_url}")
            self.record_step(
                "Demarrer le service",
                command=f"docker start {self.container_name}",
                output=f"Service joignable sur {base_url}",
                note="Port hote attribue par Docker, pour ne pas dependre de ce qui tourne deja.",
            )

            # Step 3: Wait for API to be ready
            probe_started = time.time()
            self._wait_for_api_ready(base_url)
            api_ready = True
            self.record_step(
                "Attendre que l'API reponde",
                command=f"GET {base_url}/readyz  (puis /healthz, /livez, /openapi.json, /docs, /)",
                output="La sonde de disponibilite a repondu",
                duration=time.time() - probe_started,
            )

            # Step 4: Run API tests
            tests_started = time.time()
            test_results = self._run_api_tests(base_url)
            credentials_source = getattr(self, "credentials_source", None)
            if credentials_source:
                in_tests = getattr(self, "credentials_in_tests", False)
                self.record_step(
                    "Retrouver les identifiants du service",
                    output=f"Lus dans : {credentials_source}",
                    note=(
                        "Les identifiants sont ecrits en dur dans un fichier de test. "
                        "La correction s'en sert pour ne pas refuser une copie qui "
                        "fonctionne, mais c'est une pratique a signaler a l'apprenant."
                        if in_tests
                        else "Identifiants declares par le service de l'apprenant."
                    ),
                )
            self.record_step(
                "Appeler les endpoints attendus",
                command=f"POST {base_url}/login  puis  POST {base_url}/predict",
                output=(
                    f"/login : {'valide' if test_results.get('login_passed') else 'echec'} | "
                    f"/predict : {'valide' if test_results.get('predict_passed') else 'echec'}"
                ),
                exit_code=0 if test_results.get("all_passed") else 1,
                duration=time.time() - tests_started,
            )

            # Step 5: Extract token (if login successful)
            token = test_results.get("token")

            # Step 6: Run pytest tests while API is running
            pytest_results = self._run_pytest(base_url)

            result = {
                "success": test_results.get("all_passed", False),
                "exit_code": 0 if test_results.get("all_passed") else 1,
                "base_url": base_url,
                "token": token,
                "test_results": test_results,
                "pytest_results": pytest_results,
                "auto_containerized": auto_containerized,
                "image_name": self.image_name,
                "image_loaded": image_loaded,
                "container_started": container_started,
                "api_ready": api_ready,
                "port_mode": port_mode,
                "host_port": host_port,
                "emulated_platform": emulated_platform,
                "host_arch": self._host_arch(),
                "credentials_source": self.credentials_source,
                "credentials_in_tests": self.credentials_in_tests,
                "steps": self.steps,
            }

            self._log_execution_end(result)
            return result

        except TimeoutError as e:
            elapsed = time.time() - run_started
            diagnostics = self._collect_diagnostics()
            died, container_exit_code = self._container_exit_state()
            if died:
                # Le conteneur s'est arrete de lui-meme : ce n'est pas un delai
                # depasse, c'est un echec d'execution. Le dire, et dire pourquoi.
                error_msg = self._describe_container_death(container_exit_code, elapsed)
                result_exit_code = 2
            else:
                error_msg = (
                    f"API muette apres {elapsed:.1f}s "
                    f"(sonde limitee a {API_STARTUP_TIMEOUT}s) : {e}"
                )
                result_exit_code = 3
            self.logger.error(error_msg)
            result = {
                "success": False,
                "error": error_msg,
                "elapsed_seconds": round(elapsed, 1),
                "container_exited": died,
                "container_exit_code": container_exit_code,
                "diagnostics": diagnostics,
                "exit_code": result_exit_code,
                "image_name": self.image_name,
                "image_loaded": image_loaded,
                "container_started": container_started,
                "api_ready": api_ready,
                "container_logs": self._capture_container_logs(),
            }
            # L'echec est une etape comme une autre : il doit se lire dans la
            # trace, au bon endroit, avec ce que le conteneur a dit.
            self.record_step(
                "L'evaluation s'interrompt",
                output=result["container_logs"] or error_msg,
                exit_code=container_exit_code,
                note=error_msg,
                duration=elapsed,
            )
            result["steps"] = self.steps
            result["fault"] = self._attribute_fault(diagnostics, result["container_logs"])
            self.logger.info(f"Attribution de la faute : {result['fault']}")
            self._log_execution_end(result)
            return result

        except Exception as e:
            elapsed = time.time() - run_started
            diagnostics = self._collect_diagnostics()
            error_msg = f"Execution error apres {elapsed:.1f}s : {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            result = {
                "success": False,
                "error": error_msg,
                "elapsed_seconds": round(elapsed, 1),
                "diagnostics": diagnostics,
                "exit_code": 2,
                "image_name": self.image_name,
                "image_loaded": image_loaded,
                "container_started": container_started,
                "api_ready": api_ready,
                "container_logs": self._capture_container_logs(),
            }
            # L'echec est une etape comme une autre : il doit se lire dans la
            # trace, au bon endroit, avec ce que le conteneur a dit.
            self.record_step(
                "L'evaluation s'interrompt",
                output=result["container_logs"] or error_msg,
                exit_code=diagnostics.get("container_exit_code"),
                note=error_msg,
                duration=elapsed,
            )
            result["steps"] = self.steps
            result["fault"] = self._attribute_fault(diagnostics, result["container_logs"])
            self.logger.info(f"Attribution de la faute : {result['fault']}")
            self._log_execution_end(result)
            return result

        finally:
            # Guaranteed cleanup
            self.cleanup(force=True)

    def _should_use_image_only_mode(self) -> bool:
        """
        Detect stripped submissions that only provide Docker image + tests.
        """
        if not self.image_tar or not os.path.isfile(self.image_tar):
            return False
        tests_dir = os.path.join(self.eval_dir, "tests")
        has_tests = os.path.isdir(tests_dir)
        has_service_src = os.path.isfile(os.path.join(self.eval_dir, "src", "service.py"))
        has_bentofile = os.path.isfile(os.path.join(self.eval_dir, "bentofile.yaml"))
        return has_tests and not has_service_src and not has_bentofile

    def _load_docker_image_cli(self) -> Optional[str]:
        """
        Load Docker image with `docker load -i` and extract image tag if possible.
        """
        try:
            result = subprocess.run(
                ["docker", "load", "-i", self.image_tar],
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            if result.returncode != 0:
                self.logger.error(f"docker load failed: {output.strip()}")
                return None

            match = re.search(r"Loaded image:\s*(.+)", output)
            if match:
                image_name = match.group(1).strip()
                self.logger.info(f"✓ Image loaded via docker CLI: {image_name}")
                return image_name

            id_match = re.search(r"Loaded image ID:\s*(.+)", output)
            if id_match:
                image_id = id_match.group(1).strip()
                self.logger.info(f"✓ Image loaded via docker CLI: {image_id}")
                return image_id

            self.logger.error("docker load succeeded but no image tag/id found in output")
            return None
        except Exception as e:
            self.logger.error(f"docker load CLI error: {e}")
            return None

    def _run_image_only_evaluation(self) -> Dict[str, Any]:
        """
        Fallback evaluator for submissions that only provide image + tests.
        Uses docker CLI: load image, run container, execute tests.
        """
        self._log_execution_start()

        image_loaded = False
        container_started = False
        api_ready = False
        port_mode = "fixed"

        try:
            self.image_name = self._load_docker_image_cli()
            self.record_exam_image(self.image_name)
            if not self.image_name:
                if self.image_tar and self.image_tar.endswith(".bento"):
                    self.logger.warning(
                        "docker load failed for .bento artifact; trying auto-containerization fallback"
                    )
                    bento_result = self._auto_containerize_bento()
                    if not bento_result.get("success"):
                        return {
                            "success": False,
                            "error": bento_result.get(
                                "error",
                                "Failed to load Docker image and failed to auto-containerize .bento",
                            ),
                            "exit_code": 2,
                            "image_loaded": False,
                        }
                    self.image_name = bento_result["image_name"]
                else:
                    return {
                        "success": False,
                        "error": "Failed to load Docker image with docker load -i",
                        "exit_code": 2,
                        "image_loaded": False,
                    }
            image_loaded = True

            # Laisser Docker choisir le port hote, puis le relire. Choisir
            # nous-memes supposait de savoir ce qui est libre sur l'hote, ce
            # qu'un processus conteneurise ne peut pas savoir.
            port_mode = "ephemere"
            run_cmd = [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                self.container_name,
            ]
            platform = self._platform_for_image()
            if platform:
                self.logger.info(f"Image d'une autre architecture : execution en {platform}")
                run_cmd += ["--platform", platform]
            service_port = self._service_port_from_image()
            run_cmd += [
                "-p",
                f"{service_port}",
                self.image_name,
            ]
            run_result = subprocess.run(
                run_cmd, capture_output=True, text=True, timeout=60
            )
            if run_result.returncode == 0:
                host_port = self._published_host_port_cli(service_port)
                self.logger.info(f"Port hote publie : {host_port}")
            if run_result.returncode != 0:
                output = (run_result.stdout or "") + "\n" + (run_result.stderr or "")
                return {
                    "success": False,
                    "error": f"docker run failed: {output.strip()}",
                    "exit_code": 2,
                    "image_name": self.image_name,
                    "image_loaded": image_loaded,
                    "container_started": False,
                }

            self.cli_container_id = (run_result.stdout or "").strip() or self.container_name
            container_started = True
            published_host = self._resolve_published_host()
            base_url = f"http://{published_host}:{host_port}"
            self.logger.info(f"Container started via docker CLI at {base_url}")

            self._wait_for_api_ready(base_url)
            api_ready = True

            test_results = self._run_api_tests(base_url)
            pytest_results = self._run_pytest(base_url)
            pytest_success = (
                pytest_results.get("executed", False)
                and pytest_results.get("total", 0) > 0
                and pytest_results.get("failed", 0) == 0
                and pytest_results.get("errors", 0) == 0
            )

            success = test_results.get("all_passed", False) or pytest_success
            exit_code = 0 if success else 1

            result = {
                "success": success,
                "exit_code": exit_code,
                "base_url": base_url,
                "token": test_results.get("token"),
                "test_results": test_results,
                "pytest_results": pytest_results,
                "auto_containerized": False,
                "image_name": self.image_name,
                "image_loaded": image_loaded,
                "container_started": container_started,
                "api_ready": api_ready,
                "port_mode": port_mode,
                "host_port": str(host_port),
                "execution_mode": "image_only_cli",
            }
            self._log_execution_end(result)
            return result

        except Exception as e:
            error_msg = f"Execution error: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            result = {
                "success": False,
                "error": error_msg,
                "exit_code": 2,
                "image_name": self.image_name,
                "image_loaded": image_loaded,
                "container_started": container_started,
                "api_ready": api_ready,
                "execution_mode": "image_only_cli",
            }
            self._log_execution_end(result)
            return result
        finally:
            self.cleanup(force=True)

    def _load_docker_image(self) -> Optional[str]:
        """
        Load Docker image from tar file.

        Returns:
            Image name if successful, None otherwise
        """
        import docker

        try:
            client = docker.from_env()

            with open(self.image_tar, "rb") as f:
                self.logger.info("Loading image (this may take a minute)...")
                images = client.images.load(f.read())

                if images:
                    image = images[0]
                    # Get image name (first tag or ID)
                    image_name = image.tags[0] if image.tags else image.id
                    self.logger.info(f"✓ Image loaded: {image_name}")
                    return image_name
                else:
                    self.logger.error("No image found in tar file")
                    return None

        except Exception as e:
            self.logger.error(f"Error loading image: {e}")
            return None

    def _find_bentoml_image(self) -> Optional[str]:
        """
        Find BentoML image in local Docker images.

        Returns:
            Image name if found, None otherwise
        """
        import docker

        try:
            client = docker.from_env()
            images = client.images.list()

            # Look for images with 'bento' in name or tags
            for image in images:
                for tag in image.tags:
                    if "bento" in tag.lower():
                        self.logger.info(f"Found BentoML image: {tag}")
                        return tag

            self.logger.warning("No BentoML image found")
            return None

        except Exception as e:
            self.logger.error(f"Error finding image: {e}")
            return None

    def _wait_for_api_ready(self, base_url: str):
        """
        Wait for BentoML API to be ready.

        Readiness is established when at least one HTTP probe responds, or when
        the TCP port is accepting connections after BentoML startup has been
        observed in container logs. Endpoint correctness is still verified by
        the API tests that follow.
        """
        self.logger.info(
            f"Waiting for API to be ready (timeout: {API_STARTUP_TIMEOUT}s)..."
        )

        parsed = urlparse(base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or BENTOML_PORT
        readiness_paths = ("/readyz", "/healthz", "/livez", "/openapi.json", "/docs", "/")
        accepted_statuses = {200, 204, 401, 403, 404}
        start_time = time.time()
        check_interval = 2
        saw_startup_signal = False

        while True:
            elapsed = time.time() - start_time

            if elapsed > API_STARTUP_TIMEOUT:
                raise TimeoutError(f"API not ready after {API_STARTUP_TIMEOUT}s")

            container_logs = ""
            if getattr(self, "container", None) is not None:
                try:
                    raw_logs = self.container.get_logs() or b""
                    if isinstance(raw_logs, bytes):
                        container_logs = raw_logs.decode("utf-8", errors="ignore")
                    else:
                        container_logs = str(raw_logs)
                except Exception:
                    container_logs = ""
            elif getattr(self, "cli_container_id", None):
                # Mode CLI : `self.container` n'est pose que par le chemin
                # testcontainers, il reste None ici. Sans cette branche
                # `saw_startup_signal` ne peut jamais devenir vrai, et le repli
                # ci-dessous -- ecrit precisement pour les demarrages lents sous
                # emulation -- est du code mort sur ce chemin. La copie 459884 a
                # ete notee 0/25 en « Live API behavior » sur un « API not ready
                # after 60s » que ce repli devait absorber.
                # Les logs se lisent par NOM de conteneur, ce qui marche dans les
                # deux modes : `docker run` recoit `--name self.container_name`.
                container_logs = self._capture_container_logs()

            if BENTOML_READY_PATTERN in container_logs or "Service loaded from Bento directory" in container_logs:
                saw_startup_signal = True

            for readiness_path in readiness_paths:
                try:
                    response = requests.get(f"{base_url}{readiness_path}", timeout=self._http_timeout(3))
                    if response.status_code in accepted_statuses:
                        self.logger.info(
                            f"✓ API readiness probe succeeded on {readiness_path} with status {response.status_code}"
                        )
                        return
                except requests.exceptions.RequestException:
                    pass

            tcp_ready = False
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                tcp_ready = sock.connect_ex((host, port)) == 0

            # Un port TCP ouvert ne veut pas dire que l'application repond : le
            # noyau accepte la connexion et le serveur la reinitialise aussitot
            # tant qu'il demarre. Mesure sous emulation : reset instantane a
            # t+0, puis 200 en 24s. Se contenter du TCP faisait echouer toutes
            # les requetes suivantes, et notait 0 une copie qui fonctionne.
            #
            # On ne retient donc ce repli que dans le dernier quart du delai,
            # quand aucune reponse HTTP n'est venue -- pour ne pas bloquer un
            # service qui refuserait vraiment toutes les routes sondees.
            if tcp_ready and saw_startup_signal and elapsed > API_STARTUP_TIMEOUT * 0.75:
                self.logger.warning(
                    "Aucune reponse HTTP, mais le port est ouvert et le demarrage "
                    "observe : on tente les tests malgre tout"
                )
                return

            self.logger.debug(f"API not ready yet (elapsed: {elapsed:.1f}s)")
            time.sleep(check_interval)

    def _iter_submission_texts(self):
        """Yield readable text files from the submission or embedded OCI layers."""
        interesting_suffixes = ('.py', '.md', '.txt', '.yaml', '.yml')

        for root, _, files in os.walk(self.eval_dir):
            for file_name in files:
                if not file_name.endswith(interesting_suffixes):
                    continue
                full_path = os.path.join(root, file_name)
                try:
                    if os.path.getsize(full_path) > 200_000:
                        continue
                    with open(full_path, encoding='utf-8', errors='replace') as handle:
                        yield full_path, handle.read()
                except Exception:
                    continue

        manifest_path = os.path.join(self.eval_dir, 'manifest.json')
        blobs_root = os.path.join(self.eval_dir, 'blobs', 'sha256')
        if not (os.path.isfile(manifest_path) and os.path.isdir(blobs_root)):
            return

        try:
            manifest = json.loads(open(manifest_path, encoding='utf-8').read())
            layers = manifest[0].get('Layers', []) if isinstance(manifest, list) and manifest else []
        except Exception:
            return

        wanted = ('README', 'readme', 'service.py', 'app.py', 'requirements.txt', 'bentofile.yaml', '/tests/', '/src/')
        for layer in layers[-8:]:
            layer_path = os.path.join(self.eval_dir, layer)
            if not os.path.isfile(layer_path):
                continue
            try:
                with tarfile.open(layer_path) as archive:
                    for member in archive.getmembers():
                        name = member.name
                        if member.isdir() or member.size > 200_000:
                            continue
                        if not any(token in name for token in wanted):
                            continue
                        try:
                            extracted = archive.extractfile(member)
                            if not extracted:
                                continue
                            yield name, extracted.read().decode('utf-8', errors='replace')
                        except Exception:
                            continue
            except Exception:
                continue

    def _extract_class_fields(self, text: str, class_name: str) -> Dict[str, Optional[str]]:
        pattern = re.compile(
            rf'class\s+{re.escape(class_name)}\s*\([^)]*BaseModel[^)]*\)\s*:\s*(.*?)(?:\nclass\s+|\ndef\s+|\n@|\Z)',
            re.S,
        )
        match = pattern.search(text)
        if not match:
            return {}
        fields: Dict[str, Optional[str]] = {}
        for raw_line in match.group(1).splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            field_match = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*([^=]+?)(?:\s*=.*)?$', line)
            if field_match:
                field_name = field_match.group(1)
                field_type = field_match.group(2).strip()
                fields[field_name] = field_type
        return fields

    def _sample_value_for_field(self, field_name: str) -> Any:
        samples = {
            'gre_score': 330,
            'toefl_score': 115,
            'university_rating': 5,
            'sop': 4.5,
            'lor': 4.5,
            'cgpa': 9.0,
            'research': 1,
            'authorization': 'Bearer {token}',
            'token': '{token}',
        }
        normalized = field_name.lower()
        if normalized in samples:
            return samples[normalized]
        if normalized.endswith('score'):
            return 1
        if normalized.startswith('is_') or normalized in {'research', 'holiday', 'workingday'}:
            return 1
        return 1

    def _discover_api_contract(self) -> Dict[str, Any]:
        contract: Dict[str, Any] = {
            'source': 'fallback',
            'usernames': ['admin', 'testuser'],
            'passwords': ['admin', 'testpass'],
            'login_wrappers': [None, 'credentials'],
            'predict_wrappers': [None, 'request'],
            'predict_bodies': [
                {
                    'GRE': 330,
                    'TOEFL': 115,
                    'University_Rating': 5,
                    'SOP': 4.5,
                    'LOR': 4.5,
                    'CGPA': 9.0,
                    'Research': 1,
                },
                {
                    'gre_score': 320,
                    'toefl_score': 110,
                    'university_rating': 4,
                    'sop': 4.0,
                    'lor': 4.0,
                    'cgpa': 8.5,
                    'research': 1,
                },
            ],
        }

        # Les identifiants sont cherches sur l'ensemble du rendu, le service
        # d'abord, les tests ensuite. Un apprenant qui les ecrit en dur dans son
        # test a quand meme droit a une correction -- et a le savoir.
        texts = list(self._iter_submission_texts())

        # Un rendu qui ne livre que l'image n'expose aucun source sur le disque.
        # Sans lui, ni les identifiants ni la forme des payloads ne sont
        # connus, et le correcteur teste a cote : mauvais noms de champs,
        # mauvais nom de parametre. On lit donc le service dans l'image.
        if not any(re.search(r"def\s+predict\s*\(", text) for _, text in texts):
            in_image = self._read_service_from_image()
            if in_image:
                self.logger.info(
                    f"Aucun service sur le disque ; lecture de {len(in_image)} fichier(s) dans l'image"
                )
                texts = list(texts) + in_image

        # Le contrat le plus fiable est celui que l'apprenant documente
        # lui-meme : un exemple curl vers /predict dans le README donne les
        # noms de champs exacts. 459145 (FastAPI, pas de `def predict(self`)
        # documentait GRE_Score/TOEFL_Score... et recevait quand meme les
        # payloads generiques — 422 sur toute la ligne.
        for source_path, text in texts:
            for bloc in re.findall(
                r"/predict[\s\S]{0,300}?-d\s+'(\{[\s\S]*?\})'", text
            ):
                try:
                    corps = json.loads(bloc)
                except ValueError:
                    continue
                # Un corps enveloppe (ex. {"request": {...}}) porte aussi le
                # nom du wrapper.
                if isinstance(corps, dict) and corps:
                    contract['predict_bodies'].insert(0, corps)
                    contract['predict_documented_source'] = source_path
                    self.logger.info(f"Payload /predict lu dans {source_path}")
                break
            if contract.get('predict_documented_source'):
                break

        ordered = sorted(texts, key=lambda item: looks_like_test_file(item[0]))
        found = find_credentials_in_texts(ordered)
        if found:
            contract['usernames'] = [found['username']]
            contract['passwords'] = [found['password']]
            contract['credentials_source'] = found['source']
            contract['credentials_in_tests'] = found['in_tests']
            self.credentials_source = found['source']
            self.credentials_in_tests = found['in_tests']
            self.logger.info(
                f"Identifiants lus dans {found['source']}"
                + (" -- ecrits en dur dans un test" if found['in_tests'] else "")
            )

        for source_path, text in texts:
            if contract['source'] == 'fallback':
                contract['source'] = source_path

            login_signature = re.search(r'def\s+login\s*\(\s*self\s*,\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*([a-zA-Z_][a-zA-Z0-9_]*)', text)
            if login_signature:
                login_wrapper = login_signature.group(1)
                if login_wrapper not in contract['login_wrappers']:
                    contract['login_wrappers'].insert(0, login_wrapper)

            predict_signature = re.search(r'def\s+predict\s*\(\s*self\s*,\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*([a-zA-Z_][a-zA-Z0-9_]*)', text)
            if predict_signature:
                predict_wrapper = predict_signature.group(1)
                # Un parametre positionnel (marque par `/`) n'est PAS enveloppe
                # par BentoML : les champs vont a la racine du corps. Verifie
                # sur une copie reelle -- le corps nu rend 200, l'enveloppe 400.
                positional = re.search(
                    r'def\s+predict\s*\([\s\S]{0,300}?^\s*/\s*,', text, re.M
                )
                if predict_wrapper not in contract['predict_wrappers'] and not positional:
                    contract['predict_wrappers'].insert(0, predict_wrapper)
                request_type = predict_signature.group(2)
                request_fields = self._extract_class_fields(text, request_type)
                if request_fields:
                    nested_body = {}
                    request_payload = {}
                    for field_name, field_type in request_fields.items():
                        normalized = (field_type or '').strip()
                        if normalized and normalized in text and field_name != 'authorization':
                            nested_fields = self._extract_class_fields(text, normalized)
                            if nested_fields:
                                nested_body = {
                                    nested_name: self._sample_value_for_field(nested_name)
                                    for nested_name in nested_fields
                                }
                                request_payload[field_name] = nested_body
                                continue
                        request_payload[field_name] = self._sample_value_for_field(field_name)
                    if request_payload:
                        if contract.get('predict_documented_source'):
                            # Le payload documente par l'apprenant prime sur
                            # celui reconstruit depuis les types.
                            contract['predict_bodies'].append(request_payload)
                        else:
                            contract['predict_bodies'] = [request_payload]

        return contract

    def _run_api_tests(self, base_url: str) -> Dict[str, Any]:
        """
        Run API endpoint tests.

        Args:
            base_url: Base URL of the API

        Returns:
            Dictionary with test results
        """
        results = {
            "login_passed": False,
            "predict_passed": False,
            "token": None,
            "all_passed": False,
            "login_attempt_statuses": [],
            "predict_attempt_statuses": [],
        }

        contract = self._discover_api_contract()
        self.logger.info("Discovered API contract source: %s", contract.get("source", "fallback"))

        # Test 1: Login endpoint with adaptive payloads derived from the submission when possible.
        login_candidates = []
        for username in contract.get("usernames", ["admin", "testuser"]):
            for password in contract.get("passwords", ["admin", "testpass"]):
                for wrapper in contract.get("login_wrappers", [None, "credentials"]):
                    payload = {"username": username, "password": password}
                    if wrapper:
                        payload = {wrapper: payload}
                    if payload not in login_candidates:
                        login_candidates.append(payload)

        for payload in login_candidates:
            try:
                self.logger.info("Testing /login endpoint with adaptive payload...")
                login_response = requests.post(
                    f"{base_url}/login",
                    json=payload,
                    timeout=self._http_timeout(10),
                )
                results["login_attempt_statuses"].append(login_response.status_code)
                if login_response.status_code != 200:
                    continue
                data = login_response.json()
                token = data.get("token") or data.get("access_token")
                if token:
                    results["login_passed"] = True
                    results["token"] = token
                    self.logger.info(
                        f"✓ Login successful, token: {token[:20]}..."
                    )
                    break
            except Exception as e:
                # En warning, pas en debug : sans ca on ne voit pas la
                # difference entre "refuse" et "n'a jamais repondu".
                self.logger.warning(
                    f"Requete /login en echec ({type(e).__name__}): {str(e)[:120]}"
                )

        if not results["login_passed"]:
            self.logger.error(
                "Login failed for all supported payload variants (status codes: %s)",
                results["login_attempt_statuses"],
            )

        # Test 2: Predict endpoint (requires token), reusing the discovered wrapper when possible.
        if results["token"]:
            token = results["token"]
            predict_bodies = contract.get("predict_bodies", [])
            if not predict_bodies:
                predict_bodies = [
                    {
                        "GRE": 330,
                        "TOEFL": 115,
                        "University_Rating": 5,
                        "SOP": 4.5,
                        "LOR": 4.5,
                        "CGPA": 9.0,
                        "Research": 1,
                    },
                    {
                        "gre_score": 320,
                        "toefl_score": 110,
                        "university_rating": 4,
                        "sop": 4.0,
                        "lor": 4.0,
                        "cgpa": 8.5,
                        "research": 1,
                    },
                ]
            for payload in predict_bodies:
                normalized_payload = json.loads(json.dumps(payload).replace('{token}', token))
                # Le token obtenu au login doit accompagner la requete. Il ne
                # l'etait pas : les en-tetes partaient vides et /predict rendait
                # 401, ce qui etait compte contre l'apprenant alors que son
                # endpoint verifie correctement l'autorisation.
                auth_headers = {"Authorization": f"Bearer {token}"} if token else {}
                request_variants = []
                for wrapper in contract.get("predict_wrappers", [None, "request"]):
                    if wrapper:
                        request_variants.append(({wrapper: normalized_payload}, dict(auth_headers)))
                    else:
                        request_variants.append((normalized_payload, dict(auth_headers)))
                if isinstance(normalized_payload, dict) and 'authorization' in normalized_payload:
                    body = dict(normalized_payload)
                    auth_value = body.pop('authorization')
                    request_variants.append((body, {"Authorization": auth_value}))
                for body, headers in request_variants:
                    try:
                        self.logger.info("Testing /predict endpoint with adaptive payload...")
                        predict_response = requests.post(
                            f"{base_url}/predict",
                            json=body,
                            headers=headers,
                            timeout=self._http_timeout(10),
                        )
                        results["predict_attempt_statuses"].append(
                            predict_response.status_code
                        )
                        if predict_response.status_code != 200:
                            continue
                        data = predict_response.json()
                        # Un 200 sur /predict est une reponse valide. Exiger une
                        # cle precise revient a deviner comment l'apprenant a
                        # nomme son champ : un service qui repond correctement
                        # avec un autre nom etait recale pour rien.
                        results["predict_passed"] = True
                        results["predict_response"] = str(data)[:400]
                        self.logger.info(f"✓ Prediction successful : {str(data)[:200]}")
                        break
                    except Exception as e:
                        self.logger.debug(
                            f"Predict probe failed for payload variant: {e}"
                        )
                if results["predict_passed"]:
                    break

            if not results["predict_passed"]:
                self.logger.error(
                    "Predict failed for all supported payload variants (status codes: %s)",
                    results["predict_attempt_statuses"],
                )

        # Overall result
        results["all_passed"] = results["login_passed"] and results["predict_passed"]

        return results

    def _default_gateway_ip(self) -> Optional[str]:
        """Return the default gateway seen from the current process, when available."""
        try:
            with open("/proc/net/route", encoding="utf-8") as handle:
                for line in handle:
                    fields = line.strip().split()
                    if len(fields) > 2 and fields[1] == "00000000":
                        gateway_hex = fields[2]
                        return ".".join(
                            str(int(gateway_hex[index:index + 2], 16))
                            for index in range(6, -2, -2)
                        )
        except Exception:
            return None
        return None

    def _resolve_published_host(self) -> str:
        """Pick a host that can reach ports published by the Docker daemon."""
        override = os.getenv("PI_CORRECTOR_DOCKER_HOST")
        candidates = []
        if override:
            candidates.append(override)

        in_container = os.path.exists("/.dockerenv")
        gateway = self._default_gateway_ip()
        if in_container:
            candidates.extend(["host.docker.internal", gateway, "localhost"])
        else:
            candidates.extend(["localhost", "host.docker.internal", gateway])

        seen = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                socket.gethostbyname(candidate)
                self.logger.info(f"Using published Docker host: {candidate}")
                return candidate
            except socket.gaierror:
                continue

        return "localhost"

    def _find_tests_path(self) -> Optional[str]:
        """Find a tests directory or test files in the project."""
        tests_dir = os.path.join(self.eval_dir, "tests")
        if os.path.isdir(tests_dir):
            return tests_dir

        for root, _, files in os.walk(self.eval_dir):
            for filename in files:
                if filename.startswith("test_") and filename.endswith(".py"):
                    return root
                if filename.endswith("_test.py"):
                    return root

        return None

    def _project_root_for_tests(self, tests_path: str) -> str:
        """Le dossier depuis lequel les tests de l'apprenant peuvent s'importer."""
        normalise = os.path.normpath(tests_path)
        if os.path.isdir(tests_path):
            parent = os.path.dirname(normalise)
        else:
            parent = os.path.dirname(os.path.dirname(normalise))
        # `parent == normalise` arrive à la racine du système : plus rien à
        # remonter, on reste dans le répertoire d'évaluation.
        if not parent or parent == normalise or not os.path.isdir(parent):
            return self.eval_dir
        return parent

    def _prepare_tests_for_dynamic_base_url(
        self, tests_path: str, base_url: str
    ) -> str:
        """Rewrite hardcoded BASE_URL values in place to honour BENTOML_BASE_URL.

        Sur place, et non dans une copie : les tests d'un apprenant se situent
        par rapport a `__file__` — un conftest qui remonte vers `../src` pour
        importer son code, ou qui lance `PROJECT_ROOT/src/simple_server.py`,
        casse des que les fichiers changent de dossier. Le repertoire
        d'evaluation est deja une copie de travail extraite de l'archive et
        supprimee a la fin ; la modifier ne touche a rien de l'apprenant.
        """
        dst_tests_path = tests_path if os.path.isdir(tests_path) else os.path.dirname(tests_path)

        base_url_pattern = re.compile(
            r'(^\s*BASE_URL\s*=\s*)(["\'])http://(?:127\.0\.0\.1|localhost):\d+(["\'])',
            re.MULTILINE,
        )

        for root, _, files in os.walk(dst_tests_path):
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                filepath = os.path.join(root, filename)
                with open(filepath, encoding="utf-8") as handle:
                    content = handle.read()

                new_content = base_url_pattern.sub(
                    r'\1os.getenv("BENTOML_BASE_URL", \2http://127.0.0.1:3000\3)',
                    content,
                )
                if new_content != content:
                    if "import os" not in new_content:
                        if re.search(r"^from __future__ import .+$", new_content, re.MULTILINE):
                            new_content = re.sub(
                                r"^(from __future__ import .+\n)",
                                r"\1import os\n",
                                new_content,
                                count=1,
                                flags=re.MULTILINE,
                            )
                        else:
                            new_content = "import os\n" + new_content
                    with open(filepath, "w", encoding="utf-8") as handle:
                        handle.write(new_content)
                    self.logger.info(
                        f"Rewrote hardcoded BASE_URL in tests for dynamic port: {filepath}"
                    )

        self.logger.info(
            f"Tests patched in place at {dst_tests_path} (base_url={base_url})"
        )
        return dst_tests_path

    def _read_junit_report(self, chemin: str):
        """Lire le compte réel des tests dans le rapport JUnit de pytest.

        Compter à la regex sur la sortie texte se trompe dès que la collecte
        échoue : pytest n'écrit alors aucun « N passed » et le total tombe à
        zéro, ce qui se confond avec une copie sans tests.
        """
        if not os.path.isfile(chemin):
            return None
        try:
            racine = ET.parse(chemin).getroot()
        except ET.ParseError:
            return None

        suites = [racine] if racine.tag == "testsuite" else racine.findall("testsuite")
        total = failed = errors = skipped = 0
        for suite in suites:
            total += int(suite.get("tests", 0))
            failed += int(suite.get("failures", 0))
            errors += int(suite.get("errors", 0))
            skipped += int(suite.get("skipped", 0))
        passed = max(0, total - failed - errors - skipped)
        return passed, failed, errors, total

    def _run_pytest(self, base_url: str) -> Dict[str, Any]:
        """Run pytest suite against the running API."""
        results: Dict[str, Any] = {
            "executed": False,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "total": 0,
            "pass_rate": 0,
            "tests_path": None,
            "output_file": None,
            "reason": None,
        }

        tests_path = self._find_tests_path()
        if not tests_path:
            # L'apprenant n'a pas rendu de tests. C'est un constat sur la copie,
            # pas une limite de l'évaluation : la note 0 est méritée.
            results["reason"] = "No tests directory found"
            results["tests_present"] = False
            results["evaluable"] = True
            return results
        results["tests_present"] = True

        # Le requirements.txt vit dans le projet de l'apprenant, qui est
        # rarement a la racine du repertoire d'evaluation : le chercher
        # seulement a la racine revenait a lancer les tests sans leurs
        # dependances.
        requirements_path = os.path.join(
            self._project_root_for_tests(tests_path), "requirements.txt"
        )
        if not os.path.isfile(requirements_path):
            requirements_path = os.path.join(self.eval_dir, "requirements.txt")
        output_file = os.path.join(self.eval_dir, "test_results.log")
        results["output_file"] = output_file

        if not shutil.which("uvx"):
            # Notre outillage manque : l'apprenant n'y est pour rien.
            results["reason"] = "uvx not available"
            results["evaluable"] = False
            return results

        command = [
            "uvx",
            "--python",
            "3.11",
            "--with",
            "pytest",
            "--with",
            "requests",
            "--with",
            "pyjwt",
            "--with",
            "setuptools",
            # L'outillage standard qu'un apprenant importe sans toujours le
            # declarer dans requirements.txt : httpx pour appeler l'API depuis
            # les tests, fastapi et uvicorn quand son conftest lance un serveur
            # de test local. Sur 458784, les 7 tests d'integration echouaient
            # sur `No module named 'fastapi'` — leur serveur ne demarrait
            # jamais. Fournir les manquants standards note ce que les tests
            # valent ; la declaration manquante se corrige dans le feedback.
            # Convention d'examen declarable (pi_test_packages, #49).
        ] + [arg for paquet in (os.environ.get("EXAM_TEST_PACKAGES")
                                or "httpx,pytest-asyncio,fastapi,uvicorn").split(",")
             if paquet.strip() for arg in ("--with", paquet.strip())]
        if os.path.isfile(requirements_path):
            command.extend(["--with-requirements", requirements_path])
        patched_tests_path = self._prepare_tests_for_dynamic_base_url(
            tests_path, base_url
        )
        results["tests_path"] = patched_tests_path
        projet = self._project_root_for_tests(tests_path)
        junit_path = os.path.join(self.eval_dir, "pytest_report.xml")
        if os.path.exists(junit_path):
            os.remove(junit_path)
        # pytest-asyncio en mode strict fait echouer les tests `async def` non
        # marques — ceux que l'apprenant a ecrits en pensant qu'ils tourneraient.
        # Le mode auto les execute au lieu de les compter en erreur.
        command.extend(
            [
                "pytest",
                patched_tests_path,
                "-v",
                "--tb=short",
                "-o",
                "asyncio_mode=auto",
                f"--junit-xml={junit_path}",
            ]
        )

        self.logger.info("Running pytest against running API...")
        env = os.environ.copy()
        env["BENTOML_BASE_URL"] = base_url
        env["BENTOML_PORT"] = base_url.rsplit(":", 1)[-1]
        # Les apprenants propres lisent une variable d'environnement — mais
        # chacun la sienne. 459404 lisait BASE_URL, on n'exportait que
        # BENTOML_BASE_URL : son repli localhost:3000 partait en connexion
        # refusée et ses 7 tests « échouaient ». Exporter les conventions
        # courantes ne coûte rien et note ce que les tests valent vraiment.
        # Convention d'examen declarable (pi_test_env, #49).
        noms_env = [n.strip() for n in (os.environ.get("EXAM_TEST_ENV")
                                        or "BASE_URL,API_URL,SERVICE_URL").split(",") if n.strip()]
        for nom in noms_env:
            env.setdefault(nom, base_url)
        # Les tests importent le code de l'apprenant, qui vit dans le projet.
        env["PYTHONPATH"] = os.pathsep.join(
            [p for p in (projet, env.get("PYTHONPATH")) if p]
        )

        result = subprocess.run(
            command,
            cwd=projet,
            capture_output=True,
            text=True,
            env=env,
        )

        if result.returncode == 5:
            # Pytest exit code 5 means nothing collected; retry with explicit python files.
            discovered_files = []
            for root, _, files in os.walk(patched_tests_path):
                for filename in files:
                    if filename.endswith(".py"):
                        discovered_files.append(os.path.join(root, filename))
            if discovered_files:
                prefixe = command[: command.index("pytest")]
                retry_command = prefixe + [
                    "pytest",
                    *discovered_files,
                    "-v",
                    "--tb=short",
                    "-o",
                    "asyncio_mode=auto",
                    f"--junit-xml={junit_path}",
                ]
                self.logger.info(
                    "Pytest collected no tests by default pattern; retrying with explicit test files"
                )
                result = subprocess.run(
                    retry_command,
                    cwd=projet,
                    capture_output=True,
                    text=True,
                    env=env,
                )

        output = (result.stdout or "") + "\n" + (result.stderr or "")
        with open(output_file, "w", encoding="utf-8") as handle:
            handle.write(output)

        compte = self._read_junit_report(junit_path)
        if compte is None:
            # Le rapport machine n'existe pas : pytest s'est arrêté avant
            # d'écrire quoi que ce soit. On retombe sur la prose, en sachant
            # qu'elle ment quand la collecte échoue.
            passed = sum(int(match) for match in re.findall(r"(\d+)\s+passed", output))
            failed = sum(int(match) for match in re.findall(r"(\d+)\s+failed", output))
            errors = sum(int(match) for match in re.findall(r"(\d+)\s+error", output))
            total = passed + failed + errors
        else:
            passed, failed, errors, total = compte
        pass_rate = int((passed * 100 / total)) if total else 0

        # Zéro test collecté sur une copie qui en contient veut dire que la
        # collecte a échoué chez nous. Noter 0 dans ce cas punit l'apprenant
        # pour notre panne : la catégorie devient non évaluable.
        evaluable = total > 0
        # Tous les tests en échec ET des erreurs de connexion dans la sortie :
        # les tests n'ont jamais atteint le service. C'est le harnais (port,
        # URL, service pas prêt), pas la copie — noter 0 serait un mensonge.
        if (
            evaluable
            and passed == 0
            and (failed + errors) == total
            and re.search(r"ConnectionError|Connection refused|NewConnectionError|ConnectionRefused", output)
        ):
            evaluable = False
            results["reason"] = (
                f"les {total} tests échouent tous en erreur de connexion : ils n'ont "
                "jamais atteint le service. Harnais en cause (URL/port), pas la copie."
            )
        if not evaluable and results.get("reason") is None:
            queue = "\n".join(output.strip().splitlines()[-15:])
            results["reason"] = (
                f"pytest n'a collecté aucun test (code {result.returncode}). "
                f"Fin de sortie :\n{queue}"
            )

        # Le fichier de sortie vit dans le repertoire de travail, qui est
        # supprime a la fin. Sans cet extrait, la revue n'a plus rien a lire
        # pour juger si un echec vient de la copie ou de nous.
        # stderr (téléchargements uv) est concaténé après stdout : sans
        # filtre, la queue ne montre que des « Downloading » et cache les
        # échecs de tests que la revue doit lire.
        lignes_utiles = [
            l for l in output.strip().splitlines()
            if not re.match(r"\s*(Downloading|Downloaded|Installed \d|Resolved \d|Prepared \d|Built |Audited )", l)
        ]
        results["output_tail"] = "\n".join(lignes_utiles[-60:])

        results.update(
            {
                "executed": True,
                "evaluable": evaluable,
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "total": total,
                "pass_rate": pass_rate,
                "exit_code": result.returncode,
            }
        )

        colon_style_refs = re.findall(r"([^\s:]+\.py:[0-9]+)", output)
        traceback_style_refs = [
            f"{path}:{line}"
            for path, line in re.findall(
                r'File "([^"]+\.py)", line ([0-9]+)',
                output,
            )
        ]
        problem_lines = sorted(set(colon_style_refs + traceback_style_refs))
        if problem_lines:
            self.logger.error("Pytest reported problematic lines:")
            for ref in problem_lines[:20]:
                self.logger.error("  - %s", ref)
        results["problem_lines"] = problem_lines[:50]

        return results

    def _collect_diagnostics(self) -> Dict[str, Any]:
        """Tout ce qu'il faut pour diagnostiquer, avant que la sandbox parte.

        Le nettoyage est garanti et doit le rester : on ne differe rien, on
        capture davantage pendant qu'il en est encore temps. Sans ca, l'agent
        qui doit etablir d'ou vient la panne n'a plus rien a inspecter.
        """
        import docker

        diag: Dict[str, Any] = {
            "host_arch": self._host_arch(),
            "qemu_available": self._qemu_available(),
        }

        try:
            client = docker.from_env()
        except Exception as exc:
            diag["docker_error"] = str(exc)
            return diag

        try:
            image = client.images.get(self.image_name)
            diag["image_arch"] = image.attrs.get("Architecture")
            diag["image_os"] = image.attrs.get("Os")
            diag["image_size_bytes"] = image.attrs.get("Size")
            diag["image_entrypoint"] = (image.attrs.get("Config") or {}).get("Entrypoint")
            diag["image_cmd"] = (image.attrs.get("Config") or {}).get("Cmd")
        except Exception as exc:
            diag["image_error"] = str(exc)

        try:
            container = client.containers.get(self.container_name)
            container.reload()
            state = container.attrs.get("State") or {}
            diag["container_status"] = container.status
            diag["container_exit_code"] = state.get("ExitCode")
            diag["container_oom_killed"] = state.get("OOMKilled")
            diag["container_error"] = state.get("Error") or None
            diag["container_started_at"] = state.get("StartedAt")
            diag["container_finished_at"] = state.get("FinishedAt")
            diag["container_ports"] = (container.attrs.get("NetworkSettings") or {}).get("Ports")
        except Exception as exc:
            diag["container_error_reading"] = str(exc)

        try:
            entries = sorted(os.listdir(self.eval_dir))
            diag["submission_entries"] = entries[:60]
            diag["submission_entry_count"] = len(entries)
        except OSError as exc:
            diag["submission_error"] = str(exc)

        return diag

    def _attribute_fault(self, diag: Dict[str, Any], container_logs: str) -> str:
        """A qui imputer l'echec : `apprenant`, `systeme`, ou `indetermine`.

        Ce champ decide de la colonne du ticket. Dans le doute on ne tranche
        pas : envoyer un REPASS sur la foi d'une supposition est le seul
        dommage irreversible de la chaine.
        """
        logs = container_logs or ""

        # Notre environnement n'a pas su executer l'image telle qu'elle est.
        if "exec format error" in logs and not diag.get("qemu_available"):
            return "systeme"
        if diag.get("container_oom_killed"):
            return "systeme"
        if "port is already allocated" in logs or "driver failed programming" in logs:
            return "systeme"
        if diag.get("docker_error") or diag.get("image_error"):
            return "systeme"

        # Le programme de l'apprenant a demarre puis a rendu la main en erreur.
        exit_code = diag.get("container_exit_code")
        if isinstance(exit_code, int) and exit_code not in (0, 125, 126, 127):
            return "apprenant"
        if isinstance(exit_code, int) and exit_code in (126, 127):
            # 126/127 : commande introuvable ou non executable dans l'image,
            # ce que l'apprenant a construit.
            return "apprenant"

        return "indetermine"

    def _http_timeout(self, base: int) -> int:
        """Delai d'une requete HTTP, allonge quand on emule.

        Sous QEMU une image d'une autre architecture repond plusieurs fois plus
        lentement. Mesure sur une copie reelle : un /login qui repond en 9s
        emule expirait sur un delai de 10s, et la copie etait notee 0 sur le
        comportement de l'API alors que son endpoint fonctionne.
        """
        if getattr(self, "_emulating", False):
            return base * EMULATION_TIMEOUT_FACTOR
        return base

    def _read_service_from_image(self):
        """Lire le code du service a l'interieur de l'image.

        Un rendu qui ne livre que l'image Docker -- cas frequent en BentoML --
        n'expose aucun source sur le disque. Les identifiants y sont pourtant,
        et sans eux le correcteur echoue au login sur une copie qui fonctionne.

        Rend une liste de (chemin, contenu), vide si rien n'est lisible.
        """
        if not self.image_name:
            return []

        command = [
            "docker", "run", "--rm", "--entrypoint", "sh",
        ]
        platform = self._platform_for_image()
        if platform:
            command += ["--platform", platform]
        command += [
            self.image_name,
            "-c",
            # Un fichier a la fois, precede de son chemin, pour rester lisible.
            "find /home/bentoml/bento/src -name '*.py' -size -200k "
            "-exec sh -c 'echo \"===FILE:$1\"; cat \"$1\"' _ {} \\; 2>/dev/null",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        except Exception as exc:
            self.logger.debug(f"Lecture du service dans l'image impossible : {exc}")
            return []

        texts = []
        current_path, buffer = None, []
        for line in (result.stdout or "").splitlines():
            if line.startswith("===FILE:"):
                if current_path:
                    texts.append((f"{current_path} (dans l'image)", "\n".join(buffer)))
                current_path, buffer = line[len("===FILE:"):], []
            elif current_path:
                buffer.append(line)
        if current_path:
            texts.append((f"{current_path} (dans l'image)", "\n".join(buffer)))
        return texts

    def _host_arch(self) -> str:
        """Architecture de la machine, dans le vocabulaire de Docker."""
        import platform as _platform

        return {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(
            _platform.machine(), _platform.machine()
        )

    def _image_arch(self) -> Optional[str]:
        """Architecture pour laquelle l'image a ete construite, si lisible."""
        import docker

        try:
            client = docker.from_env()
            return client.images.get(self.image_name).attrs.get("Architecture")
        except Exception as exc:
            self.logger.debug(f"Architecture de l'image illisible : {exc}")
            return None

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

    def _platform_for_image(self) -> Optional[str]:
        """`linux/<arch>` a passer a Docker, ou None si rien a forcer.

        On ne penalise pas un apprenant qui a construit son image sur un Mac
        Apple Silicon ou sur une machine Windows : si l'emulation est
        disponible, on execute dans l'architecture de l'image. Sinon on ne
        force rien, et l'echec est diagnostique plus loin.
        """
        image_arch = self._image_arch()
        if not image_arch or image_arch == self._host_arch():
            return None
        if not self._qemu_available():
            self.logger.warning(
                f"Image en {image_arch} sur une machine {self._host_arch()}, "
                "et aucun gestionnaire QEMU dans binfmt_misc : l'execution va echouer. "
                "Installer l'emulation avec : "
                "docker run --privileged --rm tonistiigi/binfmt --install all"
            )
            return None
        return f"linux/{image_arch}"

    def _service_port_from_image(self) -> int:
        """Le port qu'ecoute reellement le service de l'image.

        Ordre de lecture : `--port N` dans le Cmd (uvicorn, bentoml serve),
        puis ExposedPorts de la config, puis le 3000 conventionnel de BentoML.
        """
        try:
            import docker
            config = docker.from_env().images.get(self.image_name).attrs.get("Config") or {}
        except Exception:
            return BENTOML_PORT
        cmd = config.get("Cmd") or []
        for i, morceau in enumerate(cmd):
            if morceau == "--port" and i + 1 < len(cmd) and str(cmd[i + 1]).isdigit():
                return int(cmd[i + 1])
            if isinstance(morceau, str) and morceau.startswith("--port="):
                valeur = morceau.split("=", 1)[1]
                if valeur.isdigit():
                    return int(valeur)
        exposes = sorted(
            int(port.split("/")[0])
            for port in (config.get("ExposedPorts") or {})
            if port.split("/")[0].isdigit()
        )
        if exposes:
            return exposes[0]
        return BENTOML_PORT

    def _published_host_port_cli(self, container_port: int) -> int:
        """Port hote reellement publie, lu aupres de Docker."""
        result = subprocess.run(
            ["docker", "port", self.container_name, str(container_port)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # `docker port` rend par exemple "0.0.0.0:49154" (parfois plusieurs lignes)
        for line in (result.stdout or "").splitlines():
            _, _, port = line.strip().rpartition(":")
            if port.isdigit():
                return int(port)
        raise RuntimeError(
            f"Port publie introuvable pour {self.container_name}:{container_port} "
            f"({(result.stderr or '').strip()})"
        )

    def _container_exit_state(self):
        """Le conteneur tourne-t-il encore, et sinon avec quel code de sortie ?

        Renvoie (mort, code_de_sortie). `mort` vaut False si l'etat est
        indeterminable : on ne veut pas accuser un conteneur qu'on n'a pas pu
        inspecter.
        """
        import docker

        try:
            client = docker.from_env()
            container = client.containers.get(self.container_name)
            container.reload()
            if container.status == "running":
                return False, None
            code = (container.attrs.get("State") or {}).get("ExitCode")
            return True, code
        except Exception as exc:
            self.logger.debug(f"Etat du conteneur indeterminable : {exc}")
            return False, None

    def _describe_container_death(self, exit_code, elapsed: float) -> str:
        """Message d'erreur fonde sur ce que le conteneur a reellement fait."""
        logs = self._capture_container_logs() or ""
        last = next(
            (line.strip() for line in reversed(logs.splitlines()) if line.strip()),
            "",
        )

        cause = ""
        if "exec format error" in logs:
            # Cas frequent et parfaitement diagnosticable : image construite
            # pour une autre architecture que celle de la machine de correction.
            cause = self._describe_architecture_mismatch()

        parts = [
            f"Le conteneur s'est arrete apres {elapsed:.1f}s"
            + (f" (code {exit_code})" if exit_code is not None else "")
        ]
        if cause:
            parts.append(cause)
        if last:
            parts.append(f"Derniere sortie du conteneur : {last}")
        return ". ".join(parts)

    def _describe_architecture_mismatch(self) -> str:
        """Compare l'architecture de l'image a celle de la machine."""
        import platform

        import docker

        host = {"x86_64": "amd64", "aarch64": "arm64"}.get(
            platform.machine(), platform.machine()
        )
        image_arch = "inconnue"
        try:
            client = docker.from_env()
            image_arch = client.images.get(self.image_name).attrs.get(
                "Architecture", "inconnue"
            )
        except Exception:
            pass
        return (
            f"L'image est construite pour l'architecture {image_arch}, "
            f"la machine de correction est en {host} : le binaire ne peut pas "
            f"s'executer (exec format error)"
        )

    def _capture_container_logs(self) -> str:
        """Capture logs from the BentoML container."""
        import docker

        try:
            client = docker.from_env()
            container = client.containers.get(self.container_name)
            return container.logs(tail=500).decode("utf-8", errors="replace")
        except Exception as exc:
            return f"Error capturing container logs: {exc}"

    def cleanup(self, force: bool = False):
        """
        Stop and remove BentoML container.

        Args:
            force: If True, ignore errors and force cleanup
        """
        if self.container:
            try:
                self.logger.info("Stopping BentoML container...")
                self.container.stop()
                self.logger.info("✓ Container stopped and removed")
            except Exception as e:
                message = str(e)
                if "No such container" in message or "404 Client Error" in message:
                    self.logger.info("Container already removed during cleanup")
                elif force:
                    self.logger.warning(f"Cleanup error (forced): {e}")
                else:
                    self.logger.error(f"Cleanup error: {e}")
                    raise
            finally:
                self.container = None

        # Additional manual cleanup
        self._force_cleanup_container()
        self.remove_exam_images()

        if self.cli_container_id:
            try:
                self.logger.info("Stopping docker CLI container...")
                subprocess.run(
                    ["docker", "stop", self.cli_container_id],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except Exception as e:
                if force:
                    self.logger.warning(f"CLI cleanup error (forced): {e}")
                else:
                    self.logger.error(f"CLI cleanup error: {e}")
                    raise
            finally:
                self.cli_container_id = None

    def _force_cleanup_container(self):
        """Force cleanup of any remaining container."""
        import docker

        try:
            client = docker.from_env()

            # Find and remove container by name
            try:
                container = client.containers.get(self.container_name)
                self.logger.info(f"Force removing container: {self.container_name}")
                container.remove(force=True, v=True)
            except docker.errors.NotFound:
                pass  # Already removed

        except Exception as e:
            self.logger.warning(f"Error in force cleanup: {e}")

    def _find_bento_file(self) -> Optional[str]:
        """
        Find .bento file in evaluation directory.

        Returns:
            Path to .bento file if found, None otherwise
        """
        # Récursif : les rendus arrivent presque toujours dans un dossier
        # (`examen_bentoml/`), et une recherche limitée à la racine ne voyait
        # ni le .bento livré par l'apprenant ni celui qu'on vient de construire.
        trouves = []
        for current, dirs, files in os.walk(self.eval_dir):
            dirs[:] = [d for d in dirs
                       if d not in {".git", ".venv", "venv", "__pycache__", ".bentoml_home"}]
            trouves += [os.path.join(current, f) for f in sorted(files) if f.endswith(".bento")]

        if trouves:
            # Le moins profond d'abord : un .bento à la racine du rendu prime
            # sur un artefact enfoui.
            trouves.sort(key=lambda chemin: chemin.count(os.sep))
            self.logger.info(f"Found .bento file: {trouves[0]}")
            return trouves[0]

        self.logger.warning("No .bento file found")
        return None

    def _find_bento_source(self) -> Optional[str]:
        """Racine d'un rendu livré en source : `bentofile.yaml` à côté du code.

        C'est une forme de rendu légitime — l'apprenant livre ce qu'il a écrit
        plutôt qu'un artefact construit. Le runner ne savait que charger une
        image ou un `.bento` déjà compilé, et abandonnait en une seconde.
        """
        for current, dirs, files in os.walk(self.eval_dir):
            dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__", "node_modules"}]
            if "bentofile.yaml" in files:
                return current
        return None

    def _build_bento_from_source(self, source_root: str) -> Dict[str, Any]:
        """Construire le `.bento` depuis la source, puis l'image.

        `bentoml build` tourne dans un conteneur jetable : la machine de
        correction n'a pas à porter bentoml ni les dépendances de l'apprenant.
        Le `.bento` produit est déposé dans le rendu, où le chemin existant
        sait le conteneuriser.
        """
        self.record_step(
            "Construire le service depuis la source",
            command=f"bentoml build  # dans {os.path.relpath(source_root, self.eval_dir)}",
            note=(
                "Le rendu livre la source plutôt qu'une image. On la construit pour "
                "pouvoir l'évaluer, au lieu de refuser la copie."
            ),
        )

        image = os.environ.get("EXAM_BENTOML_BUILDER_IMAGE", "python:3.11-slim")
        # Tout dans un seul conteneur : installer, suivre la procedure de
        # l'apprenant, construire, exporter. Un second conteneur repartirait
        # sans bentoml -- l'export echouait ainsi sur une commande introuvable,
        # et le message disait « aucun bento liste » plutot que la verite.
        prealables = " ".join(
            f"if [ -f {script} ]; then echo '--- {script}'; python {script} 2>&1 | tail -5 || true; fi;"
            for script in ("src/prepare_data.py", "src/train_model.py",
                           "src/save_model_joblib.py", "train_model.py")
        )
        script = (
            "cd /src; "
            "pip install --quiet --disable-pip-version-check bentoml >/dev/null 2>&1; "
            "if [ -f requirements.txt ]; then "
            "pip install --quiet --disable-pip-version-check -r requirements.txt >/dev/null 2>&1 || true; fi; "
            "export BENTOML_HOME=/src/.bentoml_home; "
            f"{prealables} "
            "echo '--- bentoml build'; bentoml build 2>&1 | tail -20; "
            "echo '--- export'; "
            "tag=$(bentoml list -o json 2>/dev/null | python -c "
            "'import json,sys; b=json.load(sys.stdin); print(b[0][\"tag\"] if b else \"\")' 2>/dev/null); "
            "if [ -n \"$tag\" ]; then bentoml export \"$tag\" /src/service.bento && "
            "echo \"exporte: $tag\"; else echo 'aucun bento construit'; fi; "
            # Le conteneur ecrit en root dans un repertoire monte : sans ca, le
            # nettoyage cote hote echoue en EACCES et laisse le rendu derriere.
            f"chown -R {os.getuid()}:{os.getgid()} /src 2>/dev/null || true"
        )
        resultat = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{source_root}:/src", image, "sh", "-c", script],
            capture_output=True, text=True, timeout=1800,
        )
        sortie = (resultat.stdout or "") + (resultat.stderr or "")
        self.steps[-1]["output"] = sortie[-3000:]
        self.steps[-1]["exit_code"] = resultat.returncode

        chemin = os.path.join(source_root, "service.bento")
        if not os.path.exists(chemin):
            return {"success": False,
                    "error": f"aucun .bento produit par la construction : {sortie[-400:]}"}
        self.logger.info(f"✓ .bento construit : {chemin}")
        return {"success": True, "bento": chemin}

    def _auto_containerize_bento(self, bento_file_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Auto-containerize a .bento file using Docker build.

        The .bento file is a tar.gz archive containing a Dockerfile.
        We extract it and build the Docker image directly.

        Args:
            bento_file_path: Explicit path to .bento file. If None, search for it.

        Returns:
            Dictionary with success status and image name
        """
        import tarfile
        import shutil

        # Use provided path or search for .bento file
        bento_file = bento_file_path or self._find_bento_file()

        if not bento_file:
            return {
                "success": False,
                "error": "No .bento file found for auto-containerization",
            }

        # Extract bento name from file (e.g., "admission_bento.bento" -> "admission_bento")
        bento_name = os.path.basename(bento_file).replace(".bento", "")
        image_tag = f"{bento_name}_eval:{self.student_name.lower()}"

        # Create temp directory for extraction
        extract_dir = os.path.join(self.eval_dir, f".bento_extracted_{bento_name}")

        self.logger.info(f"Auto-containerizing {bento_file}...")
        self.logger.info(f"Target image: {image_tag}")

        try:
            # Step 1: Extract .bento file (supports gzip and plain tar archives)
            self.logger.info(f"Extracting .bento file to {extract_dir}...")
            os.makedirs(extract_dir, exist_ok=True)

            with tarfile.open(bento_file, "r:*") as tar:
                tar.extractall(path=extract_dir)

            self.logger.info("✓ .bento file extracted")

            # Verify Dockerfile exists
            dockerfile_path = os.path.join(extract_dir, "env", "docker", "Dockerfile")
            if not os.path.exists(dockerfile_path):
                return {
                    "success": False,
                    "error": "No Dockerfile found in .bento bundle (expected at env/docker/Dockerfile)",
                }

            # Step 2: Build Docker image
            self.logger.info(f"Building Docker image from {extract_dir}...")
            build_result = subprocess.run(
                ["docker", "build", "-t", image_tag, "-f", dockerfile_path, "."],
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutes max
                cwd=extract_dir,
            )

            if build_result.returncode != 0:
                error_msg = build_result.stderr or build_result.stdout
                self.logger.error(
                    f"Docker build failed: {error_msg[-500:]}"
                )  # Last 500 chars
                return {
                    "success": False,
                    "error": f"Docker build failed: {error_msg[-200:]}",
                }

            self.logger.info(f"✓ Successfully built Docker image: {image_tag}")

            # Cleanup extracted directory
            try:
                shutil.rmtree(extract_dir)
                self.logger.info("✓ Cleaned up extracted .bento directory")
            except Exception as e:
                self.logger.warning(f"Could not clean up {extract_dir}: {e}")

            return {"success": True, "image_name": image_tag, "bento_file": bento_file}

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Docker build timeout (exceeded 5 minutes)",
            }
        except tarfile.TarError as e:
            return {
                "success": False,
                "error": f"Failed to extract .bento file: {str(e)}",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Unexpected error during containerization: {str(e)}",
            }
        finally:
            # Ensure cleanup even on error
            if os.path.exists(extract_dir):
                try:
                    shutil.rmtree(extract_dir)
                except:
                    pass
