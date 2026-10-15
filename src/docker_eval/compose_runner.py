"""
Docker Compose runner for evaluations.

Handles Linux-Bash, NGINX, and Prometheus-Grafana evaluations that use
docker-compose.yml configurations.
"""

import logging
import os
import re
import time
from typing import Dict, Any

from testcontainers.compose import DockerCompose

from .base_runner import BaseRunner
from . import sondes_nginx
from .sonde_http import contexte_tls_permissif, resume, sonder
from .config import SERVICE_READY_TIMEOUT, FLASK_READY_PATTERN


# Les quatre noms que docker compose accepte lui-meme, dans son ordre de
# priorite : compose.yaml > compose.yml > docker-compose.yaml >
# docker-compose.yml (on garde ce dernier en tete, c'est le nom demande par
# les enonces).
NOMS_COMPOSE = ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")
_DOSSIERS_IGNORES = {".git", ".venv", "venv", "node_modules", "__pycache__", "__MACOSX"}


def fichier_compose(dossier: str) -> str | None:
    """Le fichier Compose d'un dossier, s'il en porte un."""
    return next((chemin for nom in NOMS_COMPOSE
                 if os.path.isfile(chemin := os.path.join(dossier, nom))), None)


def dossier_du_compose(eval_dir: str, profondeur_max: int = 2) -> tuple[str | None, list[str]]:
    """Le dossier du projet : celui qui porte le fichier Compose.

    La racine du rendu d'abord. Sinon, un dossier d'emballage : 466056
    livrait un projet complet et conforme — compose, deployment/, src/ — dans
    `projet/`, et l'evaluation s'arretait sur « compose not found » a la
    racine, Bloque pour une affaire de zip. On descend jusqu'a
    `profondeur_max` niveaux ; un seul dossier candidat est le projet. Plusieurs
    candidats : on ne choisit pas a la place de l'apprenant.

    Rend (dossier ou None, candidats relatifs a eval_dir).
    """
    if fichier_compose(eval_dir):
        return eval_dir, []
    candidats = []
    for racine, dossiers, _ in os.walk(eval_dir):
        dossiers[:] = sorted(d for d in dossiers if d not in _DOSSIERS_IGNORES and not d.startswith("."))
        profondeur = os.path.relpath(racine, eval_dir).count(os.sep) + 1 if racine != eval_dir else 0
        if profondeur >= profondeur_max:
            dossiers[:] = []
        if racine != eval_dir and fichier_compose(racine):
            candidats.append(racine)
            dossiers[:] = []  # un compose imbriqué dans un projet n'est pas un autre projet
    relatifs = [os.path.relpath(c, eval_dir) for c in candidats]
    return (candidats[0] if len(candidats) == 1 else None), relatifs


# Interpolation Compose. `$$` d'abord : c'est un dollar littéral, pas une
# variable. Groupe 2 = le modificateur éventuel (`:-`, `-`, `:+`, `+`, `:?`,
# `?`) ; le message de `:?` peut porter espaces, accents, parenthèses (469217).
_INTERPOLATION = re.compile(
    r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)((?::?[-+?])[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
EXEMPLES_ENV = (".env.example", ".env.sample", ".env.template", "env.example", "example.env")


def variables_requises(texte_compose: str) -> list:
    """Les variables sans lesquelles compose refuse d'interpoler, dans l'ordre.

    Seules `${VAR:?msg}` et `${VAR?msg}` font échouer `up`. Un `${VAR}` ou
    `$VAR` absent devient une chaîne vide (avertissement) et la stack démarre :
    le harnais reproduit compose sans l'élargir — `DEBUG=${DEBUG}` rempli au
    hasard deviendrait vrai. Défauts (`:-`, `-`) et `:+`, `+` : jamais requis.
    On lit les valeurs du YAML chargé, pas le texte brut : un
    commentaire n'est pas interpolé par compose. Texte brut en secours si le
    YAML ne se charge pas.
    """
    chaines = []
    try:
        import yaml

        def parcourir(noeud):
            if isinstance(noeud, str):
                chaines.append(noeud)
            elif isinstance(noeud, dict):
                for valeur in noeud.values():
                    parcourir(valeur)
            elif isinstance(noeud, list):
                for valeur in noeud:
                    parcourir(valeur)

        parcourir(yaml.safe_load(texte_compose))
    except Exception:
        chaines = [texte_compose]
    requises = []
    for chaine in chaines:
        for m in _INTERPOLATION.finditer(chaine):
            nom = m.group(1)
            if not nom:
                continue  # `$$` ou `$VAR` : jamais une erreur pour compose
            if not (m.group(2) or "").lstrip(":").startswith("?"):
                continue
            if nom not in requises:
                requises.append(nom)
    return requises


_AFFECTATION_ENV = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")


def completer_env(texte: str, noms: list, generer) -> tuple:
    """Complète un texte .env : affectations vides remplies sur place, noms
    absents ajoutés en fin. Pas de doublon : on ne dépend pas de celui que le
    lecteur dotenv de compose garderait. Rend (texte, noms complétés)."""
    lignes = texte.splitlines()
    vus, completes = set(), []
    for i, ligne in enumerate(lignes):
        m = _AFFECTATION_ENV.match(ligne)
        if not m or m.group(2) not in noms:
            continue
        vus.add(m.group(2))
        if m.group(4).strip() in ("", '""', "''"):
            lignes[i] = f"{m.group(1)}{m.group(2)}={generer()}"
            completes.append(m.group(2))
    for nom in noms:
        if nom not in vus:
            lignes.append(f"{nom}={generer()}")
            completes.append(nom)
    return "\n".join(lignes) + "\n", completes


def chemins_nginx_declares(eval_dir: str, maxi: int = 8) -> list:
    """Les chemins que la copie déclare dans son nginx.conf.

    Sonder seulement la racine `/` note 0/10 en API une copie qui met ses
    services derrière le reverse proxy sans publier leurs ports — ce qui est
    précisément l'architecture attendue (vu sur 458925). On lit les
    `location` pour sonder ce que la copie promet de servir.
    """
    texte = sondes_nginx.lire_nginx_conf(eval_dir)
    if texte is None:
        return []
    chemins = []
    for correspondance in re.finditer(r"location\s+(?:=\s*)?(/[^\s{]*)", texte):
        chemin = correspondance.group(1)
        if chemin != "/" and chemin not in chemins:
            chemins.append(chemin)
    return chemins[:maxi]


def identifiants_declares(eval_dir: str, maxi: int = 3) -> list:
    """Les identifiants que la copie déclare elle-même (tests, README, Makefile).

    L'énoncé suggère un couple, mais l'apprenant choisit le sien : deux copies
    (458925, 459038) protégeaient /predict avec admin:admin, écrit dans leur
    run_tests.sh et vérifiable contre leur .htpasswd. Conclure « identifiants
    refusés » sans avoir lu la copie est une faute de correction : on cherche
    d'abord ce que l'apprenant a fourni.
    """
    import re
    candidats = []
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = [d for d in dossiers if d not in (".git", ".venv", "__pycache__", "node_modules")]
        for nom in fichiers:
            if not (nom.lower().endswith((".sh", ".md", ".yml", ".yaml", ".txt")) or nom == "Makefile"):
                continue
            try:
                with open(os.path.join(racine, nom), encoding="utf-8", errors="replace") as lecteur:
                    texte = lecteur.read()
            except OSError:
                continue
            for m in re.finditer(r"(?:--user[= ]|-u )([A-Za-z0-9_.-]+:[^\s'\"`\\;)]+)", texte):
                couple = m.group(1)
                if "wrong" in couple.lower() or "bad" in couple.lower():
                    continue  # les cas négatifs de leurs propres tests
                source = os.path.relpath(os.path.join(racine, nom), eval_dir)
                if couple not in [c for c, _ in candidats]:
                    candidats.append((couple, source))
    return candidats[:maxi]


def requetes_declarees(eval_dir: str, maxi: int = 6) -> list:
    """Les requêtes que la copie déclare dans ses propres tests (curl).

    Sonder en GET une route POST rend 405 : le moteur concluait « API non
    fonctionnelle » sur une API parfaitement saine (458925 : son run_tests.sh
    fait POST /predict avec payload et identifiants → 200). La seule preuve
    de fonction, c'est de rejouer ce que l'apprenant déclare : méthode,
    chemin, corps, en-têtes, identifiants.
    """
    import re
    requetes = []
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = [d for d in dossiers if d not in (".git", ".venv", "__pycache__", "node_modules")]
        for nom in fichiers:
            if not (nom.endswith(".sh") or nom == "Makefile"):
                continue
            try:
                with open(os.path.join(racine, nom), encoding="utf-8", errors="replace") as lecteur:
                    texte = lecteur.read().replace("\\\n", " ")
            except OSError:
                continue
            for appel in re.finditer(r"curl\s+[^\n]+", texte):
                ligne = appel.group(0)
                url = re.search(r"https?://(?:localhost|127\.0\.0\.1)(?::(\d+))?(/[^\s\"']*)", ligne)
                if not url:
                    continue
                methode = re.search(r"-X\s+(\w+)", ligne)
                corps = re.search(r"(?:-d|--data(?:-raw)?)\s+'([^']*)'", ligne) or \
                        re.search(r"(?:-d|--data(?:-raw)?)\s+\"([^\"]*)\"", ligne)
                user = re.search(r"--user[= ]([^\s'\"]+)", ligne)
                entetes = dict(re.findall(r"-H\s+['\"]([^:'\"]+):\s*([^'\"]+)['\"]", ligne))
                requete = {
                    "methode": (methode.group(1) if methode else "GET").upper(),
                    # Les curl vivent souvent dans un $(...) : ne pas avaler
                    # la parenthèse ou la quote de fermeture.
                    "chemin": url.group(2).rstrip(")\"'"),
                    # Le port visé : `localhost:9090/api/v1/...` parle à
                    # Prometheus, pas au reverse proxy (469145).
                    "port": int(url.group(1)) if url.group(1) else None,
                    "corps": corps.group(1) if corps else None,
                    "entetes": entetes,
                    "identifiants": user.group(1) if user else None,
                    "source": os.path.relpath(os.path.join(racine, nom), eval_dir),
                }
                if requete not in requetes:
                    requetes.append(requete)
                if len(requetes) >= maxi:
                    return requetes
    return requetes


def sens_sonde_authentifiee(code_sans_auth, code_auth) -> str:
    """Ce qu'une sonde authentifiée en GET prouve, dit en clair dans l'étape.

    401 sans identifiants puis 405 avec : le reverse proxy a ACCEPTÉ les
    identifiants, et c'est l'application qui refuse le verbe GET sur une
    route POST. 469145 (15/10) : ce 405 était lu « identifiants introuvables
    dans le rendu » alors que les POST authentifiés de la copie rendaient 200."""
    if code_sans_auth == 401 and code_auth == 405:
        return ("authentification acceptée (le 401 sans identifiants devient 405) : seule la méthode GET "
                "n'est pas servie par cette route — voir les requêtes déclarées pour le bon verbe\n")
    if code_sans_auth == 401 and isinstance(code_auth, int) and code_auth != 401:
        return "authentification acceptée (le 401 sans identifiants disparaît)\n"
    return ""


def code_attendu_negatif(identifiants: str):
    """401 pour les identifiants qu'une copie fausse exprès dans ses tests."""
    return 401 if re.search(r"wrong|bad|invalid|faux|mauvais", identifiants, re.IGNORECASE) else None


PAUSE_LIMITATION_S = 1.5

# Sondes de comportement nginx (469145). `sonder` coupe les corps à 300
# octets : assez pour un code, trop peu pour comparer deux réponses A/B ou
# trouver `nginx_up` sous le préambule go_/process_ d'un /metrics.
TAILLE_CORPS_SONDE = 200_000
REQUETES_PAR_RAFALE = 20
COMPORTEMENT_NON_MESURE = {"ab": None, "rafale": None, "mauvais_identifiants": None,
                           "sans_identifiants": None, "repartition": None, "redirection_80_443": None}
MONITORING_NON_MESURE = {"exporter_nginx_up": None, "prometheus_cibles_up": None,
                         "prometheus_cibles_down": None, "grafana_datasources": None,
                         "grafana_dashboards": None}


def limitation_de_debit_declaree(eval_dir: str) -> bool:
    """La copie configure-t-elle une limitation de débit nginx (limit_req) ?"""
    import re
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = [d for d in dossiers if d not in (".git", ".venv", "__pycache__", "node_modules")]
        for nom in fichiers:
            if not nom.endswith(".conf"):
                continue
            try:
                with open(os.path.join(racine, nom), encoding="utf-8", errors="replace") as lecteur:
                    # `limit_req zone=…`, pas `limit_req_zone` (la déclaration de la zone).
                    if re.search(r"\blimit_req\s+zone=", lecteur.read()):
                        return True
            except OSError:
                continue
    return False


def racine_du_port(sondes_racine: list, port: int):
    """L'URL hôte où le port de conteneur `port` est publié, dans le schéma
    qui y répond ; None s'il n'est publié nulle part."""
    for sonde in sondes_racine:
        if str(sonde.get("port", "")).split("/")[0] != str(port):
            continue
        if isinstance(sonde.get("code"), int) and not str(sonde.get("note", "")).startswith("échec attendu"):
            return sonde["url"].rstrip("/")
    return None


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


class _DemarrageSansDemontage:
    """`with compose:` sans le démontage que testcontainers fait sur un `up`
    raté : son __enter__ appelle __exit__ en cas d'échec, et les conteneurs
    disparaissaient avant que le moteur ne relève leur état et leurs logs
    (461416). Le démontage reste garanti par cleanup()."""

    def __init__(self, compose):
        self.compose = compose

    def __enter__(self):
        self.compose.start()
        return self.compose

    def __exit__(self, *exc):
        return self.compose.__exit__(*exc)


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
        # Le .env écrit par le harnais (469217), retiré au nettoyage ; jamais
        # celui de la copie.
        self._env_cree = None

    @staticmethod
    def _nom_fixe_en_alias(service: dict) -> bool:
        """Remplacer `container_name` par un alias réseau du même nom.

        461416 : deux corrections simultanées se disputaient
        `container_name: prometheus_server`. L'alias garde le nom résolvable
        (une copie qui fait `proxy_pass http://api_v2` marche toujours) sans
        réserver un nom global sur la machine. Sans réseau Compose
        (`network_mode`), pas d'alias possible : le nom reste.
        """
        nom = service.get("container_name")
        if not nom or "network_mode" in service:
            return False
        reseaux = service.get("networks") or ["default"]
        if isinstance(reseaux, list):
            reseaux = {reseau: None for reseau in reseaux}
        for reseau, reglages in list(reseaux.items()):
            reglages = dict(reglages or {})
            reglages["aliases"] = [*reglages.get("aliases", []), nom]
            reseaux[reseau] = reglages
        service["networks"] = reseaux
        del service["container_name"]
        return True

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
        alias = []
        for nom_service, service in doc["services"].items():
            if isinstance(service, dict) and self._nom_fixe_en_alias(service):
                alias.append(nom_service)
                changed = True
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
        self._port_relaxation_outcome = f"Ports hôte relâchés dans {relaxed_name}." + (
            f" Noms de conteneur fixes devenus alias réseau : {', '.join(alias)}." if alias else "")
        self._port_relaxation_exit_code = 0
        return relaxed_name

    def _preparer_env(self, compose_file: str) -> None:
        """Fournir le `.env` qu'une copie ne livre pas, sans lancer son Makefile.

        469217 gardait `.env` hors de l'archive (`.gitignore`, bonne pratique)
        et le générait par `make env`, appelé par `make start-project`. Le
        moteur lance compose directement : « required variable
        GRAFANA_ADMIN_PASSWORD is missing a value », stack à terre, faute du
        harnais imputée à l'apprenant. Exécuter son Makefile sur l'hôte serait
        du shell arbitraire hors conteneur : on fournit seulement les
        variables que le compose exige sans défaut — l'exemple livré, complété,
        sinon des valeurs aléatoires.

        Le `.env` s'écrit dans le dossier du projet (compose l'y lit), qui est
        la copie de travail extraite, comme `docker-compose.eval.yml`. L'étape
        nomme les variables, jamais les valeurs. Un `.env` déjà présent, ou
        aucune variable `:?`/`?` : on ne touche à rien (même un exemple livré
        reste en place) et on ne consigne rien. Le `.env` créé ici est retiré
        par `cleanup`, pour qu'un rejeu dans le même workdir donne la même
        trace.
        """
        import secrets
        import shutil

        debut = time.time()
        cible = os.path.join(self.eval_dir, ".env")
        if os.path.lexists(cible):
            return
        try:
            with open(compose_file, encoding="utf-8", errors="replace") as lecteur:
                requises = variables_requises(lecteur.read())
        except OSError:
            return
        if not requises:
            return

        def generer():
            return secrets.token_hex(12)

        # Un lien symbolique de la copie pourrait pointer hors du rendu : on
        # ne recopie qu'un vrai fichier.
        exemple = next((nom for nom in EXEMPLES_ENV
                        if os.path.isfile(chemin := os.path.join(self.eval_dir, nom))
                        and not os.path.islink(chemin)), None)
        try:
            if exemple:
                shutil.copyfile(os.path.join(self.eval_dir, exemple), cible)
                with open(cible, encoding="utf-8", errors="replace") as lecteur:
                    texte, completes = completer_env(lecteur.read(), requises, generer)
                source = f"exemple livré « {exemple} »" + (
                    f", complété par le harnais pour : {', '.join(completes)}" if completes
                    else "")
            else:
                texte, completes = completer_env("", requises, generer)
                source = "généré par le harnais (valeurs aléatoires)"
            with open(cible, "w", encoding="utf-8") as sortie:
                sortie.write(texte)
        except OSError as erreur:
            if os.path.isfile(cible) and not os.path.islink(cible):
                self._env_cree = cible  # copie partielle : la retirer aussi
            self.record_step(
                "Variables d'environnement de la copie",
                command="préparation du .env manquant",
                output=f"Échec explicite : .env non écrit ({erreur}).",
                exit_code=2,
                duration=time.time() - debut,
            )
            return
        self._env_cree = cible
        self.logger.info(f".env fourni par le harnais : {', '.join(requises)}")
        self.record_step(
            "Variables d'environnement de la copie",
            command="préparation du .env manquant (valeurs non consignées)",
            output=(f"Aucun .env livré ; le compose exige sans défaut : {', '.join(requises)}. "
                    f"Source : {source}. La copie ne livre pas ses secrets (bonne pratique) ; "
                    "le harnais les fournit comme le ferait sa préparation (make env)."),
            exit_code=0,
            duration=time.time() - debut,
        )

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
            compose_resolution_started = time.time()
            dossier, candidats = dossier_du_compose(self.eval_dir)
            if dossier is not None and dossier != self.eval_dir:
                # Le rendu est emballe : l'evaluation part du dossier du projet
                # (contexte de build, nom de projet Compose, lectures de la
                # copie). Consigne, pour que le relecteur voie le detour.
                self.record_step(
                    "Dossier du projet",
                    command="recherche du fichier Compose sous la racine du rendu",
                    output=(f"Aucun fichier Compose à la racine ; le projet est dans "
                            f"« {candidats[0]} », d'où l'évaluation repart."),
                    exit_code=0,
                    duration=time.time() - compose_resolution_started,
                )
                self.eval_dir = dossier
            compose_file = fichier_compose(dossier) if dossier is not None else None
            if compose_file is None:
                if len(candidats) > 1:
                    error_msg = (f"plusieurs fichiers Compose sous {self.eval_dir} "
                                 f"({', '.join(candidats)}) et aucun à la racine : projet ambigu")
                else:
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

            # Avant tout `up` : sans ses variables requises, compose refuse
            # d'interpoler et la stack ne démarre pas (469217).
            self._preparer_env(compose_file)

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
            # Avant le build : un échec de build (exec format error, image de
            # base introuvable) est justement là où la plateforme compte.
            self.consigner_environnement(None, runner_installe=False)

            # Create testcontainers Compose instance
            self.logger.info(f"Creating Docker Compose instance with project name: {self.project_name}")
            # testcontainers ne transmet pas de nom de projet : compose prenait
            # celui du dossier, le même pour toutes les copies d'un examen
            # (mlops-nginx-exam-2). Deux corrections simultanées partageaient
            # alors un projet et se démontaient l'une l'autre (« No such
            # container »). Chaque commande compose hérite de cette variable.
            os.environ["COMPOSE_PROJECT_NAME"] = self.project_name
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
                with _DemarrageSansDemontage(self.compose):
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
                    self._retenir_images_de_la_copie()

                    # Wait for services to be ready
                    self._wait_for_services()
                    self._completer_environnement_compose()

                    # Deux formes d'examen. Un service `pipeline` declare = un
                    # traitement qui se termine (linux-bash) : on attend sa fin.
                    # Sans lui (nginx, prometheus-grafana), rien ne finit jamais —
                    # attendre un conteneur pipeline-1 inexistant faisait tourner
                    # 600s de boucle NotFound avant un timeout impute a la copie.
                    services = self._services_du_compose(os.path.join(self.eval_dir, compose_name))
                    # Convention d'examen declarable (pi_stack_mode, #49) :
                    # serving | pipeline | auto (defaut : deviner par la
                    # presence d'un service `pipeline`).
                    mode = os.environ.get("EXAM_STACK_MODE", "auto")
                    servante = mode == "serving" or (mode != "pipeline" and "pipeline" not in services)
                    if servante:
                        self.logger.info(f"Stack servante ({'déclarée' if mode == 'serving' else services})")
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
                        output=self._diagnostic_up_rate(exc),
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
        Un service sorti autrement est mort, et un service qui redémarre en
        boucle aussi : sur 461338, bike-api plantait à l'import et Docker le
        relançait sans fin, la stack passait pour debout.
        """
        morts = {n: e for n, e in etats.items()
                 if e[0] == "exited" and e[1] != 0 or e[0] in ("dead", "restarting")}
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

    def _boucles_de_redemarrage(self, delai: float = 45, pas: float = 3) -> set:
        """Les conteneurs qui redémarrent en boucle.

        461338 : bike-api plantait à l'import ; Docker le relançait sans fin,
        et à l'instant du relevé il était souvent « running » (code remis à
        0). Seuls les conteneurs qui ont déjà redémarré sont observés, au
        plus `delai` secondes : si le compteur de redémarrages augmente ou si
        le conteneur repasse par restarting/exited, c'est une boucle. Un
        service qui a redémarré en attendant sa base puis s'est stabilisé
        n'est pas en boucle. Une stack sans redémarrage n'attend rien.
        """
        suivis = {}
        for conteneur in self._conteneurs_du_projet():
            conteneur.reload()
            compte = conteneur.attrs.get("RestartCount", 0) or 0
            if conteneur.status == "restarting":
                suivis[conteneur.name] = (conteneur, -1)
            elif compte > 0:
                suivis[conteneur.name] = (conteneur, compte)
        boucles = {nom for nom, (_, compte) in suivis.items() if compte == -1}
        debut = time.time()
        while suivis.keys() - boucles and time.time() - debut < delai:
            time.sleep(pas)
            for nom, (conteneur, compte) in suivis.items():
                if nom in boucles:
                    continue
                conteneur.reload()
                if (conteneur.status in ("restarting", "exited")
                        or (conteneur.attrs.get("RestartCount", 0) or 0) > compte):
                    boucles.add(nom)
        return boucles

    def _pipeline_container(self):
        """Le conteneur pipeline réellement créé par Compose, s'il existe."""
        for container in self._conteneurs_du_projet():
            if container.labels.get("com.docker.compose.service") == "pipeline":
                return container
        return None

    def _completer_environnement_compose(self) -> None:
        """Image, architecture et limites de chaque conteneur démarré."""
        from . import environnement
        lignes = []
        try:
            for c in self._conteneurs_du_projet():
                image = (c.attrs.get("Config") or {}).get("Image") or c.attrs.get("Image") or "?"
                arch = environnement.arch_image(c.attrs.get("Image") or image)
                lignes.append(f"conteneur {c.name} : image {image} ({arch or 'architecture non lue'}), "
                              f"limites : {environnement.limites(c.attrs)}")
        except Exception as erreur:
            lignes.append(f"conteneurs non lus : {erreur}")
        self.completer_environnement(lignes)

    def _sonder_ports_publies(self) -> list:
        """GET sur chaque port publié du projet, en HTTP puis HTTPS.

        On ne juge pas les codes ici : un 401 sur une racine protégée est un
        bon signe, un refus TLS sur le port HTTP est normal. On enregistre ce
        qui répond, l'agent et le barème en font ce qu'ils savent.
        """
        sondes = []
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
                        # Un 301 vers le 443 du conteneur, non publié,
                        # finissait en « Connection refused » : la chaîne est
                        # consignée, et une cible injoignable n'est pas une
                        # panne (#320).
                        sonde = {"service": c.name, "port": interne, "url": url}
                        sonde.update(sonder(url))
                        sonde["duration_seconds"] = time.time() - started
                        sondes.append(sonde)
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
            note = sonde.get("note")
            bruit = bool(note and note.startswith("échec attendu"))
            self.record_step(
                f"Sonde {sonde['url']}",
                command=f"GET {sonde['url']}",
                output=(f"{resume(sonde)}\n{note + chr(10) if note else ''}{sonde.get('extrait', '')}")[:500],
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

        # Les exigences nginx prouvées en marche (469145, grille du 15/10) :
        # le barème lit `nginx_comportement`, plus la présence des directives.
        comportement = None
        if sondes_nginx.fichier_nginx_conf(self.eval_dir):
            try:
                comportement = self._sonder_comportement_nginx(sondes)
            except Exception as erreur:
                # Une sonde de comportement ne fait jamais échouer l'évaluation.
                comportement = dict(COMPORTEMENT_NON_MESURE, monitoring=dict(MONITORING_NON_MESURE),
                                    tls={"tls_ancien_refuse": None, "certificat_valide": None})
                self.record_step("Sondes de comportement nginx",
                                 output=f"non mesurées : erreur interne du harnais ({erreur})", exit_code=0)

        boucles = self._boucles_de_redemarrage()
        for nom in boucles:
            etats[nom] = ("restarting", etats.get(nom, ("", 1))[1] or 1)
        if boucles:
            self.record_step(
                "Services qui redémarrent en boucle",
                command="docker inspect : RestartCount observé pendant l'évaluation",
                output="\n".join(sorted(boucles)),
                exit_code=1,
            )
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
        resultat = {
            "success": succes,
            "exit_code": 0 if succes else 1,
            "error": None if succes else f"service(s) en échec : {sorted(morts)}",
            "services": {n: {"status": e[0], "exit_code": e[1]} for n, e in etats.items()},
            "probes": sondes,
            "logs": logs,
            "steps": self.steps,
        }
        if comportement is not None:
            resultat["nginx_comportement"] = comportement
        return resultat

    def _sonder_chemins_declares(self, sondes_racine: list) -> list:
        """Sonder, à travers le port TLS du reverse proxy, les chemins que la
        copie déclare. Seulement sur les ports où le TLS termine déjà (code
        HTTP reçu en https à la racine) : zéro bruit de schéma."""
        # Sans nginx.conf, pas de chemin à sonder par le proxy — mais les
        # requêtes que la copie déclare (curl vers :8000/metrics, :9090/api…)
        # se rejouent quand même : prometheus-grafana n'a pas de reverse proxy,
        # et ses requêtes déclarées n'étaient jamais rejouées.
        chemins = chemins_nginx_declares(self.eval_dir)
        contexte = contexte_tls_permissif()
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
                sonde.update(sonder(url, contexte=contexte))
                sonde["duration_seconds"] = time.time() - started
                code = sonde.get("code")
                self.record_step(
                    f"Sonde {url}",
                    command=f"GET {url}",
                    output=f"{resume(sonde)}\n{sonde.get('extrait', '')}"[:500],
                    exit_code=0 if isinstance(code, int) else 1,
                    duration=sonde["duration_seconds"],
                )
                resultats.append(sonde)
                # Un 401 prouve le défi d'authentification ; il ne dit rien de
                # l'API derrière. On rejoue avec chaque couple d'identifiants
                # disponible : ceux de l'énoncé (par env, jamais en dur dans ce
                # dépôt public) ET ceux que la copie déclare elle-même (tests,
                # README) — l'apprenant choisit ses identifiants, les lire fait
                # partie de la correction.
                if code == 401:
                    candidats = []
                    enonce = os.environ.get("EXAM_DOCKER_EVAL_BASIC_AUTH", "")
                    if enonce:
                        candidats.append((enonce, "énoncé"))
                    candidats.extend(
                        (c, f"déclarés dans la copie ({src})")
                        for c, src in identifiants_declares(self.eval_dir)
                        if c != enonce
                    )
                    import base64
                    for identifiants, source in candidats:
                        jeton = base64.b64encode(identifiants.encode()).decode()
                        started = time.time()
                        sonde_auth = {"service": base["service"], "port": base["port"],
                                      "url": url, "auth": True, "source_identifiants": source}
                        sonde_auth.update(sonder(url, entetes={"Authorization": f"Basic {jeton}"},
                                                 contexte=contexte))
                        sonde_auth["duration_seconds"] = time.time() - started
                        code_auth = sonde_auth.get("code")
                        note_auth = sens_sonde_authentifiee(code, code_auth)
                        self.record_step(
                            f"Sonde authentifiée {url} (identifiants {source})",
                            command=f"GET {url} avec Authorization: Basic (identifiants {source})",
                            output=f"{resume(sonde_auth)}\n{note_auth}{sonde_auth.get('extrait', '')}"[:500],
                            exit_code=0 if isinstance(code_auth, int) else 1,
                            duration=sonde_auth["duration_seconds"],
                        )
                        resultats.append(sonde_auth)
                        # Le premier couple accepté suffit : la sécurité est
                        # traversée, inutile d'essayer les suivants.
                        if isinstance(code_auth, int) and code_auth != 401:
                            break

        # La preuve de fonction : rejouer les requêtes que la copie déclare
        # (méthode, corps, en-têtes, identifiants), sur les mêmes bases TLS —
        # sauf celles qui visent explicitement un autre port : elles partent
        # vers le port hôte où ce port de conteneur est publié, ou ne partent
        # pas. 469145 : `curl http://localhost:9090/api/v1/status/runtimeinfo`
        # (Prometheus) rejoué sur le 443 de nginx rendait un 404 « bloquant ».
        import base64
        requetes = requetes_declarees(self.eval_dir)
        vers_proxy = [r for r in requetes if r.get("port") in (None, 80, 443)]
        for requete in (r for r in requetes if r.get("port") not in (None, 80, 443)):
            racine_url = racine_du_port(sondes_racine, requete["port"])
            if not racine_url:
                self.record_step(
                    f"Requête déclarée ({requete['source']}) : {requete['methode']} {requete['chemin']}",
                    command=f"{requete['methode']} localhost:{requete['port']}{requete['chemin']}",
                    output=f"non rejouée : le port {requete['port']} n'est publié par aucun conteneur du projet",
                    exit_code=0,
                )
                continue
            resultats.append(self._rejouer_requete(requete, racine_url, contexte, {"service": None,
                                                                                  "port": requete["port"]}))
        for base in bases:
            racine_url = base["url"].rstrip("/")
            for requete in vers_proxy:
                resultats.append(self._rejouer_requete(requete, racine_url, contexte, base))
        return resultats

    def _rejouer_requete(self, requete: dict, racine_url: str, contexte, base: dict) -> dict:
        """Une requête déclarée, rejouée et consignée en étape.

        Un 429/503 sur une copie qui déclare `limit_req` est la limitation de
        débit que l'énoncé demande, déclenchée par nos propres sondes en
        rafale (469145 : 10 r/s, burst 5 — le 503 par défaut de nginx). La
        requête est rejouée une fois après une pause, et l'étape le dit."""
        import base64
        url = f"{racine_url}{requete['chemin']}"
        entetes = dict(requete["entetes"])
        if requete["identifiants"]:
            entetes["Authorization"] = "Basic " + base64.b64encode(
                requete["identifiants"].encode()).decode()
        corps = requete["corps"].encode() if requete["corps"] else None
        if corps and "Content-Type" not in entetes:
            entetes["Content-Type"] = "application/json"
        started = time.time()
        sonde = {"service": base["service"], "port": base["port"], "url": url,
                 "auth": bool(requete["identifiants"]), "declared": True}
        sonde.update(sonder(url, entetes=entetes, corps=corps, methode=requete["methode"],
                            timeout=15, contexte=contexte))
        note = ""
        if sonde.get("code") in (429, 503) and limitation_de_debit_declaree(self.eval_dir):
            premiere = sonde.get("code")
            time.sleep(PAUSE_LIMITATION_S)
            sonde.update(sonder(url, entetes=entetes, corps=corps, methode=requete["methode"],
                                timeout=15, contexte=contexte))
            sonde["limitation_de_debit"] = premiere
            note = (f"limitation de débit de la copie (limit_req) atteinte par les sondes du "
                    f"harnais : {premiere} d'abord, rejouée après {PAUSE_LIMITATION_S:g} s\n")
        if requete["identifiants"] and code_attendu_negatif(requete["identifiants"]) == sonde.get("code"):
            note += ("requête négative de la copie (identifiants volontairement faux) : "
                     "le 401 est la réponse attendue, la protection fonctionne\n")
        sonde["duration_seconds"] = time.time() - started
        code = sonde.get("code")
        self.record_step(
            f"Requête déclarée ({requete['source']}) : {requete['methode']} {requete['chemin']}",
            command=f"{requete['methode']} {url}"
                    + (" + corps JSON" if corps else "")
                    + (" + Authorization Basic" if requete["identifiants"] else ""),
            output=f"{resume(sonde)}\n{note}{sonde.get('extrait', '')}"[:500],
            exit_code=0 if isinstance(code, int) else 1,
            duration=sonde["duration_seconds"],
        )
        return sonde

    def _requete_predict(self) -> dict:
        """Le POST /predict à rejouer : celui que la copie déclare.

        469145 déclare trois POST /predict : le nominal, la variante A/B
        (X-Experiment-Group: debug) et un cas négatif (admin:wrongpassword).
        On prend le premier qui vise le proxy avec des identifiants non
        négatifs : c'est lui qui rend 200. Sans requête déclarée, un POST
        sans corps : le code reste lisible (422 n'est pas 401 ni 503)."""
        for requete in requetes_declarees(self.eval_dir):
            if (requete["methode"] == "POST" and requete["chemin"].rstrip("/").endswith("/predict")
                    and requete.get("port") in (None, 80, 443)
                    and not (requete["identifiants"] and code_attendu_negatif(requete["identifiants"]))):
                return requete
        return {"methode": "POST", "chemin": "/predict", "corps": None, "entetes": {}, "identifiants": None}

    def _identifiants_candidats(self, requete: dict) -> list:
        """Ceux de la requête déclarée, puis ceux que la copie écrit ailleurs,
        puis ceux de l'énoncé (par env, jamais en dur dans ce dépôt public)."""
        candidats = []
        if requete.get("identifiants"):
            candidats.append(requete["identifiants"])
        candidats.extend(c for c, _ in identifiants_declares(self.eval_dir))
        if os.environ.get("EXAM_DOCKER_EVAL_BASIC_AUTH"):
            candidats.append(os.environ["EXAM_DOCKER_EVAL_BASIC_AUTH"])
        uniques = []
        for couple in candidats:
            if couple not in uniques and not code_attendu_negatif(couple):
                uniques.append(couple)
        return uniques

    def _poster_predict(self, url: str, requete: dict, identifiants, contexte, *,
                        entete_ab: str | None = None, valeur_ab: str | None = None,
                        reessayer: bool = True) -> dict:
        """Un POST /predict, corps et en-têtes de la copie, en-tête A/B fixé.

        `limit_req` agit avant `auth_basic` (phase preaccess de nginx) : nos
        propres sondes peuvent vider le seau, même avec de mauvais
        identifiants. Un 429/503 est rejoué une fois après la pause."""
        import base64
        entetes = {k: v for k, v in (requete.get("entetes") or {}).items()
                   if not entete_ab or k.lower() != entete_ab.lower()}
        if entete_ab and valeur_ab is not None:
            entetes[entete_ab] = valeur_ab
        if identifiants:
            entetes["Authorization"] = "Basic " + base64.b64encode(identifiants.encode()).decode()
        corps = requete["corps"].encode() if requete.get("corps") else None
        if corps and not any(k.lower() == "content-type" for k in entetes):
            entetes["Content-Type"] = "application/json"
        sonde = sonder(url, entetes=entetes, corps=corps, methode="POST", timeout=15, contexte=contexte,
                       taille_extrait=TAILLE_CORPS_SONDE)
        if reessayer and sonde.get("code") in (429, 503):
            time.sleep(PAUSE_LIMITATION_S)
            sonde = sonder(url, entetes=entetes, corps=corps, methode="POST", timeout=15, contexte=contexte,
                           taille_extrait=TAILLE_CORPS_SONDE)
        return sonde

    def _sonder_comportement_nginx(self, sondes_racine: list) -> dict:
        """Les exigences nginx de la grille, prouvées en marche (469145).

        Chaque sonde est au mieux : celle qui ne peut pas tourner vaut None,
        jamais une exception, jamais un échec de l'évaluation. La rafale
        passe en dernier parmi les POST : elle vide le seau de `limit_req`
        et fausserait les autres."""
        from collections import Counter
        from concurrent.futures import ThreadPoolExecutor
        from urllib.parse import urlsplit

        debut = time.time()
        texte = sondes_nginx.lire_nginx_conf(self.eval_dir) or ""
        contexte = contexte_tls_permissif()
        comportement = dict(COMPORTEMENT_NON_MESURE)
        base = next((
            s for s in sondes_racine
            if "nginx" in str(s.get("service", "")).lower()
            and "exporter" not in str(s.get("service", "")).lower()
            and s.get("url", "").startswith("https")
            and isinstance(s.get("code"), int)
        ), None)
        requete = self._requete_predict()
        chemin = requete["chemin"]
        try:
            ab_declare = sondes_nginx.routage_ab_declare(texte)
        except Exception:
            ab_declare = None

        if base is not None:
            url = base["url"].rstrip("/") + chemin
            entete_ab = ab_declare["entete"] if ab_declare else None
            # Les sondes déclarées viennent de passer sur le même seau.
            time.sleep(PAUSE_LIMITATION_S)
            identifiants = None
            defaut = None
            try:
                # Le POST nominal sert aussi à trouver les identifiants qui
                # passent : le premier couple qui ne rend pas 401.
                for couple in self._identifiants_candidats(requete) or [None]:
                    defaut = self._poster_predict(url, requete, couple, contexte, entete_ab=entete_ab)
                    identifiants = couple
                    if defaut.get("code") != 401:
                        break
            except Exception:
                defaut = None

            if ab_declare and defaut is not None:
                try:
                    variante = self._poster_predict(url, requete, identifiants, contexte,
                                                    entete_ab=entete_ab, valeur_ab=ab_declare["valeur"])
                    deux_codes = isinstance(defaut.get("code"), int) and isinstance(variante.get("code"), int)
                    comportement["ab"] = {
                        "entete": entete_ab,
                        "valeur": ab_declare["valeur"],
                        "code_defaut": defaut.get("code"),
                        "code_variante": variante.get("code"),
                        "corps_differents": (defaut.get("extrait") != variante.get("extrait")) if deux_codes else None,
                    }
                except Exception:
                    comportement["ab"] = None

            try:
                utilisateur = (identifiants or "admin").split(":", 1)[0]
                mauvais = self._poster_predict(url, requete, f"{utilisateur}:mauvais-mot-de-passe-scriptorium",
                                               contexte, entete_ab=entete_ab)
                code = mauvais.get("code")
                comportement["mauvais_identifiants"] = code if isinstance(code, int) else \
                    f"erreur : {mauvais.get('erreur') or code}"
            except Exception:
                comportement["mauvais_identifiants"] = None

            try:
                # 469217 (rejeu du 15/10) : /predict limité au POST, nginx rend
                # 403 au GET avant auth_basic. Le GET sans identifiants des
                # chemins déclarés ne montre alors jamais le défi : on le
                # mesure sur la méthode que la copie sert, corps et en-têtes
                # de la requête nominale, sans Authorization (même déclarée
                # en en-tête explicite). Avant la rafale : seau encore plein.
                anonyme = dict(requete, identifiants=None, entetes={
                    k: v for k, v in (requete.get("entetes") or {}).items() if k.lower() != "authorization"})
                sans = self._poster_predict(url, anonyme, None, contexte, entete_ab=entete_ab)
                code = sans.get("code")
                comportement["sans_identifiants"] = code if isinstance(code, int) else \
                    f"erreur : {sans.get('erreur') or code}"
            except Exception:
                comportement["sans_identifiants"] = None

            try:
                # Seau plein avant la rafale : la mesure ne dépend pas des
                # sondes qui précèdent.
                time.sleep(PAUSE_LIMITATION_S)
                with ThreadPoolExecutor(max_workers=REQUETES_PAR_RAFALE) as pool:
                    reponses = list(pool.map(
                        lambda _: self._poster_predict(url, requete, identifiants, contexte,
                                                       entete_ab=entete_ab, reessayer=False),
                        range(REQUETES_PAR_RAFALE)))
                codes = Counter(str(r.get("code")) for r in reponses)
                time.sleep(PAUSE_LIMITATION_S)
                apres = self._poster_predict(url, requete, identifiants, contexte,
                                             entete_ab=entete_ab, reessayer=False).get("code")
                comportement["rafale"] = {
                    "envoyees": REQUETES_PAR_RAFALE,
                    "codes": dict(codes),
                    "limite_declenchee": bool(codes.get("429") or codes.get("503")),
                    "retour_2xx_apres_pause": isinstance(apres, int) and 200 <= apres < 300,
                }
            except Exception:
                comportement["rafale"] = None

        # Répartition : après la rafale, chaque réplique v1 a-t-elle servi ?
        # Les services v1 sont les serveurs de l'upstream par défaut du map ;
        # à défaut, les noms de service en api-v1 / api_v1 / apis-v1.
        try:
            services_v1 = sondes_nginx.serveurs_upstream(texte, (ab_declare or {}).get("upstream_defaut"))
            conteneurs = list(self._conteneurs_du_projet())

            def service(c):
                return (getattr(c, "labels", None) or {}).get("com.docker.compose.service", c.name)
            repliques = [c for c in conteneurs if sondes_nginx.est_replique_v1(service(c), services_v1)]
            if not repliques and services_v1:
                repliques = [c for c in conteneurs if sondes_nginx.est_replique_v1(service(c), [])]
            if repliques:
                compte = {c.name: sondes_nginx.compter_requetes(c.logs(), chemin) for c in repliques}
                comportement["repartition"] = {"conteneurs": compte,
                                               "recoivent": sum(1 for n in compte.values() if n > 0)}
        except Exception:
            comportement["repartition"] = None

        comportement["monitoring"] = self._sonder_monitoring(sondes_racine)

        try:
            comportement["redirection_80_443"] = sondes_nginx.redirection_vers_https(sondes_racine)
        except Exception:
            comportement["redirection_80_443"] = None

        tls = {"tls_ancien_refuse": None, "certificat_valide": None}
        if base is not None:
            cible = urlsplit(base["url"])
            for champ, sonde_tls in (("tls_ancien_refuse", sondes_nginx.tls_ancien_refuse),
                                     ("certificat_valide", sondes_nginx.certificat_valide)):
                try:
                    tls[champ] = sonde_tls(cible.hostname, cible.port or 443)
                except Exception:
                    tls[champ] = None
        comportement["tls"] = tls

        self.record_step(
            "Sondes de comportement nginx",
            command=(f"POST {base['url'].rstrip('/')}{chemin} (A/B, mauvais identifiants, sans identifiants, rafale de "
                     f"{REQUETES_PAR_RAFALE}), logs des répliques v1, exporter/Prometheus/Grafana, "
                     "redirection du port 80, poignée de main TLS 1.1 et certificat")
                    if base is not None else
                    "pas de port TLS nginx joignable : sondes /predict et TLS non lancées",
            output=sondes_nginx.resume_comportement(comportement),
            exit_code=0,
            duration=time.time() - debut,
        )
        return comportement

    def _racine_monitoring(self, sondes_racine: list, mots: tuple, port: int, exclure: str | None = None):
        """L'URL hôte d'un service de monitoring : par son nom de conteneur,
        sinon par son port de conteneur habituel."""
        for sonde in sondes_racine:
            nom = str(sonde.get("service", "")).lower()
            if all(m in nom for m in mots) and not (exclure and exclure in nom) and isinstance(sonde.get("code"), int) \
                    and not str(sonde.get("note", "")).startswith("échec attendu"):
                return sonde["url"].rstrip("/")
        return racine_du_port(sondes_racine, port)

    def _identifiants_grafana(self) -> str:
        """GF_SECURITY_ADMIN_USER/PASSWORD du compose, sinon admin:admin
        (le défaut de l'image)."""
        utilisateur, mot_de_passe = "admin", "admin"
        try:
            import yaml
            with open(fichier_compose(self.eval_dir), encoding="utf-8") as lecteur:
                services = (yaml.safe_load(lecteur) or {}).get("services") or {}
            for nom, service in services.items():
                if "grafana" not in f"{nom} {service.get('image', '')}".lower():
                    continue
                env = service.get("environment") or {}
                if isinstance(env, list):
                    env = dict(e.split("=", 1) for e in env if isinstance(e, str) and "=" in e)
                u = str(env.get("GF_SECURITY_ADMIN_USER") or "")
                p = str(env.get("GF_SECURITY_ADMIN_PASSWORD") or "")
                # ${VAR} viendrait d'un .env que l'on ne résout pas ici.
                if u and "${" not in u:
                    utilisateur = u
                if p and "${" not in p:
                    mot_de_passe = p
                break
        except Exception:
            pass
        return f"{utilisateur}:{mot_de_passe}"

    def _sonder_monitoring(self, sondes_racine: list) -> dict:
        """exporter `nginx_up 1`, cibles Prometheus, provisionnement Grafana,
        par les ports publiés. Injoignable : None pour le champ."""
        import base64
        import json
        monitoring = dict(MONITORING_NON_MESURE)

        try:
            racine = self._racine_monitoring(sondes_racine, ("nginx", "exporter"), 9113)
            if racine:
                sonde = sonder(f"{racine}/metrics", taille_extrait=TAILLE_CORPS_SONDE)
                if sonde.get("code") == 200:
                    valeur = re.search(r"^nginx_up\s+(\S+)", sonde.get("extrait", ""), re.MULTILINE)
                    monitoring["exporter_nginx_up"] = bool(valeur) and float(valeur.group(1)) == 1
        except Exception:
            monitoring["exporter_nginx_up"] = None

        try:
            racine = self._racine_monitoring(sondes_racine, ("prometheus",), 9090, exclure="exporter")
            if racine:
                sonde = sonder(f"{racine}/api/v1/targets", taille_extrait=TAILLE_CORPS_SONDE)
                if sonde.get("code") == 200:
                    cibles = json.loads(sonde.get("extrait", ""))["data"]["activeTargets"]
                    monitoring["prometheus_cibles_up"] = sum(1 for c in cibles if c.get("health") == "up")
                    monitoring["prometheus_cibles_down"] = sum(1 for c in cibles if c.get("health") == "down")
        except Exception:
            monitoring["prometheus_cibles_up"] = monitoring["prometheus_cibles_down"] = None

        racine = self._racine_monitoring(sondes_racine, ("grafana",), 3000)
        if racine:
            jeton = base64.b64encode(self._identifiants_grafana().encode()).decode()
            for champ, chemin in (("grafana_datasources", "/api/datasources"),
                                  ("grafana_dashboards", "/api/search?type=dash-db")):
                try:
                    sonde = sonder(f"{racine}{chemin}", entetes={"Authorization": f"Basic {jeton}"},
                                   taille_extrait=TAILLE_CORPS_SONDE)
                    if sonde.get("code") == 200:
                        liste = json.loads(sonde.get("extrait", ""))
                        if isinstance(liste, list):
                            monitoring[champ] = len(liste)
                except Exception:
                    monitoring[champ] = None
        return monitoring

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

    def _diagnostic_up_rate(self, exc: Exception) -> str:
        """Pourquoi le `compose up` a échoué, pris avant le démontage.

        461416 : la trace ne disait que « returned non-zero exit status 1 ».
        testcontainers garde pourtant la sortie de la commande dans
        l'exception, et les conteneurs existent encore (le contexte n'a pas
        été ouvert, donc pas refermé) : leur état et les logs des services
        tombés disent ce qui a cassé.
        """
        parties = [str(exc)]
        for flux, contenu in (("stderr", getattr(exc, "stderr", None)), ("stdout", getattr(exc, "stdout", None))):
            if contenu:
                texte = contenu.decode("utf-8", errors="replace") if isinstance(contenu, bytes) else str(contenu)
                parties.append(f"--- {flux} du compose up (fin) ---\n{texte[-4000:].strip()}")
        try:
            conteneurs = self._conteneurs_du_projet()
        except Exception as erreur:
            parties.append(f"--- services : état illisible ({erreur}) ---")
            conteneurs = []
        if conteneurs:
            lignes = []
            for conteneur in conteneurs:
                try:
                    conteneur.reload()
                except Exception:
                    pass
                code = conteneur.attrs.get("State", {}).get("ExitCode")
                service = conteneur.labels.get("com.docker.compose.service", conteneur.name)
                lignes.append(f"{service} ({conteneur.name}) : {conteneur.status}"
                              + (f" ({code})" if conteneur.status != "running" else ""))
            parties.append("--- services ---\n" + "\n".join(lignes))
            for conteneur in conteneurs:
                if conteneur.status == "running":
                    continue
                try:
                    logs = conteneur.logs(tail=40).decode("utf-8", errors="replace").strip()
                except Exception as erreur:
                    logs = f"logs illisibles : {erreur}"
                service = conteneur.labels.get("com.docker.compose.service", conteneur.name)
                parties.append(f"--- logs de {service} (fin) ---\n{logs}")
        return "\n".join(parties)

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
        # Après `compose stop` : sans le .env, compose ne saurait plus
        # interpoler le fichier pour démonter la stack (469217).
        if self._env_cree:
            try:
                os.remove(self._env_cree)
                self._env_cree = None
            except FileNotFoundError:
                self._env_cree = None
            except OSError as e:
                errors.append(f".env du harnais non retiré : {e}")
        self.record_step(
            "Nettoyage Compose",
            command="docker compose down et suppression des ressources d'examen",
            output="ok" if not errors else " ; ".join(errors),
            exit_code=0 if not errors else 1,
            duration=time.time() - started,
        )

    def _retenir_images_de_la_copie(self) -> None:
        """Seules les images construites pour cette copie : elles portent le
        nom du projet compose (eval_<id>, posé par COMPOSE_PROJECT_NAME).
        `nginx:latest` et autres images publiques restent, elles resserviront.
        Le préfixe était le nom du dossier, devenu faux avec le projet par copie."""
        try:
            prefixe = self.project_name.lower()
            for image in self._images_du_projet():
                if image.lower().startswith(prefixe):
                    self.record_exam_image(image)
        except Exception as erreur:
            self.logger.debug(f"images non recensées : {erreur}")

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
