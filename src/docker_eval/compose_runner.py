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


def chemins_nginx_declares(eval_dir: str, maxi: int = 8) -> list:
    """Les chemins que la copie déclare dans son nginx.conf.

    Sonder seulement la racine `/` note 0/10 en API une copie qui met ses
    services derrière le reverse proxy sans publier leurs ports — ce qui est
    précisément l'architecture attendue (vu sur 458925). On lit les
    `location` pour sonder ce que la copie promet de servir.
    """
    import re
    conf = None
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = [d for d in dossiers if d not in (".git", ".venv", "__pycache__", "node_modules")]
        for nom in fichiers:
            if nom.lower() == "nginx.conf":
                conf = os.path.join(racine, nom)
                break
        if conf:
            break
    if not conf:
        return []
    try:
        with open(conf, encoding="utf-8", errors="replace") as lecteur:
            texte = lecteur.read()
    except OSError:
        return []
    chemins = []
    for correspondance in re.finditer(r"location\s+(?:=\s*)?(/[^\s{]*)", texte):
        chemin = correspondance.group(1)
        if chemin != "/" and chemin not in chemins:
            chemins.append(chemin)
    return chemins[:maxi]


def annoter_bruit_de_sondes(sondes: list) -> None:
    """Qualifier le bruit mécanique du double sondage http/https.

    Chaque port publié est sondé dans les deux schémas : sur un port en clair
    l'échec HTTPS est structurel (WRONG_VERSION_NUMBER), sur un port TLS le
    400 « plain HTTP request sent to HTTPS port » prouve au contraire que le
    TLS termine. Sans cette annotation, la revue prend ce bruit pour une
    faute de la copie — vu sur la tentative 459038.
    """
    def _ok(sonde) -> bool:
        code = sonde.get("code")
        return isinstance(code, int) and 200 <= code < 400

    par_cible: dict = {}
    for sonde in sondes:
        schema = sonde["url"].split(":", 1)[0]
        par_cible.setdefault((sonde.get("service"), sonde.get("port")), {})[schema] = sonde

    for sonde in sondes:
        schema = sonde["url"].split(":", 1)[0]
        autre = par_cible.get((sonde.get("service"), sonde.get("port")), {}).get(
            "https" if schema == "http" else "http"
        )
        extrait = str(sonde.get("extrait", ""))
        if schema == "http" and "sent to HTTPS port" in extrait:
            sonde["note"] = "preuve TLS : ce port refuse le HTTP en clair, le TLS termine bien ici"
        elif not _ok(sonde) and autre is not None and _ok(autre):
            sonde["note"] = (
                f"échec attendu : ce port répond en {'https' if schema == 'http' else 'http'} — "
                "sonder l'autre schéma échoue mécaniquement et ne prouve rien contre la copie"
            )


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
        # Testcontainers choisit le nom de projet Compose à partir du dossier,
        # pas à partir de ``project_name``. On mémorise les labels réellement
        # observés tant que les conteneurs existent afin de pouvoir nettoyer
        # fidèlement après la sortie du context manager.
        self._compose_project_names = set()

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
            self._port_relaxation_outcome = "Échec explicite : pyyaml est indisponible."
            self._port_relaxation_exit_code = 2
            return os.path.basename(compose_file)

        try:
            with open(compose_file, encoding="utf-8") as handle:
                doc = yaml.safe_load(handle)
        except Exception as exc:
            self.logger.warning(f"docker-compose.yml illisible ({exc}) : ports laisses tels quels")
            self._port_relaxation_outcome = f"Échec explicite : compose illisible ({exc})."
            self._port_relaxation_exit_code = 2
            return os.path.basename(compose_file)

        if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
            self._port_relaxation_outcome = "Absence explicite : aucun service Compose exploitable."
            self._port_relaxation_exit_code = 0
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
            self._port_relaxation_outcome = "Absence explicite : aucun port hôte fixe à relâcher."
            self._port_relaxation_exit_code = 0
            return os.path.basename(compose_file)

        relaxed_name = "docker-compose.eval.yml"
        relaxed_path = os.path.join(self.eval_dir, relaxed_name)
        try:
            with open(relaxed_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(doc, handle, sort_keys=False)
        except Exception as exc:
            self.logger.warning(f"Copie du compose impossible ({exc}) : ports laisses tels quels")
            self._port_relaxation_outcome = f"Échec explicite : copie impossible ({exc})."
            self._port_relaxation_exit_code = 2
            return os.path.basename(compose_file)

        self.logger.info("Ports hote relaches : Docker en attribuera de libres")
        self._port_relaxation_outcome = f"Ports hôte relâchés dans {relaxed_name}."
        self._port_relaxation_exit_code = 0
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
            # Les quatre noms que docker compose accepte lui-meme, dans son
            # ordre de priorite : compose.yaml > compose.yml > docker-compose.yaml
            # > docker-compose.yml (on garde ce dernier en tete, c'est le nom
            # demande par les enonces).
            compose_resolution_started = time.time()
            compose_file = next(
                (
                    candidate
                    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")
                    if os.path.exists(candidate := os.path.join(self.eval_dir, name))
                ),
                None,
            )
            if compose_file is None:
                error_msg = f"docker-compose.yml (ou variante compose.yaml) not found in {self.eval_dir}"
                self.logger.error(error_msg)
                self.record_step(
                    "Fichier Compose résolu",
                    command="recherche d'un fichier Compose",
                    output=error_msg,
                    exit_code=2,
                    duration=time.time() - compose_resolution_started,
                )
                return self.echec(error_msg)
            self.record_step(
                "Fichier Compose résolu",
                command="recherche d'un fichier Compose",
                output=compose_file,
                exit_code=0,
                duration=time.time() - compose_resolution_started,
            )

            # Liberer les ports hote declares par l'apprenant. Deux copies du
            # meme examen, ou n'importe quel conteneur deja lance sur la
            # machine, se disputeraient sinon le meme port. On ne touche qu'a
            # une copie du fichier, et seulement au port cote hote : le service
            # ecoute toujours sur le meme port dans son conteneur, donc ce qui
            # est evalue ne change pas.
            port_relaxation_started = time.time()
            compose_name = self._relax_host_ports(compose_file)
            self.record_step(
                "Ports hôte relâchés",
                command=f"réécriture YAML des ports de {os.path.basename(compose_file)}",
                output=getattr(self, "_port_relaxation_outcome", "État inconnu."),
                exit_code=getattr(self, "_port_relaxation_exit_code", 2),
                duration=time.time() - port_relaxation_started,
            )

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
            compose_started = time.time()
            try:
                with self.compose:
                    self.record_step(
                        "Build et démarrage Compose",
                        command=f"docker compose -f {compose_name} up --build",
                        output="Entrée dans le contexte testcontainers réussie (stdout non disponible).",
                        exit_code=0,
                        duration=time.time() - compose_started,
                    )
                    self.logger.info("Services started successfully")
                    # Retenir les images construites pour cette copie : le ménage
                    # les supprimera. Celles de l'apprenant portent le préfixe du
                    # projet compose ; les images publiques (nginx, prometheus)
                    # restent, elles resserviront.
                    try:
                        prefixe = os.path.basename(self.eval_dir).lstrip(".-_").lower()
                        for image in self._images_du_projet():
                            # Seules les images construites pour cette copie : le
                            # préfixe du projet compose. `nginx:latest` et autres
                            # images publiques restent, elles resserviront.
                            if image.lower().startswith(prefixe):
                                self.record_exam_image(image)
                    except Exception as erreur:
                        self.logger.debug(f"images non recensées : {erreur}")

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
                    try:
                        exit_code = self._wait_for_pipeline_completion()
                    except TimeoutError:
                        # Le context manager supprime les conteneurs en sortant.
                        # L'état doit donc être capturé avant de relancer le
                        # timeout vers le gestionnaire d'erreur extérieur.
                        try:
                            self._record_service_states()
                        except Exception as state_error:
                            self.record_step(
                                "État des services",
                                command="docker inspect des conteneurs Compose",
                                output=f"État non reçu : {state_error}",
                                exit_code=2,
                                duration=0,
                            )
                        raise
                    self._record_service_states()

                    # Capture logs before cleanup
                    self.logger.info("Capturing container logs...")
                    logs = self._capture_logs()

                    # Evaluate result
                    success = exit_code == 0
                    result = {
                        "success": success,
                        "exit_code": exit_code,
                        "logs": logs,
                        "steps": self.steps,
                    }

                    self._log_execution_end(result)
                    return result
            except Exception as exc:
                if not any(step["title"] == "Build et démarrage Compose" for step in self.steps):
                    self.record_step(
                        "Build et démarrage Compose",
                        command=f"docker compose -f {compose_name} up --build",
                        output=str(exc),
                        exit_code=2,
                        duration=time.time() - compose_started,
                    )
                raise

        except TimeoutError as e:
            error_msg = f"Timeout apres {time.time() - run_started:.1f}s (limite {self.timeout}s)"
            self.logger.error(error_msg)
            if not any(step["title"] == "État des services" for step in self.steps):
                try:
                    self._record_service_states()
                except Exception as state_error:
                    self.record_step(
                        "État des services",
                        command="docker inspect des conteneurs Compose",
                        output=f"État non reçu : {state_error}",
                        exit_code=2,
                        duration=0,
                    )
            result = {"success": False, "error": error_msg, "exit_code": 3, "steps": self.steps}
            self._log_execution_end(result)
            return result

        except Exception as e:
            error_msg = f"Execution error: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            result = {"success": False, "error": error_msg, "exit_code": 2, "steps": self.steps}
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
                container = client.containers.get(identifiant)
                project = container.labels.get("com.docker.compose.project")
                if project:
                    self._compose_project_names.add(project)
                conteneurs.append(container)
        return conteneurs

    def _images_du_projet(self) -> list:
        import docker
        client = docker.from_env()
        images = set()
        for c in self._conteneurs_du_projet():
            for tag in (c.image.tags or []):
                images.add(tag)
        return sorted(images)

    def _etats_du_projet(self) -> dict:
        etats = {}
        for c in self._conteneurs_du_projet():
            c.reload()
            etats[c.name] = (c.status, c.attrs.get("State", {}).get("ExitCode", 0))
        return etats

    def _pipeline_container(self):
        """Le conteneur pipeline réellement créé par Compose, s'il existe."""
        for container in self._conteneurs_du_projet():
            if container.labels.get("com.docker.compose.service") == "pipeline":
                return container
        return None

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
                # IPv4 et IPv6 publient le même port hôte : une seule sonde.
                hotes = sorted({pub.get("HostPort") for pub in publications or [] if pub.get("HostPort")})
                for hote in hotes:
                    # Les deux schémas, toujours : « HTTPS joignable » est au
                    # barème nginx, et un port TLS répond 400 au HTTP nu — ce
                    # qui est déjà une réponse, pas une raison de s'arrêter.
                    for schema in ("http", "https"):
                        url = f"{schema}://127.0.0.1:{hote}/"
                        started = time.time()
                        try:
                            reponse = urllib.request.urlopen(
                                url, timeout=10,
                                context=contexte_tls if schema == "https" else None,
                            )
                            sondes.append({"service": c.name, "port": interne, "url": url,
                                           "code": reponse.status,
                                           "extrait": reponse.read(200).decode("utf-8", "replace"),
                                           "duration_seconds": time.time() - started})
                        except urllib.error.HTTPError as erreur:
                            corps = erreur.read(300).decode("utf-8", "replace")
                            sondes.append({"service": c.name, "port": interne, "url": url,
                                           "code": erreur.code,
                                           "entetes": dict(erreur.headers) if "WWW-Authenticate" in erreur.headers else None,
                                           "extrait": corps,
                                           "duration_seconds": time.time() - started})
                        except Exception as erreur:
                            sondes.append({"service": c.name, "port": interne, "url": url,
                                           "code": "non reçu", "erreur": str(erreur),
                                           "extrait": str(erreur)})
                            sondes[-1]["duration_seconds"] = time.time() - started
        return sondes

    def _evaluer_services_persistants(self) -> Dict[str, Any]:
        """Évaluer un examen dont les services servent au lieu de se terminer.

        nginx, prometheus-grafana : rien ne « finit », le succès c'est une
        stack debout qui répond sur ses ports.
        """
        etats = self._record_service_states()

        sondes = self._sonder_ports_publies()
        annoter_bruit_de_sondes(sondes)
        for sonde in sondes:
            code = sonde.get("code", "non reçu")
            resume = f"code {code}"
            note = sonde.get("note")
            bruit = bool(note and note.startswith("échec attendu"))
            self.record_step(
                f"Sonde {sonde['url']}",
                command=f"GET {sonde['url']}",
                output=(f"{resume}\n{note + chr(10) if note else ''}{sonde.get('extrait', '')}")[:500],
                # Une sonde qui reçoit un code HTTP a atteint le service : la
                # qualité de la réponse est l'affaire du barème, pas de l'étape.
                # Un échec attendu (mauvais schéma sur ce port) ne compte pas
                # non plus : il ne prouve rien contre la copie.
                exit_code=0 if bruit or isinstance(code, int) else 1,
                duration=sonde.get("duration_seconds", 0),
            )

        # Les chemins que la copie déclare (location du nginx.conf), sondés à
        # travers le port TLS : une API derrière le reverse proxy sans port
        # publié est l'architecture attendue, pas une API absente.
        sondes.extend(self._sonder_chemins_declares(sondes))

        succes, morts = self._classer_services(etats)
        if not etats:
            # Ne jamais réussir sur du vide : si on ne voit aucun conteneur,
            # c'est notre lecture qui est cassée, pas la copie qui est bonne.
            succes, morts = False, {"(aucun conteneur visible)": ("absent", -1)}

        # Investigation outillée (#78) : tant que la stack tourne, un vrai
        # échec (étape non annotée « attendu ») mérite un debug actif. Après
        # le teardown, plus rien n'est testable.
        echecs = [s for s in self.steps if s.get("exit_code") not in (0, None)]
        if echecs:
            from .investigator import Investigator
            enqueteur = Investigator(self, self.eval_dir, list(etats.keys()))
            if enqueteur.disponible():
                self.logger.info(f"Investigation outillée : {len(echecs)} étape(s) en échec")
                try:
                    enqueteur.investiguer(echecs)
                except Exception as erreur:
                    self.record_step("Investigation interrompue",
                                     output=f"erreur interne : {erreur}", exit_code=1)
            else:
                self.record_step(
                    "Investigation non configurée",
                    output="étapes en échec mais pas de gateway LLM "
                           "(LIORA_GATEWAY_URL/LIORA_API_KEY ou PI_CORRECTOR_INVESTIGATE_*)",
                    exit_code=0,
                )
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

    def _sonder_chemins_declares(self, sondes_racine: list) -> list:
        """Sonder, à travers le port TLS du reverse proxy, les chemins que la
        copie déclare. Seulement sur les ports où le TLS termine déjà (code
        HTTP reçu en https à la racine) : zéro bruit de schéma."""
        chemins = chemins_nginx_declares(self.eval_dir)
        if not chemins:
            return []
        import ssl
        import urllib.request
        import urllib.error
        contexte = ssl.create_default_context()
        contexte.check_hostname = False
        contexte.verify_mode = ssl.CERT_NONE
        bases = [
            s for s in sondes_racine
            if "nginx" in str(s.get("service", "")).lower()
            and s["url"].startswith("https")
            and isinstance(s.get("code"), int)
        ]
        resultats = []
        for base in bases:
            racine_url = base["url"].rstrip("/")
            for chemin in chemins:
                url = f"{racine_url}{chemin}"
                started = time.time()
                sonde = {"service": base["service"], "port": base["port"], "url": url}
                try:
                    reponse = urllib.request.urlopen(url, timeout=10, context=contexte)
                    sonde.update(code=reponse.status,
                                 extrait=reponse.read(200).decode("utf-8", "replace"))
                except urllib.error.HTTPError as erreur:
                    sonde.update(code=erreur.code,
                                 entetes=dict(erreur.headers) if "WWW-Authenticate" in erreur.headers else None,
                                 extrait=erreur.read(300).decode("utf-8", "replace"))
                except Exception as erreur:
                    sonde.update(code="non reçu", erreur=str(erreur), extrait=str(erreur))
                sonde["duration_seconds"] = time.time() - started
                code = sonde.get("code")
                self.record_step(
                    f"Sonde {url}",
                    command=f"GET {url}",
                    output=f"code {code}\n{sonde.get('extrait', '')}"[:500],
                    exit_code=0 if isinstance(code, int) else 1,
                    duration=sonde["duration_seconds"],
                )
                resultats.append(sonde)
                # Un 401 prouve le défi d'authentification ; il ne dit rien de
                # l'API derrière. On rejoue avec les identifiants de l'énoncé
                # (par env, jamais en dur dans ce dépôt public) : seul un 2xx
                # authentifié prouve une API fonctionnelle.
                identifiants = os.environ.get("EXAM_DOCKER_EVAL_BASIC_AUTH", "")
                if code == 401 and identifiants:
                    import base64
                    jeton = base64.b64encode(identifiants.encode()).decode()
                    started = time.time()
                    sonde_auth = {"service": base["service"], "port": base["port"],
                                  "url": url, "auth": True}
                    try:
                        requete = urllib.request.Request(
                            url, headers={"Authorization": f"Basic {jeton}"})
                        reponse = urllib.request.urlopen(requete, timeout=10, context=contexte)
                        sonde_auth.update(code=reponse.status,
                                          extrait=reponse.read(200).decode("utf-8", "replace"))
                    except urllib.error.HTTPError as erreur:
                        sonde_auth.update(code=erreur.code,
                                          extrait=erreur.read(300).decode("utf-8", "replace"))
                    except Exception as erreur:
                        sonde_auth.update(code="non reçu", erreur=str(erreur), extrait=str(erreur))
                    sonde_auth["duration_seconds"] = time.time() - started
                    code_auth = sonde_auth.get("code")
                    self.record_step(
                        f"Sonde authentifiée {url} (identifiants de l'énoncé)",
                        command=f"GET {url} avec Authorization: Basic (identifiants de l'énoncé)",
                        output=f"code {code_auth}\n{sonde_auth.get('extrait', '')}"[:500],
                        exit_code=0 if isinstance(code_auth, int) else 1,
                        duration=sonde_auth["duration_seconds"],
                    )
                    resultats.append(sonde_auth)
        return resultats

    def _record_service_states(self) -> dict:
        """Lire et consigner l'état de chaque conteneur du projet."""
        states_started = time.time()
        etats = self._etats_du_projet()
        self.record_step(
            "État des services",
            command="docker inspect des conteneurs Compose",
            output="\n".join(f"{n} : {s} (code {c})" for n, (s, c) in sorted(etats.items())),
            exit_code=0,
            duration=time.time() - states_started,
        )
        return etats

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
        start_time = time.time()
        check_interval = 2  # Check every 2 seconds

        while True:
            elapsed = time.time() - start_time

            # Check timeout
            if elapsed > self.timeout:
                self.logger.error(f"Timeout exceeded ({self.timeout}s)")
                raise TimeoutError(f"Execution exceeded {self.timeout} seconds")

            try:
                container = self._pipeline_container()
                if container is None:
                    self.logger.debug(f"Pipeline container not found yet (elapsed: {elapsed:.1f}s)")
                    time.sleep(check_interval)
                    continue

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

            except Exception as e:
                self.logger.error(f"Error checking pipeline status: {e}")
                time.sleep(check_interval)

    def _capture_logs(self) -> str:
        """
        Capture logs from all compose services.

        Returns:
            Combined logs from all containers
        """
        all_logs = []

        try:
            for container in self._conteneurs_du_projet():
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
        started = time.time()
        errors = []
        if self.compose:
            try:
                self.logger.info("Cleaning up Docker Compose resources...")
                # The context manager should handle this, but ensure cleanup
                self.compose.stop()
                self.logger.info("✓ Docker Compose cleanup completed")
            except Exception as e:
                errors.append(str(e))
                if force:
                    self.logger.warning(f"Cleanup error (forced): {e}")
                else:
                    self.logger.error(f"Cleanup error: {e}")

        # Additional manual cleanup using docker client
        for operation in (self._force_cleanup_docker_resources, self.remove_exam_images):
            try:
                operation_errors = operation()
                if operation_errors:
                    errors.extend(str(error) for error in operation_errors)
            except Exception as e:
                errors.append(str(e))
                self.logger.warning(f"Cleanup error: {e}")
        self.record_step(
            "Nettoyage Compose",
            command="docker compose down et suppression des ressources d'examen",
            output="ok" if not errors else " ; ".join(errors),
            exit_code=0 if not errors else 1,
            duration=time.time() - started,
        )

    def _force_cleanup_docker_resources(self):
        """Supprimer les ressources portant les labels Compose réellement vus.

        Toutes les erreurs sont regroupées et remontées à ``cleanup`` : la step
        de ménage doit refléter un échec réel au lieu d'annoncer ``ok``.
        """
        import docker

        if not self._compose_project_names:
            return

        client = docker.from_env()
        errors = []
        for project in sorted(self._compose_project_names):
            label = f"com.docker.compose.project={project}"
            try:
                containers = client.containers.list(all=True, filters={"label": label})
            except Exception as exc:
                errors.append(f"liste conteneurs {project}: {exc}")
                containers = []
            for container in containers:
                try:
                    self.logger.info(f"Force removing container: {container.name}")
                    container.remove(force=True, v=True)
                except Exception as exc:
                    errors.append(f"conteneur {container.name}: {exc}")
            try:
                networks = client.networks.list(filters={"label": label})
            except Exception as exc:
                errors.append(f"liste réseaux {project}: {exc}")
                networks = []
            for network in networks:
                try:
                    self.logger.info(f"Force removing network: {network.name}")
                    network.remove()
                except Exception as exc:
                    errors.append(f"réseau {network.name}: {exc}")

        if errors:
            raise RuntimeError(" ; ".join(errors))
