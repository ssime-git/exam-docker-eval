"""
Docker Compose runner for evaluations.

Handles Linux-Bash, NGINX, and Prometheus-Grafana evaluations that use
docker-compose.yml configurations.
"""

import logging
import os
import time
import urllib.request
import urllib.error
import ssl
from typing import Dict, Any

from testcontainers.compose import DockerCompose

from .base_runner import BaseRunner
from .config import SERVICE_READY_TIMEOUT, FLASK_READY_PATTERN


class ComposeRunner(BaseRunner):
    """
    Runner for evaluations using Docker Compose.

    Manages multi-container setups with guaranteed cleanup using
    testcontainers' DockerCompose context manager.
    """

    def __init__(self, student_name: str, eval_dir: str, timeout: int, logger: logging.Logger):
        """
        Initialize Compose runner.

        Args:
            student_name: Student identifier
            eval_dir: Directory containing docker-compose.yml
            timeout: Maximum execution time in seconds
            logger: Logger instance
        """
        super().__init__(student_name, eval_dir, timeout, logger)
        self.compose = None
        self.project_name = f"eval_{student_name}".replace(" ", "_").lower()

    def _relax_host_ports(self, compose_file: str) -> str:
        """Ecrit une copie du compose sans port hote fige, et rend son nom.

        Une declaration `"3000:3000"` reserve le port 3000 de la machine. Deux
        corrections simultanees, ou n'importe quel conteneur deja lance, se le
        disputent. En ne gardant que le port du conteneur, Docker en attribue
        un libre et testcontainers le relit.

        Rend le nom du fichier d'origine si rien n'a pu etre reecrit : mieux
        vaut tenter avec les ports declares que ne pas evaluer du tout.
        """
        try:
            import yaml
        except ImportError:
            self.logger.warning("pyyaml absent : ports hote laisses tels quels")
            return os.path.basename(compose_file)

        try:
            with open(compose_file, encoding="utf-8") as handle:
                doc = yaml.safe_load(handle)
        except Exception as exc:
            self.logger.warning(f"docker-compose.yml illisible ({exc}) : ports laisses tels quels")
            return os.path.basename(compose_file)

        if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
            return os.path.basename(compose_file)

        changed = False
        for service in doc["services"].values():
            if not isinstance(service, dict) or "ports" not in service:
                continue
            relaxed = []
            for entry in service["ports"] or []:
                new_entry = self._container_port_only(entry)
                changed = changed or new_entry != entry
                relaxed.append(new_entry)
            service["ports"] = relaxed

        if not changed:
            return os.path.basename(compose_file)

        relaxed_name = "docker-compose.eval.yml"
        relaxed_path = os.path.join(self.eval_dir, relaxed_name)
        try:
            with open(relaxed_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(doc, handle, sort_keys=False)
        except Exception as exc:
            self.logger.warning(f"Copie du compose impossible ({exc}) : ports laisses tels quels")
            return os.path.basename(compose_file)

        self.logger.info("Ports hote relaches : Docker en attribuera de libres")
        return relaxed_name

    @staticmethod
    def _container_port_only(entry):
        """Ne garder que le port cote conteneur d'une declaration de ports.

        Accepte les deux formes du format Compose : la chaine `"8080:80"` et le
        mapping `{"published": 8080, "target": 80}`.
        """
        if isinstance(entry, dict):
            if "target" in entry:
                return {k: v for k, v in entry.items() if k != "published"}
            return entry
        if isinstance(entry, int):
            return entry
        if isinstance(entry, str):
            # "127.0.0.1:8080:80/tcp" -> "80/tcp" ; "80" reste "80"
            head, _, proto = entry.partition("/")
            parts = head.split(":")
            if len(parts) == 1:
                return entry
            container_port = parts[-1]
            return f"{container_port}/{proto}" if proto else container_port
        return entry

    def run_evaluation(self) -> Dict[str, Any]:
        """
        Execute evaluation using Docker Compose.

        Returns:
            Dictionary with evaluation results
        """
        self._log_execution_start()

        run_started = time.time()
        self.record_submission_step()
        try:
            # Verify docker-compose.yml exists
            compose_file = os.path.join(self.eval_dir, "docker-compose.yml")
            if not os.path.exists(compose_file):
                error_msg = f"docker-compose.yml not found in {self.eval_dir}"
                self.logger.error(error_msg)
                return {"success": False, "error": error_msg, "exit_code": 2}

            # Liberer les ports hote declares par l'apprenant. Deux copies du
            # meme examen, ou n'importe quel conteneur deja lance sur la
            # machine, se disputeraient sinon le meme port. On ne touche qu'a
            # une copie du fichier, et seulement au port cote hote : le service
            # ecoute toujours sur le meme port dans son conteneur, donc ce qui
            # est evalue ne change pas.
            compose_name = self._relax_host_ports(compose_file)

            # Create testcontainers Compose instance
            self.logger.info(f"Creating Docker Compose instance with project name: {self.project_name}")
            self.compose = DockerCompose(
                context=self.eval_dir,
                compose_file_name=compose_name,
                pull=False,  # Use local builds
                build=True,  # Build images before starting
                env_file=None  # No .env file needed
            )

            # Start services with context manager (auto-cleanup on exit)
            self.logger.info("Starting Docker Compose services...")
            with self.compose:
                self.logger.info("Services started successfully")

                # Wait for services to be ready
                self._wait_for_services()

                # Deux formes d'examen. Un service `pipeline` declare = un
                # traitement qui se termine (linux-bash) : on attend sa fin.
                # Sans lui (nginx, prometheus-grafana), rien ne finit jamais —
                # attendre un conteneur pipeline-1 inexistant faisait tourner
                # 600s de boucle NotFound avant un timeout impute a la copie.
                services = self._services_du_compose(os.path.join(self.eval_dir, compose_name))
                if "pipeline" not in services:
                    self.logger.info(f"Pas de service pipeline ({services}) : stack servante")
                    result = self._evaluer_services_persistants()
                    self._log_execution_end(result)
                    return result

                # Wait for pipeline container to complete
                self.logger.info(f"Waiting for pipeline completion (timeout: {self.timeout}s)...")
                exit_code = self._wait_for_pipeline_completion()

                # Capture logs before cleanup
                self.logger.info("Capturing container logs...")
                logs = self._capture_logs()

                # Evaluate result
                success = exit_code == 0
                result = {
                    "success": success,
                    "exit_code": exit_code,
                    "logs": logs
                }

                self._log_execution_end(result)
                return result

        except TimeoutError as e:
            error_msg = f"Timeout apres {time.time() - run_started:.1f}s (limite {self.timeout}s)"
            self.logger.error(error_msg)
            result = {"success": False, "error": error_msg, "exit_code": 3}
            self._log_execution_end(result)
            return result

        except Exception as e:
            error_msg = f"Execution error: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            result = {"success": False, "error": error_msg, "exit_code": 2}
            self._log_execution_end(result)
            return result

        finally:
            # Guaranteed cleanup (even on crash)
            self.cleanup(force=True)

    @staticmethod
    def _services_du_compose(compose_file: str) -> list:
        import yaml
        with open(compose_file, encoding="utf-8") as handle:
            contenu = yaml.safe_load(handle) or {}
        return sorted((contenu.get("services") or {}).keys())

    @staticmethod
    def _classer_services(etats: dict) -> tuple:
        """(succes, morts) depuis {nom: (status, exit_code)}.

        Un service encore debout est sain. Un service sorti en 0 est un
        one-shot qui a fini son travail (init de certificats, migration).
        Un service sorti autrement est mort.
        """
        morts = {n: e for n, e in etats.items()
                 if e[0] == "exited" and e[1] != 0 or e[0] == "dead"}
        return (not morts, morts)

    def _conteneurs_du_projet(self) -> list:
        """Les conteneurs de la stack, via testcontainers lui-même.

        Filtrer par label com.docker.compose.project=<self.project_name> rend
        une liste vide : testcontainers nomme le projet d'après le dossier du
        compose, pas d'après notre project_name. Une liste vide ferait un
        succès par vacuité — zéro service mort parce que zéro service tout
        court.
        """
        import docker
        client = docker.from_env()
        conteneurs = []
        for info in self.compose.get_containers(include_all=True):
            identifiant = getattr(info, "ID", None) or getattr(info, "id", None)
            if identifiant:
                conteneurs.append(client.containers.get(identifiant))
        return conteneurs

    def _etats_du_projet(self) -> dict:
        etats = {}
        for c in self._conteneurs_du_projet():
            c.reload()
            etats[c.name] = (c.status, c.attrs.get("State", {}).get("ExitCode", 0))
        return etats

    def _sonder_ports_publies(self) -> list:
        """GET sur chaque port publié du projet, en HTTP puis HTTPS.

        On ne juge pas les codes ici : un 401 sur une racine protégée est un
        bon signe, un refus TLS sur le port HTTP est normal. On enregistre ce
        qui répond, l'agent et le barème en font ce qu'ils savent.
        """
        sondes = []
        contexte_tls = ssl.create_default_context()
        contexte_tls.check_hostname = False
        contexte_tls.verify_mode = ssl.CERT_NONE
        for c in self._conteneurs_du_projet():
            if c.status != "running":
                continue
            ports = c.attrs.get("NetworkSettings", {}).get("Ports") or {}
            for interne, publications in ports.items():
                for pub in publications or []:
                    hote = pub.get("HostPort")
                    if not hote:
                        continue
                    for schema in ("http", "https"):
                        url = f"{schema}://127.0.0.1:{hote}/"
                        try:
                            reponse = urllib.request.urlopen(
                                url, timeout=10,
                                context=contexte_tls if schema == "https" else None,
                            )
                            sondes.append({"service": c.name, "port": interne, "url": url,
                                           "code": reponse.status,
                                           "extrait": reponse.read(200).decode("utf-8", "replace")})
                            break
                        except urllib.error.HTTPError as erreur:
                            corps = erreur.read(300).decode("utf-8", "replace")
                            sondes.append({"service": c.name, "port": interne, "url": url,
                                           "code": erreur.code,
                                           "entetes": dict(erreur.headers) if "WWW-Authenticate" in erreur.headers else None,
                                           "extrait": corps})
                            break
                        except Exception as erreur:
                            if schema == "https":
                                sondes.append({"service": c.name, "port": interne, "url": url,
                                               "erreur": str(erreur)[:150]})
        return sondes

    def _evaluer_services_persistants(self) -> Dict[str, Any]:
        """Évaluer un examen dont les services servent au lieu de se terminer.

        nginx, prometheus-grafana : rien ne « finit », le succès c'est une
        stack debout qui répond sur ses ports.
        """
        etats = self._etats_du_projet()
        self.record_step(
            "État des services",
            output="\n".join(f"{n} : {s} (code {c})" for n, (s, c) in sorted(etats.items())),
        )
        sondes = self._sonder_ports_publies()
        for sonde in sondes:
            resume = (f"code {sonde['code']}" if "code" in sonde else f"erreur {sonde.get('erreur')}")
            self.record_step(f"Sonde {sonde['url']}", output=f"{resume}\n{sonde.get('extrait', '')}"[:500])

        succes, morts = self._classer_services(etats)
        if not etats:
            # Ne jamais réussir sur du vide : si on ne voit aucun conteneur,
            # c'est notre lecture qui est cassée, pas la copie qui est bonne.
            succes, morts = False, {"(aucun conteneur visible)": ("absent", -1)}
        logs = self._capture_logs()
        return {
            "success": succes,
            "exit_code": 0 if succes else 1,
            "error": None if succes else f"service(s) en échec : {sorted(morts)}",
            "services": {n: {"status": e[0], "exit_code": e[1]} for n, e in etats.items()},
            "probes": sondes,
            "logs": logs,
            "steps": self.steps,
        }

    def _wait_for_services(self):
        """
        Wait for services to be ready.

        Uses testcontainers' wait strategies for robust readiness detection.
        """
        # For compose setups, we rely on healthchecks defined in docker-compose.yml
        # Give services time to initialize
        self.logger.info(f"Waiting {SERVICE_READY_TIMEOUT}s for services to become ready...")
        time.sleep(SERVICE_READY_TIMEOUT)
        self.logger.info("Services should be ready")

    def _wait_for_pipeline_completion(self) -> int:
        """
        Wait for pipeline container to complete execution.

        Returns:
            Exit code of pipeline container (0 for success)

        Raises:
            TimeoutError: If execution exceeds timeout
        """
        import docker

        client = docker.from_env()
        pipeline_container_name = f"{self.project_name}-pipeline-1"

        start_time = time.time()
        check_interval = 2  # Check every 2 seconds

        while True:
            elapsed = time.time() - start_time

            # Check timeout
            if elapsed > self.timeout:
                self.logger.error(f"Timeout exceeded ({self.timeout}s)")
                raise TimeoutError(f"Execution exceeded {self.timeout} seconds")

            try:
                # Find pipeline container
                container = client.containers.get(pipeline_container_name)

                # Check container status
                container.reload()
                status = container.status

                if status == "exited":
                    exit_code = container.attrs['State']['ExitCode']
                    self.logger.info(f"Pipeline container exited with code {exit_code}")
                    return exit_code

                elif status in ["created", "restarting", "running", "paused"]:
                    # Container still running
                    self.logger.debug(f"Pipeline status: {status} (elapsed: {elapsed:.1f}s)")
                    time.sleep(check_interval)

                elif status in ["removing", "dead"]:
                    self.logger.error(f"Pipeline container in unexpected state: {status}")
                    return 2  # Critical error

            except docker.errors.NotFound:
                # Container doesn't exist yet or was removed
                self.logger.debug(f"Pipeline container not found yet (elapsed: {elapsed:.1f}s)")
                time.sleep(check_interval)

            except Exception as e:
                self.logger.error(f"Error checking pipeline status: {e}")
                time.sleep(check_interval)

    def _capture_logs(self) -> str:
        """
        Capture logs from all compose services.

        Returns:
            Combined logs from all containers
        """
        import docker

        client = docker.from_env()
        all_logs = []

        try:
            # Get all containers for this project
            containers = client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.project={self.project_name}"}
            )

            for container in containers:
                service_name = container.labels.get("com.docker.compose.service", container.name)
                all_logs.append(f"\n{'=' * 80}")
                all_logs.append(f"Logs from {service_name}")
                all_logs.append('=' * 80)

                try:
                    logs = container.logs(tail=500).decode('utf-8', errors='replace')
                    all_logs.append(logs)
                except Exception as e:
                    all_logs.append(f"Error capturing logs: {e}")

            return "\n".join(all_logs)

        except Exception as e:
            self.logger.error(f"Error capturing logs: {e}")
            return f"Error capturing logs: {e}"

    def cleanup(self, force: bool = False):
        """
        Stop and remove all Docker Compose resources.

        Args:
            force: If True, ignore errors and force cleanup
        """
        if self.compose:
            try:
                self.logger.info("Cleaning up Docker Compose resources...")
                # The context manager should handle this, but ensure cleanup
                self.compose.stop()
                self.logger.info("✓ Docker Compose cleanup completed")
            except Exception as e:
                if force:
                    self.logger.warning(f"Cleanup error (forced): {e}")
                else:
                    self.logger.error(f"Cleanup error: {e}")
                    raise

        # Additional manual cleanup using docker client
        self._force_cleanup_docker_resources()

    def _force_cleanup_docker_resources(self):
        """Force cleanup of any remaining Docker resources for this project."""
        import docker

        try:
            client = docker.from_env()

            # Remove containers
            containers = client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.project={self.project_name}"}
            )

            for container in containers:
                try:
                    self.logger.info(f"Force removing container: {container.name}")
                    container.remove(force=True, v=True)
                except Exception as e:
                    self.logger.warning(f"Error removing container {container.name}: {e}")

            # Remove networks
            networks = client.networks.list(
                filters={"label": f"com.docker.compose.project={self.project_name}"}
            )

            for network in networks:
                try:
                    self.logger.info(f"Force removing network: {network.name}")
                    network.remove()
                except Exception as e:
                    self.logger.warning(f"Error removing network {network.name}: {e}")

        except Exception as e:
            self.logger.warning(f"Error in force cleanup: {e}")
