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
from . import sondes_nginx, sondes_prometheus
from .sonde_http import contexte_tls_permissif, resume, sonder
from .config import SERVICE_READY_TIMEOUT, FLASK_READY_PATTERN, REESSAI_SANTE_MAX
from . import diagnostic_demarrage
from . import lancement
from . import reconstruction_datee


# Les quatre noms que docker compose accepte lui-meme, dans son ordre de
# priorite : compose.yaml > compose.yml > docker-compose.yaml >
# docker-compose.yml (on garde ce dernier en tete, c'est le nom demande par
# les enonces).
NOMS_COMPOSE = ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")
_ENTETE_APPLEDOUBLE = b"\x00\x05\x16\x07"
_DOSSIERS_IGNORES = {".git", ".venv", "venv", "node_modules", "__pycache__", "__MACOSX"}

# Réglages DNS d'un service, retirés du compose réécrit (469634).
_CLES_DNS = ("dns", "dns_search", "dns_opt")


def _decrire_dns(reglages: dict) -> str:
    """`dns: [8.8.8.8, 1.1.1.1], dns_search: x` : les clés retirées, lisibles."""
    def valeur(v):
        return f"[{', '.join(map(str, v))}]" if isinstance(v, list) else str(v)
    return ", ".join(f"{cle}: {valeur(v)}" for cle, v in reglages.items())


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


def identifiants_mentionnes(eval_dir: str, couple: str) -> str | None:
    """Le fichier de la copie qui écrit ce couple, hors `--user`/`-u` : le
    couple littéral `user:pass`, ou l'utilisateur et le mot de passe sur une
    même ligne qui parle d'identifiants (« `admin` / mot de passe `admin` »).
    Un `.htpasswd` ne compte pas : il porte un hash, pas le mot de passe."""
    utilisateur, _, mot_de_passe = couple.partition(":")
    if not utilisateur or not mot_de_passe:
        return None
    mot = re.compile(rf"(?<![\w$]){re.escape(mot_de_passe)}(?![\w])")
    contexte = re.compile(r"pass|mot de passe|mdp|identifiant|credential|login|user|utilisateur|auth", re.IGNORECASE)
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = sorted(d for d in dossiers if d not in (".git", ".venv", "__pycache__", "node_modules"))
        for nom in sorted(fichiers):
            if not (nom.lower().endswith((".sh", ".md", ".yml", ".yaml", ".txt", ".py", ".json", ".http"))
                    or nom in ("Makefile", "makefile")):
                continue
            try:
                with open(os.path.join(racine, nom), encoding="utf-8", errors="replace") as lecteur:
                    texte = lecteur.read()
            except OSError:
                continue
            source = os.path.relpath(os.path.join(racine, nom), eval_dir)
            if couple in texte:
                return source
            for ligne in texte.splitlines():
                if utilisateur in ligne and mot.search(ligne.replace(utilisateur, "", 1)) and contexte.search(ligne):
                    return source
    return None


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
                url = re.search(r"(https?)://(?:localhost|127\.0\.0\.1)(?::(\d+))?(/[^\s\"']*)", ligne)
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
                    "chemin": url.group(3).rstrip(")\"'"),
                    # Le port visé : `localhost:9090/api/v1/...` parle à
                    # Prometheus, pas au reverse proxy (469145).
                    "port": int(url.group(2)) if url.group(2) else None,
                    # Le schéma déclaré : sur un port TLS relâché, la sonde
                    # http rend 400 (« preuve TLS »), pas une réponse à rejouer.
                    "schema": url.group(1),
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
                           "sans_identifiants": None, "identifiants_acceptes": None, "identifiants_source": None,
                           "repartition": None,
                           "redirection_80_443": None, "make_test": None}

# `make test`, la validation finale de l'énoncé, se joue dans un conteneur
# jetable : jamais sur l'hôte (shell arbitraire de l'apprenant), jamais avec
# le socket Docker. Image construite une fois, réutilisée ; surchargeable.
# Le même conteneur joue la commande de lancement de l'énoncé (lancement.py) :
# ses prérequis génèrent certificats, .htpasswd et .env avec openssl,
# htpasswd (apache2-utils), envsubst (gettext) ou python3. Un outil absent
# rend le lancement « non mesuré » (code 127), jamais un zéro ; la version
# du tag change avec la liste pour ne pas réutiliser une image en cache.
IMAGE_MAKE_TEST = os.environ.get("EXAM_DOCKER_EVAL_IMAGE_MAKE_TEST", "scriptorium-make-test:2")
DOCKERFILE_MAKE_TEST = ("FROM alpine:3.20\n"
                        "RUN apk add --no-cache bash curl make socat coreutils grep sed gawk openssl jq "
                        "apache2-utils gettext python3\n")
DELAI_MAKE_TEST_S = 120
# Au-delà du délai du script : installation des relais, copie de l'archive.
MARGE_MAKE_TEST_S = 60


def client_docker():
    import docker
    return docker.from_env()
MONITORING_NON_MESURE = {"exporter_nginx_up": None, "prometheus_cibles_up": None,
                         "prometheus_cibles_down": None, "grafana_datasources": None,
                         "grafana_dashboards": None}

# Sondes de comportement prometheus-grafana (467439, 466056). L'API de ces
# copies télécharge ses données et entraîne au démarrage : après l'attente
# fixe, sa racine peut encore se taire. On lui laisse ATTENTE_API_S pour
# exposer /metrics, puis ATTENTE_CIBLES_S à Prometheus pour la scraper (une
# cible pas encore scrapée est `unknown`, pas `down`). Le rapport Evidently
# de /trigger-drift dépasse largement les 10 s par défaut sur 2 vCPU.
ATTENTE_API_S = 60
ATTENTE_CIBLES_S = 30
PAS_ATTENTE_S = 5
DELAI_DERIVE_S = 90
MAXI_TABLEAUX = 20
# Ports et noms qui ne sont pas l'API : la stack de monitoring elle-même.
PORTS_MONITORING = {"9090", "3000", "9100", "9113", "9093"}
MOTS_MONITORING = ("prometheus", "grafana", "exporter", "alertmanager", "cadvisor")


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


def _plage(texte: str) -> list:
    """`"8080"` → [8080], `"6000-6001"` → [6000, 6001] ; [] si illisible."""
    debut, _, fin = str(texte).strip().partition("-")
    try:
        debut, fin = int(debut), int(fin or debut)
    except ValueError:
        return []
    return list(range(debut, fin + 1)) if 0 < debut <= fin else []


def publications_hote(texte_compose: str, env: dict) -> dict:
    """{port hôte : [(service, port conteneur)]} tel que le compose D'ORIGINE
    les publie, variables `${PORT:-8082}` résolues contre `env`.

    Formes : `"H:C"`, `"IP:H:C"`, `"[::1]:H:C"`, `H:C/tcp`, plages
    `"H1-H2:C1-C2"`, forme longue `published`/`target`. Un port sans hôte fixe
    (`"5000"`, `"IP::C"`, `target` seul) n'a pas d'adresse déclarée : absent."""
    try:
        import yaml
        services = (yaml.safe_load(texte_compose) or {}).get("services") or {}
    except Exception:
        return {}
    publies: dict = {}
    if not isinstance(services, dict):
        return publies
    for nom, service in services.items():
        if not isinstance(service, dict):
            continue
        for entree in service.get("ports") or []:
            if isinstance(entree, dict):
                hote, conteneur = entree.get("published"), entree.get("target")
            elif isinstance(entree, str):
                # Résoudre d'abord : `${P:-3033}:3000` porte un « : » dans son défaut.
                entree = sondes_prometheus.resoudre_variable(entree, env)
                parties = (entree or "").split("/")[0].rsplit(":", 2)
                if len(parties) < 2:
                    continue
                hote, conteneur = parties[-2], parties[-1]
            else:
                continue
            if hote is None or conteneur is None:
                continue
            hotes = _plage(sondes_prometheus.resoudre_variable(str(hote), env) or "")
            conteneurs = _plage(sondes_prometheus.resoudre_variable(str(conteneur), env) or "")
            if len(conteneurs) == 1:
                conteneurs = conteneurs * len(hotes)
            if not hotes or len(hotes) != len(conteneurs):
                continue
            for h, c in zip(hotes, conteneurs):
                if (nom, c) not in publies.setdefault(h, []):
                    publies[h].append((nom, c))
    return publies


def ports_hote_publies(dossier: str) -> dict:
    """`publications_hote` du fichier Compose de `dossier` et de son .env."""
    chemin = fichier_compose(dossier)
    if not chemin:
        return {}
    try:
        with open(chemin, encoding="utf-8", errors="replace") as lecteur:
            texte = lecteur.read()
    except OSError:
        return {}
    return publications_hote(texte, sondes_prometheus.lire_env(os.path.join(dossier, ".env")))


def racine_du_port_hote(sondes_racine: list, port: int, publies: dict, schema: str | None = None):
    """L'URL relâchée d'une requête qui vise `localhost:<port>` (port hôte).

    Rend (url ou None, cible « service:conteneur » ou None). 467379 : le
    compose publie "8082:8080", Docker relâche 8080 vers 32801 ; la requête
    du Makefile sur 8082 part vers 32801. Cible publiée mais muette : l'URL
    http de ce port conteneur, le rejeu dira ce qu'il obtient. Hors compose
    (pas de fichier lisible, port absent), on retombe sur le port conteneur."""
    for service, conteneur in publies.get(port, []):
        candidates = [s for s in sondes_racine
                      if str(s.get("port", "")).split("/")[0] == str(conteneur)
                      and not str(s.get("note", "")).startswith("échec attendu")]
        avec_service = [s for s in candidates if s.get("compose_service") == service] or \
                       [s for s in candidates if not s.get("compose_service")
                        and service in str(s.get("service", ""))] or \
                       [s for s in candidates if not s.get("compose_service")]
        repond = [s for s in avec_service if isinstance(s.get("code"), int)
                  and not str(s.get("note", "")).startswith("preuve TLS")]
        repond = [s for s in repond if schema and s["url"].startswith(f"{schema}:")] or repond
        choix = (repond or [s for s in avec_service if s["url"].startswith("http:")] or avec_service)
        if choix:
            return choix[0]["url"].rstrip("/"), f"{service}:{conteneur}"
    if port in publies:
        return None, ", ".join(f"{s}:{c}" for s, c in publies[port])
    return racine_du_port(sondes_racine, port), None


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


# Panne réseau pendant le build (460059 : `pip install` → ReadTimeoutError
# vers files.pythonhosted.org). Propre au build : apt, registre d'images, et
# les erreurs urllib3/requests déjà reconnues à l'exécution. Ces motifs ne
# servent pas à `diagnostic_demarrage.classer_service`, qui lit les logs d'exécution.
_MOTIFS_RESEAU_BUILD = re.compile(
    r"Temporary failure resolving '([^']+)'|Could not connect to ([A-Za-z0-9.\-]+)"
    r"|failed to resolve source metadata for ([^\s:]+)|dial tcp:? lookup ([A-Za-z0-9.\-]+)"
    r"|TLS handshake timeout|i/o timeout|net/http: request canceled",
    re.IGNORECASE,
)
_BLOC_ECHEC = re.compile(r"^\s*>\s*\[[^\]]+\][^\n]*:\s*$")


def _preuves_du_build(sortie: str) -> list:
    """Les lignes qui disent pourquoi le build a échoué : le bloc de l'étape
    en échec (` > [4/5] RUN …:` jusqu'à `------`) et les `failed to solve`.
    Sans bloc, toute la sortie. Les avertissements (pip `WARNING: Retrying`)
    ne prouvent rien : une étape qui échoue ensuite pour une autre raison
    reste celle-là."""
    lignes, preuves, dans_bloc = sortie.splitlines(), [], False
    for ligne in lignes:
        if _BLOC_ECHEC.match(ligne):
            dans_bloc = True
            continue
        if dans_bloc and ligne.startswith("------"):
            dans_bloc = False
            continue
        if dans_bloc or "failed to solve" in ligne:
            preuves.append(ligne)
    if not preuves:
        preuves = lignes
    return [l for l in preuves if not re.search(r"\bWARNING\b|Retrying", l)]


def classer_echec_build(sortie: str) -> dict | None:
    """Une panne réseau vers l'extérieur pendant le build, ou None.

    Panne d'infrastructure (imputation non_mesure, faute « systeme ») :
    jamais l'apprenant. Un hôte interne (localhost, service de la stack)
    n'est pas une panne réseau."""
    preuves = _preuves_du_build(sortie or "")
    preuve = diagnostic_demarrage._reseau_sortant("\n".join(preuves))
    if not preuve:
        for ligne in preuves:
            m = _MOTIFS_RESEAU_BUILD.search(ligne)
            hotes = [h for h in (m.groups() if m else ()) if h]
            if m and (not hotes or any(diagnostic_demarrage._hote_externe(h.split("/")[0]) for h in hotes)):
                preuve = ligne.strip()[:300]
                break
    if not preuve:
        return None
    return {"cause": "reseau_build", "imputation": diagnostic_demarrage.NON_MESURE, "reessayable": True,
            "preuve": f"panne réseau pendant le build (infrastructure, pas la copie) : {preuve}"}


def _avec_echelles(compose, echelles: dict):
    """Ajouter `--scale service=N` au `up` de ce DockerCompose.

    testcontainers n'a pas d'option de mise à l'échelle : la ligne `up` qu'il
    construit est complétée au passage, sur cette instance seulement, si bien
    que le réessai de `_DemarrageSansDemontage` (nouveau start() sur le même
    objet) la garde. Un `start()` dont la ligne `up` n'a pas reçu les
    `--scale` lève : une version de testcontainers qui changerait sa ligne
    ne doit pas ramener silencieusement à une réplique (banc nginx, 461091).
    """
    if not echelles:
        return compose
    options = [x for service, n in echelles.items() for x in ("--scale", f"{service}={n}")]
    lancer, demarrer = compose._run_command, compose.start
    vus = []

    def run(cmd, *args, **kwargs):
        base = list(compose.compose_command_property)
        if isinstance(cmd, list) and cmd[:len(base)] == base and cmd[len(base):len(base) + 1] == ["up"]:
            cmd = [*cmd[:len(base) + 1], *options, *cmd[len(base) + 1:]]
            vus.append(cmd)
        return lancer(cmd, *args, **kwargs)

    def start():
        avant = len(vus)
        demarrer()
        if len(vus) == avant:
            raise RuntimeError("`--scale` non transmis : la ligne `up` de testcontainers a changé")

    compose._run_command, compose.start = run, start
    return compose


class _DemarrageSansDemontage:
    """`with compose:` sans le démontage que testcontainers fait sur un `up`
    raté : son __enter__ appelle __exit__ en cas d'échec, et les conteneurs
    disparaissaient avant que le moteur ne relève leur état et leurs logs
    (461416). Le démontage reste garanti par cleanup().

    `sur_echec(exc, reessai_possible)` est appelé sur un `up` raté, stack
    encore debout : il relève les services non sains et rend True pour
    relancer le `up` une fois (healthcheck seulement trop lent, 467094)."""

    def __init__(self, compose, sur_echec=None, deja_demarre=False):
        self.compose = compose
        self.sur_echec = sur_echec
        # Stack déjà levée avec les arguments de la recette de la copie.
        self.deja_demarre = deja_demarre

    def __enter__(self):
        if self.deja_demarre:
            return self.compose
        try:
            self.compose.start()
        except Exception as exc:
            if not (self.sur_echec and self.sur_echec(exc, True)):
                raise
            try:
                self.compose.start()
            except Exception as second:
                self.sur_echec(second, False)
                raise
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
        # Cause d'un `up --wait` raté, classée (467094) : rendue dans le
        # résultat pour que le barème distingue la faute de la copie de ce
        # que nous n'avons pas pu mesurer.
        self._cause_demarrage = None
        self._debut_evaluation = time.time()
        # Passage « lancement tel que livré » (16/10) : la commande de l'énoncé
        # jouée dans le conteneur jetable, et les réparations du moteur quand
        # elle échoue. Fichiers produits par la recette et rapatriés : retirés
        # (ou rendus à leur contenu d'origine) au nettoyage.
        self._lancement = None
        self._reparations = []
        self._env_fabrique = None
        self._fichiers_rapatries = []
        self._originaux = {}
        # Reconstruction datée (467094) : date de référence du rendu, relevée
        # avant toute écriture du harnais dans la copie de travail, et
        # résultat de l'unique reconstruction tentée.
        self._date_reference = None
        self._reconstruction = None
        self._fichiers_harnais = []
        # {chemin: contenu} des AppleDouble `._*` écartés avant le lancement
        # (460188), remis au nettoyage.
        self._appledouble = {}
        # {service: {clé DNS: valeur d'origine}} retirés du compose réécrit.
        self._dns_retires = {}
        # `--scale` de la recette gardés par le up par défaut et la
        # reconstruction datée (banc nginx, 461091).
        self._echelles_up = {}
        # Profils de la recette gardés par ces mêmes up quand l'échec du up de
        # la recette n'est pas imputé à la copie (458925 : le monitoring).
        self._profils_up = []

    def _commande_up(self, compose_name: str) -> str:
        """La ligne du up par défaut, avec les profils et `--scale` gardés."""
        return (f"docker compose -f {compose_name}"
                + "".join(f" --profile {p}" for p in self._profils_up)
                + " up --build"
                + "".join(f" --scale {s}={n}" for s, n in self._echelles_up.items()))

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

    def _relax_host_ports(self, compose_file: str, nom_sortie: str = "docker-compose.eval.yml") -> str:
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
            return os.path.relpath(compose_file, self.eval_dir)

        try:
            with open(compose_file, encoding="utf-8") as handle:
                doc = yaml.safe_load(handle)
        except Exception as exc:
            self.logger.warning(f"docker-compose.yml illisible ({exc}) : ports laisses tels quels")
            self._port_relaxation_outcome = f"Échec explicite : compose illisible ({exc})."
            self._port_relaxation_exit_code = 2
            return os.path.relpath(compose_file, self.eval_dir)

        if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
            self._port_relaxation_outcome = "Absence explicite : aucun service Compose exploitable."
            self._port_relaxation_exit_code = 0
            return os.path.relpath(compose_file, self.eval_dir)

        changed = False
        alias = []
        dns_retires = {}
        for nom_service, service in doc["services"].items():
            if isinstance(service, dict) and self._nom_fixe_en_alias(service):
                alias.append(nom_service)
                changed = True
            # 469634 : `dns: [8.8.8.8, 1.1.1.1]` contournait le résolveur de
            # la machine de correction, qui atteint pourtant l'hôte visé. Comme
            # un port hôte, un réglage DNS lie la copie au réseau où elle
            # tourne : retiré, et consigné.
            reglages = {cle: service.pop(cle) for cle in _CLES_DNS
                        if isinstance(service, dict) and cle in service}
            if reglages:
                dns_retires[nom_service] = reglages
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
            return os.path.relpath(compose_file, self.eval_dir)

        relaxed_name = nom_sortie
        relaxed_path = os.path.join(self.eval_dir, relaxed_name)
        try:
            with open(relaxed_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(doc, handle, sort_keys=False)
        except Exception as exc:
            self.logger.warning(f"Copie du compose impossible ({exc}) : ports laisses tels quels")
            self._port_relaxation_outcome = f"Échec explicite : copie impossible ({exc})."
            self._port_relaxation_exit_code = 2
            return os.path.relpath(compose_file, self.eval_dir)

        self.logger.info("Ports hote relaches : Docker en attribuera de libres")
        self._dns_retires.update(dns_retires)
        self._port_relaxation_outcome = f"Ports hôte relâchés dans {relaxed_name}." + (
            f" Noms de conteneur fixes devenus alias réseau : {', '.join(alias)}." if alias else "") + (
            " Réglages DNS de la copie retirés : "
            + "; ".join(f"{s} ({_decrire_dns(r)})" for s, r in dns_retires.items())
            + "." if dns_retires else "")
        self._port_relaxation_exit_code = 0
        return relaxed_name

    def _preparer_env(self, compose_file: str, fichiers: list | None = None) -> None:
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
        # Un .env produit par la recette de la copie (make env, rapatrié du
        # conteneur jetable) se complète s'il lui manque une variable requise ;
        # un .env livré ne se touche jamais.
        produit = cible in self._fichiers_rapatries and os.path.isfile(cible) and not os.path.islink(cible)
        if os.path.lexists(cible) and not produit:
            return
        requises = []
        for fichier in fichiers or [compose_file]:
            try:
                with open(fichier, encoding="utf-8", errors="replace") as lecteur:
                    requises += [n for n in variables_requises(lecteur.read()) if n not in requises]
            except OSError:
                continue
        if produit:
            requises = [n for n in requises if n not in lancement.variables_definies(cible)]
        if not requises:
            return

        def generer():
            return secrets.token_hex(12)

        if produit:
            try:
                with open(cible, encoding="utf-8", errors="replace") as lecteur:
                    texte, completes = completer_env(lecteur.read(), requises, generer)
                with open(cible, "w", encoding="utf-8") as sortie:
                    sortie.write(texte)
            except OSError as erreur:
                self.record_step("Variables d'environnement de la copie", command="complément du .env produit",
                                 output=f"Échec explicite : .env non complété ({erreur}).", exit_code=2,
                                 duration=time.time() - debut)
                return
            self._env_fabrique = {"variables": completes,
                                  "source": "le .env produit par la recette, complété par le harnais"}
            self.record_step(
                "Variables d'environnement de la copie",
                command="complément du .env produit par la recette (valeurs non consignées)",
                output=(f"Le .env produit par la commande de lancement ne définit pas : {', '.join(completes)}, "
                        "que le compose exige sans défaut. Complété par le harnais : réparation."),
                exit_code=0,
                duration=time.time() - debut,
            )
            return

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
        self._env_fabrique = {"variables": list(requises), "source": source}
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

        run_started = self._debut_evaluation = time.time()
        self.record_submission_step()
        self._relever_date_reference()
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
            self._consigner_date_reference()
            self._ecarter_appledouble()

            # Passage « lancement tel que livré » (politique du 16/10) : la
            # commande de l'énoncé, jouée par make dans le conteneur jetable.
            # Avant le .env du harnais et le compose relâché : ce que la
            # recette produit doit venir d'elle.
            passage = self._jouer_lancement()
            up = None
            if passage is not None:
                self._lancement = passage["tel_que_livre"]
                up = passage["up"]
            fichiers_recette = [os.path.join(self.eval_dir, f) for f in (up or {}).get("fichiers", [])]
            absents = [os.path.relpath(f, self.eval_dir) for f in fichiers_recette if not os.path.isfile(f)]
            if absents:
                self._lancement = dict(self._lancement, code=1, cause="copie", motif="up_en_echec",
                                       extrait=f"`{' '.join(self._lancement['up'])}` vise un fichier Compose absent "
                                               f"de la copie : {', '.join(absents)} (constat statique)")
                up, fichiers_recette = None, []
            env_recette = [os.path.join(self.eval_dir, f) for f in (up or {}).get("env_files", [])]

            # La recette atteint `compose up`, mais sans ses variables requises
            # compose refuserait d'interpoler (469217 sans `make env`) : la
            # commande échoue telle que livrée. `config` le constate, avant que
            # le harnais ne fabrique le .env.
            reussi = bool(self._lancement) and self._lancement["code"] == 0 and self._lancement["cause"] is None
            manquantes = self._variables_env_manquantes(compose_file, fichiers_recette, env_recette)
            config_tel_que_livre = None
            if reussi and manquantes:
                code, extrait = self._config_tel_que_livre(
                    [os.path.relpath(f, self.eval_dir) for f in fichiers_recette] or
                    [os.path.relpath(compose_file, self.eval_dir)],
                    [os.path.relpath(f, self.eval_dir) for f in env_recette])
                config_tel_que_livre = (code, extrait)
                if code != 0:
                    self._lancement = dict(
                        self._lancement, code=code if isinstance(code, int) else 1, cause="copie",
                        motif="variables_requises",
                        extrait=(f"après `{self._lancement['commande']}`, le compose exige sans valeur : "
                                 f"{', '.join(manquantes)}\n{extrait}").strip())

            # Avant tout `up` : sans ses variables requises, compose refuse
            # d'interpoler et la stack ne démarre pas (469217).
            self._preparer_env(compose_file, fichiers_recette or None)
            if self._env_fabrique:
                lance = self._lancement or {}
                if config_tel_que_livre is not None:
                    tel = {"commande": " ".join(lance.get("up") or ["docker", "compose", "up"]),
                           "code": config_tel_que_livre[0], "extrait": config_tel_que_livre[1]}
                else:
                    tel = {"commande": "docker compose up", "code": None,
                           "extrait": "variables requises sans valeur dans la copie livrée : "
                                      f"{', '.join(self._env_fabrique['variables'])} (constat statique : compose "
                                      "refuse d'interpoler)"}
                self._reparations.append({
                    "id": "env_fabrique",
                    "critere_touche": "services_debout",
                    "tel_que_livre": tel,
                    "applique": f".env fourni par le harnais ({self._env_fabrique['source']}) pour : "
                                f"{', '.join(self._env_fabrique['variables'])} — valeurs non consignées",
                })

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

            # Le `up` de la recette, s'il diffère de celui du moteur (fichiers
            # `-f`, profils, --env-file, services) : joué tel quel d'abord. Un
            # échec là, puis le up par défaut : la commande de l'énoncé échoue
            # telle que livrée, le reste se mesure avec la réparation.
            commande_up = f"docker compose -f {compose_name} up --build"
            deja_demarre = False
            relatifs = [os.path.relpath(f, self.eval_dir) for f in fichiers_recette]
            echelles = dict((up or {}).get("echelles") or {})
            personnalise = up is not None and (
                relatifs not in ([], [os.path.relpath(compose_file, self.eval_dir)])
                or bool(up["profils"] or up["env_files"] or up["services"] or echelles))
            if personnalise:
                noms = []
                for chemin in fichiers_recette or [compose_file]:
                    if os.path.samefile(chemin, compose_file):
                        noms.append(compose_name)
                        continue
                    relatif = os.path.relpath(chemin, self.eval_dir)
                    racine, extension = os.path.splitext(relatif)
                    noms.append(self._relax_host_ports(chemin, nom_sortie=f"{racine}.eval{extension or '.yml'}"))
                commande_recette = (f"docker compose {' '.join(f'-f {n}' for n in noms)}"
                                    + "".join(f" --profile {p}" for p in up["profils"])
                                    + "".join(f" --env-file {e}" for e in up["env_files"])
                                    + " up --build"
                                    + "".join(f" --scale {s}={n}" for s, n in echelles.items())
                                    + "".join(f" {s}" for s in up["services"]))
                recette_started = time.time()
                try:
                    self.compose = _avec_echelles(DockerCompose(
                        context=self.eval_dir, compose_file_name=noms if len(noms) > 1 else noms[0],
                        pull=False, build=True, env_file=up["env_files"] or None,
                        profiles=up["profils"] or None, services=up["services"] or None), echelles)
                    # Même démarrage que le up du moteur : réessai sur un
                    # healthcheck seulement lent (467094), cause classée.
                    _DemarrageSansDemontage(self.compose, self._diagnostiquer_up_rate).__enter__()
                    deja_demarre = True
                    commande_up = commande_recette
                except Exception as exc:
                    imputation = (self._cause_demarrage or {}).get("imputation")
                    self.record_step(
                        "Build et démarrage Compose (arguments de la recette)",
                        command=commande_recette,
                        output=(f"imputation : {imputation or 'non classée'}\n"
                                + lancement.masquer_secrets(str(exc))[-800:]),
                        exit_code=1,
                        duration=time.time() - recette_started,
                    )
                    try:
                        if self.compose is not None:
                            self.compose.stop()
                    except Exception:
                        pass
                    self.compose = None
                    # Le up par défaut repart sans la cause du premier. Il
                    # garde les profils de la recette, sauf faute établie de
                    # la copie : la réparation mesure alors le reste sans eux.
                    self._cause_demarrage = None
                    if imputation != "copie":
                        self._profils_up = list(up["profils"])
                    extrait = (f"`{' '.join(self._lancement['up'])}` (le up de la recette) échoue "
                               f"(imputation {imputation or 'non classée'}) : "
                               f"{lancement.masquer_secrets(str(exc))[-600:]}")
                    # Seule une faute établie de la copie coûte : un up
                    # interrompu par le réseau, un délai ou une cause
                    # indéterminée n'est pas mesuré.
                    self._lancement = dict(self._lancement, code=1, cause="copie", motif="up_en_echec",
                                           extrait=extrait) if imputation == "copie" else \
                        dict(self._lancement, code=None, cause="non_mesure", motif="up_en_echec_non_impute",
                             extrait=extrait)
            if not deja_demarre:
                self._noter_reparations_de_lancement(compose_name)
                # Le up par défaut, et la reconstruction datée qui le
                # remplace, gardent les `--scale` de la recette pour les
                # services de ce compose : la topologie voulue par la copie
                # (répartition de charge) n'est pas une réparation. Les
                # profils aussi, sauf faute établie de la copie (plus haut).
                connus = set(self._services_du_compose(os.path.join(self.eval_dir, compose_name)))
                self._echelles_up = {s: n for s, n in echelles.items() if s in connus}
                commande_up = self._commande_up(compose_name)
                self.compose = _avec_echelles(DockerCompose(
                    context=self.eval_dir,
                    compose_file_name=compose_name,
                    pull=False,  # Use local builds
                    build=True,  # Build images before starting
                    env_file=None,  # No .env file needed
                    profiles=self._profils_up or None,
                ), self._echelles_up)

            # Start services with context manager (auto-cleanup on exit)
            self.logger.info("Starting Docker Compose services...")
            compose_started = time.time()
            try:
                demarrage = _DemarrageSansDemontage(self.compose, self._diagnostiquer_up_rate, deja_demarre)
                if not deja_demarre:
                    try:
                        demarrage.__enter__()
                    except Exception as echec_up:
                        # Une seule reconstruction, aux versions de la date du
                        # rendu, sur un conflit de versions (467094).
                        nom_datee = self._tenter_reconstruction_datee(compose_name, echec_up)
                        if nom_datee is None:
                            raise
                        compose_name = nom_datee
                        commande_up = self._commande_up(nom_datee) + " (reconstruction datée)"
                    demarrage = _DemarrageSansDemontage(self.compose, self._diagnostiquer_up_rate, True)
                with demarrage:
                    if (self._cause_demarrage or {}).get("cause") == "reseau_build" \
                            and getattr(self, "_etape_diagnostic", None) is not None:
                        # La panne réseau n'a pas persisté : le build relancé a passé.
                        self._etape_diagnostic["exit_code"] = 0
                        self._etape_diagnostic["output"] += "\nréessai : le build relancé passe."
                    self.record_step(
                        "Build et démarrage Compose",
                        command=commande_up,
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
                    self._compose_name = compose_name
                    # Convention d'examen declarable (pi_stack_mode, #49) :
                    # serving | pipeline | auto (defaut : deviner par la
                    # presence d'un service `pipeline`).
                    mode = os.environ.get("EXAM_STACK_MODE", "auto")
                    servante = mode == "serving" or (mode != "pipeline" and "pipeline" not in services)
                    if servante:
                        self.logger.info(f"Stack servante ({'déclarée' if mode == 'serving' else services})")
                        result = self._avec_cause(self._evaluer_services_persistants())
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
                    self._avec_cause(result)

                    self._log_execution_end(result)
                    return result
            except Exception as exc:
                if not any(step["title"] == "Build et démarrage Compose" for step in self.steps):
                    self.record_step(
                        "Build et démarrage Compose",
                        command=commande_up,
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
            result = self._avec_cause({"success": False, "error": error_msg, "exit_code": 3, "steps": self.steps})
            self._log_execution_end(result)
            return result

        except Exception as e:
            error_msg = f"Execution error: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            result = self._avec_cause({"success": False, "error": error_msg, "exit_code": 2, "steps": self.steps})
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
                        sonde = {"service": c.name, "port": interne, "url": url,
                                 "compose_service": (getattr(c, "labels", None) or {}).get(
                                     "com.docker.compose.service")}
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
        # Avant les sondes : une stack tombée sur un conflit de versions est
        # reconstruite, et seule la stack évaluée est sondée (461338).
        boucles = self._boucles_de_redemarrage()
        etats, boucles = self._reconstruire_si_conflit(etats, boucles)

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

        # Même grille, examen prometheus-grafana (467439, 466056) : le barème
        # lit `prometheus_comportement`, pas les mots-clés des fichiers.
        prometheus = None
        if comportement is None and sondes_prometheus.est_examen_prometheus(self.eval_dir):
            try:
                prometheus = self._sonder_comportement_prometheus(sondes)
            except Exception as erreur:
                prometheus = dict.fromkeys(sondes_prometheus.CHAMPS)
                self.record_step("Sondes de comportement Prometheus",
                                 output=f"non mesurées : erreur interne du harnais ({erreur})", exit_code=0)

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
        if prometheus is not None:
            resultat["prometheus_comportement"] = prometheus
        return resultat

    def _reconstruire_si_conflit(self, etats: dict, boucles: set) -> tuple:
        """Services en échec après un `up` réussi : même diagnostic que pour
        un `up` raté et, sur un conflit de versions, la même reconstruction
        datée unique (461338, 460188 : pas de healthcheck bloquant, le `up`
        passe puis bike-api redémarre en boucle sur « pyarrow requires NumPy
        2.0 »). Rend (etats, boucles) de la stack à évaluer."""
        marques = dict(etats)
        for nom in boucles:
            marques[nom] = ("restarting", 1)
        _, morts = self._classer_services(marques)
        if not morts or self._reconstruction is not None or self._cause_demarrage is not None \
                or not getattr(self, "_compose_name", None):
            return etats, boucles
        debut = time.time()
        releve = self._releve_sante()
        noms = diagnostic_demarrage.services_non_sains(
            {s: a for s, (_, a) in releve.items()}, {s for s, (c, _) in releve.items() if c.name in boucles})
        if not noms:
            return etats, boucles
        self._consigner_diagnostic(releve, noms, "", time.time() - debut)
        if not reconstruction_datee.doit_reconstruire(self._cause_demarrage):
            return etats, boucles
        derniers = {}

        def verifier():
            self._wait_for_services()
            derniers["boucles"] = self._boucles_de_redemarrage()
            observes = self._etats_du_projet()
            for nom in derniers["boucles"]:
                observes[nom] = ("restarting", 1)
            return sorted(self._classer_services(observes)[1])
        origine = self.compose
        self._tenter_reconstruction_datee(
            self._compose_name, RuntimeError(f"service(s) en échec après le démarrage : {sorted(morts)}"), verifier)
        if self.compose is origine:
            return etats, boucles  # rien n'a été relancé
        # La stack d'origine est arrêtée : l'évaluation porte sur la reconstruite.
        etats = self._record_service_states()
        return etats, derniers.get("boucles") if "boucles" in derniers else self._boucles_de_redemarrage()

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
                    # Ceux de la copie d'abord, le cours en dernier (16/10).
                    candidats = [(c, f"déclarés dans la copie ({fichier})" if origine == "copie" else
                                  "énoncé" if origine == "énoncé" else "du cours")
                                 for c, origine, fichier in self._identifiants_candidats({})]
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
        # Le port déclaré est un port HÔTE du compose d'origine (467379 :
        # "8082:8080", relâché par Docker vers 32801).
        publies = ports_hote_publies(self.eval_dir)
        for requete in (r for r in requetes if r.get("port") not in (None, 80, 443)):
            racine_url, cible = racine_du_port_hote(sondes_racine, requete["port"], publies,
                                                    requete.get("schema"))
            if not racine_url:
                self.record_step(
                    f"Requête déclarée ({requete['source']}) : {requete['methode']} {requete['chemin']}",
                    command=f"{requete['methode']} localhost:{requete['port']}{requete['chemin']}",
                    output=(f"service publié mais absent : le port hôte {requete['port']} publie {cible}, "
                            "et aucun conteneur debout ne l'expose") if cible else
                           (f"non rejouée : {requete['source']} vise localhost:{requete['port']}, que le "
                            "compose ne publie sur aucun port hôte — absence de mesure, pas une panne du service"),
                    exit_code=1 if cible else 0,
                )
                continue
            resultats.append(self._rejouer_requete(requete, racine_url, contexte, {"service": None,
                                                                                  "port": requete["port"]},
                                                   detour=cible and f"déclaré localhost:{requete['port']} → {cible}"))
        for base in bases:
            racine_url = base["url"].rstrip("/")
            for requete in vers_proxy:
                resultats.append(self._rejouer_requete(requete, racine_url, contexte, base))

        # Aucune requête de la copie n'obtient de prédiction : le moteur joue
        # la sienne, authentifiée, construite depuis le schéma de l'API.
        if sondes_nginx.fichier_nginx_conf(self.eval_dir) and not any(
                s.get("declared") and "/predict" in str(s.get("url", ""))
                and isinstance(s.get("code"), int) and 200 <= s["code"] < 300 for s in resultats):
            for base in bases[:1]:
                resultats.extend(self._poster_predict_construit(base, contexte))
        return resultats

    def _poster_predict_construit(self, base: dict, contexte) -> list:
        """POST /predict construit, essayé avec chaque couple d'identifiants
        jusqu'au premier qui passe le 401 ; une sonde `construite`."""
        racine_url = base["url"].rstrip("/")
        requete = self._requete_construite(racine_url, contexte)
        if requete is None:
            self.record_step("Requête construite par le correcteur : POST /predict",
                             command=f"GET {racine_url}/openapi.json puis lecture de src/api/v1/main.py",
                             output="non construite : ni schéma OpenAPI joignable ni modèle Pydantic lisible",
                             exit_code=0)
            return []
        self._requete_construite_memo = requete
        sonde = {}
        for identifiants, _, _ in self._identifiants_candidats(requete):
            started = time.time()
            sonde = {"service": base["service"], "port": base["port"], "url": f"{racine_url}/predict",
                     "auth": True, "construite": True}
            sonde.update(self._poster_predict(f"{racine_url}/predict", requete, identifiants, contexte))
            sonde["duration_seconds"] = time.time() - started
            if sonde.get("code") != 401:
                break
        code = sonde.get("code")
        self.record_step(
            "Requête construite par le correcteur : POST /predict",
            command=f"POST {racine_url}/predict + corps JSON {requete['corps']} + Authorization Basic "
                    f"({requete['source']})",
            output=f"{resume(sonde)}\n{str(sonde.get('extrait', ''))[:400]}"[:500],
            exit_code=0 if isinstance(code, int) else 1,
            duration=sonde.get("duration_seconds", 0),
        )
        return [sonde]

    def _rejouer_requete(self, requete: dict, racine_url: str, contexte, base: dict,
                         detour: str | None = None) -> dict:
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
                    + (" + Authorization Basic" if requete["identifiants"] else "")
                    + (f" ({detour})" if detour else ""),
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
        négatifs : c'est lui qui rend 200. Sans requête déclarée, celle que
        le moteur construit depuis le schéma de l'API ; à défaut, un POST
        sans corps : le code reste lisible (422 n'est pas 401 ni 503)."""
        for requete in requetes_declarees(self.eval_dir):
            if (requete["methode"] == "POST" and requete["chemin"].rstrip("/").endswith("/predict")
                    and requete.get("port") in (None, 80, 443)
                    and not (requete["identifiants"] and code_attendu_negatif(requete["identifiants"]))):
                return requete
        construite = getattr(self, "_requete_construite_memo", None)
        if construite:
            return construite
        corps = sondes_nginx.corps_depuis_pydantic(self.eval_dir)
        if corps is not None:
            return self._requete_depuis_corps(corps, "modèle Pydantic de l'API")
        return {"methode": "POST", "chemin": "/predict", "corps": None, "entetes": {}, "identifiants": None}

    @staticmethod
    def _requete_depuis_corps(corps, origine: str) -> dict:
        import json
        return {"methode": "POST", "chemin": "/predict", "corps": json.dumps(corps, ensure_ascii=False),
                "entetes": {"Content-Type": "application/json"}, "identifiants": None, "port": None,
                "source": f"construite par le correcteur ({origine})"}

    def _requete_construite(self, racine_url: str, contexte) -> dict | None:
        """Un POST /predict que le moteur fabrique quand la copie n'en déclare
        aucun qui passe : l'énoncé exige une API qui prédit, pas la forme de
        la requête de test. Le schéma OpenAPI s'il traverse nginx (469145 ne
        route que /predict : 404), sinon le modèle Pydantic de src/api/v1."""
        import json
        try:
            sonde = sonder(f"{racine_url}/openapi.json", contexte=contexte, taille_extrait=TAILLE_CORPS_SONDE)
            if sonde.get("code") == 200:
                requete = sondes_prometheus.requete_depuis_openapi(json.loads(sonde.get("extrait", "")), "/predict")
                if requete and requete.get("methode") == "POST" and requete.get("corps") is not None:
                    return self._requete_depuis_corps(requete["corps"], "schéma OpenAPI servi par nginx")
        except Exception:
            pass
        corps = sondes_nginx.corps_depuis_pydantic(self.eval_dir)
        return self._requete_depuis_corps(corps, "modèle Pydantic de l'API") if corps is not None else None

    def _identifiants_candidats(self, requete: dict) -> list:
        """[(couple, origine, fichier)] dans l'ordre d'essai (politique du
        16/10 : on note ce qui a été livré) : la requête déclarée, ce que la
        copie écrit ailleurs (tests, README, Makefile), ceux de l'énoncé passés
        par env, et admin:admin du cours en dernier. Un couple écrit par la
        copie est « copie » même s'il vaut celui du cours (469217 : `-u
        admin:admin` dans son README) : l'ancien code l'étiquetait « du
        cours ». Le couple du cours que la copie cite autrement (« admin /
        mot de passe admin ») reste le sien."""
        candidats = []
        if requete.get("identifiants"):
            candidats.append((requete["identifiants"], "copie", requete.get("source")))
        candidats.extend((c, "copie", src) for c, src in identifiants_declares(self.eval_dir))
        if os.environ.get("EXAM_DOCKER_EVAL_BASIC_AUTH"):
            candidats.append((os.environ["EXAM_DOCKER_EVAL_BASIC_AUTH"], "énoncé", None))
        candidats.append((sondes_nginx.IDENTIFIANTS_DU_COURS, "cours", None))
        uniques, vus = [], set()
        for couple, origine, fichier in candidats:
            if couple in vus or code_attendu_negatif(couple):
                continue
            vus.add(couple)
            if origine != "copie":
                cite = identifiants_mentionnes(self.eval_dir, couple)
                if cite:
                    origine, fichier = "copie", cite
            uniques.append((couple, origine, fichier))
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
            # L'en-tête imposé par l'énoncé, toujours ; celui du map de la
            # copie, s'il diffère, en information. La requête de base ne porte
            # ni l'un ni l'autre (469145 déclare aussi sa variante debug).
            entete_impose, valeur_imposee = sondes_nginx.ENTETE_AB_IMPOSE
            copie_differe = bool(ab_declare) and (
                ab_declare["entete"].lower(), ab_declare["valeur"]) != (entete_impose.lower(), valeur_imposee)
            a_retirer = {entete_impose.lower()} | ({ab_declare["entete"].lower()} if ab_declare else set())
            requete = dict(requete, entetes={k: v for k, v in (requete.get("entetes") or {}).items()
                                             if k.lower() not in a_retirer})
            entete_ab = None
            # Les sondes déclarées viennent de passer sur le même seau.
            time.sleep(PAUSE_LIMITATION_S)
            identifiants = None
            defaut = None
            try:
                # Le POST nominal sert aussi à trouver les identifiants qui
                # passent : le premier couple qui ne rend pas 401.
                origine = None
                for couple, origine, _ in self._identifiants_candidats(requete) or [(None, None, None)]:
                    defaut = self._poster_predict(url, requete, couple, contexte)
                    identifiants = couple
                    if defaut.get("code") != 401:
                        break
                if isinstance(defaut.get("code"), int):
                    comportement["identifiants_acceptes"] = defaut.get("code") != 401
                    # D'où vient le couple accepté : le barème retire le critère
                    # « documenté » quand ce n'est pas la copie qui l'écrit.
                    if comportement["identifiants_acceptes"]:
                        comportement["identifiants_source"] = origine
            except Exception:
                defaut = None

            def mesurer_variante(entete, valeur):
                variante = self._poster_predict(url, requete, identifiants, contexte,
                                                entete_ab=entete, valeur_ab=valeur)
                deux_codes = isinstance(defaut.get("code"), int) and isinstance(variante.get("code"), int)
                return {"code_variante": variante.get("code"),
                        "corps_differents": (defaut.get("extrait") != variante.get("extrait")) if deux_codes else None}

            if defaut is not None:
                try:
                    comportement["ab"] = {"entete": entete_impose, "valeur": valeur_imposee,
                                          "code_defaut": defaut.get("code"),
                                          **mesurer_variante(entete_impose, valeur_imposee),
                                          "entete_copie": None}
                    if copie_differe:
                        comportement["ab"]["entete_copie"] = {
                            "entete": ab_declare["entete"], "valeur": ab_declare["valeur"],
                            **mesurer_variante(ab_declare["entete"], ab_declare["valeur"])}
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

        # La validation finale de l'énoncé, en dernier : le seau de limit_req
        # se remplit pendant la pause, et les logs des répliques sont déjà lus.
        try:
            time.sleep(PAUSE_LIMITATION_S)
            comportement["make_test"] = self._executer_make_test()
        except Exception as erreur:
            comportement["make_test"] = {"code": None, "sortie": f"erreur interne du harnais : {erreur}"}

        self.record_step(
            "Sondes de comportement nginx",
            command=(f"POST {base['url'].rstrip('/')}{chemin} (A/B par X-Experiment-Group: debug, mauvais "
                     f"identifiants, sans identifiants, rafale de {REQUETES_PAR_RAFALE}), logs des répliques v1, "
                     "exporter/Prometheus/Grafana, redirection du port 80, poignée de main TLS 1.1 et certificat, "
                     "make test dans un conteneur jetable")
                    if base is not None else
                    "pas de port TLS nginx joignable : sondes /predict et TLS non lancées ; make test tenté",
            output=sondes_nginx.resume_comportement(comportement),
            exit_code=0,
            duration=time.time() - debut,
        )
        return comportement

    def _image_make_test(self, client) -> str:
        """L'image outillée (bash, curl, make, socat) du conteneur jetable,
        construite au premier besoin puis réutilisée."""
        import io
        try:
            client.images.get(IMAGE_MAKE_TEST)
        except Exception:
            client.images.build(fileobj=io.BytesIO(DOCKERFILE_MAKE_TEST.encode()), tag=IMAGE_MAKE_TEST, rm=True)
        return IMAGE_MAKE_TEST

    def _archive_de_la_copie(self) -> bytes:
        """La copie de travail en tar, sous travail/ : elle entre dans le
        conteneur par put_archive, sans montage de l'hôte."""
        import io
        import tarfile
        tampon = io.BytesIO()
        with tarfile.open(fileobj=tampon, mode="w") as archive:
            for racine, dossiers, fichiers in os.walk(self.eval_dir):
                dossiers[:] = [d for d in dossiers if d not in (".git", ".venv", "__pycache__", "node_modules")]
                for nom in fichiers:
                    chemin = os.path.join(racine, nom)
                    if os.path.islink(chemin):
                        continue  # un lien de la copie pourrait viser l'hôte
                    archive.add(chemin, arcname=os.path.join("travail", os.path.relpath(chemin, self.eval_dir)),
                                recursive=False)
        return tampon.getvalue()

    def _executer_make_test(self) -> dict:
        """`make test` de la copie, joué comme sur la machine de l'apprenant
        mais dans un conteneur jetable : {"code": int|None, "sortie": str}.

        Isolation : image alpine outillée, pas de socket Docker, pas de
        montage de l'hôte (la copie entre par archive), réseaux du projet
        seulement, `timeout 120`, conteneur retiré dans tous les cas. Le
        script vise `https://localhost` (le 443 que la copie publie) alors
        que le moteur a relâché les ports : des relais socat rendent
        localhost:<port publié> → <conteneur>:<port>, et la redirection
        `https://$host/…` de nginx retombe sur le même relais."""
        import secrets
        debut = time.time()

        def fin(code, sortie, commande="make test (conteneur jetable)"):
            resultat = {"code": code, "sortie": sortie}
            self.record_step(
                "make test de la copie (conteneur jetable)",
                command=commande,
                output=(f"code {code}" if code is not None else "non mesuré") + f"\n{sortie}"[:1500],
                # L'échec de make test est l'affaire du barème, pas une panne du harnais.
                exit_code=0,
                duration=time.time() - debut,
            )
            return resultat

        commande, note = sondes_nginx.commande_make_test(self.eval_dir)
        if commande is None:
            return fin(None, note)
        conteneurs = [c for c in self._conteneurs_du_projet() if getattr(c, "status", "running") == "running"]

        def service(c):
            return (getattr(c, "labels", None) or {}).get("com.docker.compose.service", c.name)

        def reseaux(c):
            return {nom: (info or {}).get("IPAddress")
                    for nom, info in ((c.attrs.get("NetworkSettings") or {}).get("Networks") or {}).items()}

        nginx = next((c for c in conteneurs if "nginx" in service(c).lower()
                      and "exporter" not in service(c).lower()), None)
        if nginx is None or not reseaux(nginx):
            return fin(None, "pas de conteneur nginx debout : make test non lancé")
        reseaux_utiles = list(reseaux(nginx))
        relais = []
        try:
            with open(fichier_compose(self.eval_dir), encoding="utf-8", errors="replace") as lecteur:
                publies = sondes_nginx.redirections_make_test(
                    lecteur.read(), sondes_prometheus.lire_env(os.path.join(self.eval_dir, ".env")))
        except Exception:
            publies = []
        for port_hote, nom_service, port_conteneur in publies:
            cible = next((c for c in conteneurs if service(c) == nom_service), None)
            if cible is None:
                continue
            ips = reseaux(cible)
            reseau = next((r for r in reseaux_utiles if ips.get(r)), None) or next((r for r in ips if ips[r]), None)
            if reseau is None:
                continue
            if reseau not in reseaux_utiles:
                reseaux_utiles.append(reseau)
            relais.append((port_hote, ips[reseau], port_conteneur, nom_service))
        lignes = ["set -u"]
        lignes += [f"socat TCP-LISTEN:{h},fork,reuseaddr,bind=127.0.0.1 TCP:{ip}:{c} &" for h, ip, c, _ in relais]
        lignes += ["sleep 1", "cd /travail",
                   f"timeout {DELAI_MAKE_TEST_S} {commande} 2>&1", "exit $?"]
        script = "\n".join(lignes)
        description = (f"{commande} dans {IMAGE_MAKE_TEST} (sans socket Docker, copie par archive, "
                       f"timeout {DELAI_MAKE_TEST_S} s) ; relais localhost : "
                       + (", ".join(f"{h} → {s}:{c}" for h, _, c, s in relais) or "aucun port publié"))
        client = client_docker()
        try:
            image = self._image_make_test(client)
        except Exception as erreur:
            return fin(None, f"image du conteneur jetable indisponible ({erreur})", description)
        conteneur = None
        try:
            conteneur = client.containers.create(
                image, command=["sh", "-c", script],
                name=f"{self.project_name}-make-test-{secrets.token_hex(3)}",
                network=reseaux_utiles[0], labels={"scriptorium.make-test": self.project_name},
                mem_limit="512m", pids_limit=256, security_opt=["no-new-privileges"],
                cap_drop=["NET_RAW", "MKNOD", "SYS_CHROOT", "SETFCAP", "AUDIT_WRITE"])
            for reseau in reseaux_utiles[1:]:
                client.networks.get(reseau).connect(conteneur)
            conteneur.put_archive("/", self._archive_de_la_copie())
            conteneur.start()
            try:
                etat = conteneur.wait(timeout=DELAI_MAKE_TEST_S + MARGE_MAKE_TEST_S)
            except Exception as erreur:
                return fin(None, f"délai dépassé ({DELAI_MAKE_TEST_S + MARGE_MAKE_TEST_S} s) : {erreur}", description)
            code = etat.get("StatusCode") if isinstance(etat, dict) else None
            sortie = sondes_nginx.nettoyer_sortie(conteneur.logs())
            if code == 124:
                return fin(None, f"délai de {DELAI_MAKE_TEST_S} s dépassé\n{sortie}", description)
            prefixe = f"{note}\n" if note else ""
            return fin(code if isinstance(code, int) else None, prefixe + sortie, description)
        except Exception as erreur:
            return fin(None, f"conteneur jetable en échec ({erreur})", description)
        finally:
            if conteneur is not None:
                try:
                    conteneur.remove(force=True)
                except Exception:
                    pass

    def _variables_env_manquantes(self, compose_file: str, fichiers: list, env_files: list) -> list:
        """Les variables requises (`:?`) que ni le .env du projet ni les
        --env-file de la recette ne définissent. Un .env livré par la copie
        n'est jamais complété (règle de `_preparer_env`) : rien ne manque."""
        cible = os.path.join(self.eval_dir, ".env")
        if os.path.lexists(cible) and cible not in self._fichiers_rapatries:
            return []
        requises = []
        for fichier in fichiers or [compose_file]:
            try:
                with open(fichier, encoding="utf-8", errors="replace") as lecteur:
                    requises += [n for n in variables_requises(lecteur.read()) if n not in requises]
            except OSError:
                continue
        definies = set()
        for env in [cible, *env_files]:
            definies |= lancement.variables_definies(env)
        return [n for n in requises if n not in definies]

    def _jouer_lancement(self) -> dict | None:
        """La commande de lancement de l'énoncé, jouée telle que livrée dans le
        conteneur jetable de `make test` (voir lancement.py) :
        {"tel_que_livre": {commande, code, cause, motif, extrait, up}, "up":
        arguments du moteur ou None}, ou None hors nginx/prometheus-grafana.

        Avant `_preparer_env` et `_relax_host_ports` : l'archive ne doit
        porter ni notre .env (la cible `.env:` de 469217 ne tournerait pas) ni
        notre compose relâché. Le conteneur n'a ni socket Docker ni montage de
        l'hôte ; seul un `docker` factice répond. Ce que la recette produit
        (.env, certificats) revient dans le projet : fichiers réguliers,
        bornés, retirés au nettoyage."""
        import secrets
        commande = lancement.commande_de_lancement(self.eval_dir)
        if commande is None:
            return None
        debut = time.time()
        description = (f"{commande} dans {IMAGE_MAKE_TEST} (docker factice, sans socket Docker, copie par archive, "
                       f"timeout {lancement.DELAI_LANCEMENT_S} s)")

        def fin(tel, rapatries=()):
            up = None
            if tel.get("up"):
                up = lancement.traduire_up(tel["up"])
                if up is None:
                    tel = lancement.non_mesure(commande, "up_non_reproductible",
                                               f"`{' '.join(tel['up'])}` : options que le moteur ne sait pas rejouer "
                                               "fidèlement\n" + tel["extrait"])
            self.record_step(
                "Lancement tel que livré (conteneur jetable)",
                command=description,
                output=(f"code {tel['code']}" if tel["code"] is not None else "non mesuré")
                       + (f" — {tel['cause']} ({tel['motif']})" if tel["cause"] else "")
                       + (f"\nfichiers produits par la recette, rapatriés : {', '.join(rapatries)}" if rapatries else "")
                       + f"\n{tel['extrait']}"[:1500],
                # L'échec de la commande est l'affaire du barème, pas une panne.
                exit_code=0,
                duration=time.time() - debut,
            )
            return {"tel_que_livre": tel, "up": up}

        if lancement.makefile_de(self.eval_dir) is None:
            return fin(lancement.sans_makefile(commande))
        try:
            client = client_docker()
            image = self._image_make_test(client)
        except Exception as erreur:
            return fin(lancement.non_mesure(commande, "conteneur_jetable",
                                            f"image du conteneur jetable indisponible ({erreur})"))
        script = "\n".join(["set -u", "cd /travail",
                            f"timeout {lancement.DELAI_LANCEMENT_S} {commande} 2>&1", "exit $?"])
        conteneur = None
        try:
            conteneur = client.containers.create(
                image, command=["sh", "-c", script],
                name=f"{self.project_name}-lancement-{secrets.token_hex(3)}",
                labels={"scriptorium.lancement": self.project_name},
                mem_limit="512m", pids_limit=256, security_opt=["no-new-privileges"],
                cap_drop=["NET_RAW", "MKNOD", "SYS_CHROOT", "SETFCAP", "AUDIT_WRITE"])
            conteneur.put_archive("/", self._archive_de_la_copie())
            conteneur.put_archive("/", lancement.archive_du_factice())
            conteneur.start()
            try:
                etat = conteneur.wait(timeout=lancement.DELAI_LANCEMENT_S + lancement.MARGE_LANCEMENT_S)
            except Exception as erreur:
                return fin(lancement.non_mesure(commande, "delai", f"délai dépassé : {erreur}"))
            code = etat.get("StatusCode") if isinstance(etat, dict) else None
            sortie = conteneur.logs()
            sortie = sortie.decode("utf-8", "replace") if isinstance(sortie, bytes) else str(sortie)
            flux, _ = conteneur.get_archive(lancement.FICHIER_APPELS)
            appels = lancement.lire_appels(lancement.contenu_tar(b"".join(flux), "appels"))
            tel = lancement.classer(commande, code, sortie, appels)
            flux, _ = conteneur.get_archive("/travail")
            nouveaux, originaux = lancement.rapatrier(b"".join(flux), self.eval_dir)
            self._fichiers_rapatries.extend(nouveaux)
            for chemin, contenu in originaux.items():
                self._originaux.setdefault(chemin, contenu)
            rapatries = sorted(os.path.relpath(c, self.eval_dir) for c in [*nouveaux, *originaux])
            return fin(tel, rapatries)
        except Exception as erreur:
            return fin(lancement.non_mesure(commande, "conteneur_jetable", f"conteneur jetable en échec ({erreur})"))
        finally:
            if conteneur is not None:
                try:
                    conteneur.remove(force=True)
                except Exception:
                    pass

    def _ecarter_appledouble(self) -> None:
        """Écarter les AppleDouble `._X` de la copie de travail avant le
        lancement : métadonnées d'archivage macOS (famille de `__MACOSX`), pas
        des fichiers de l'apprenant. 460188 : `rules/._alert_rules.yml` et
        `provisioning/dashboards/._dashboards.yaml`, lus par Prometheus et
        Grafana, les faisaient planter (« yaml: control characters are not
        allowed »). Ni réparation ni point : une étape lisible, et les
        fichiers remis au nettoyage. Seuls les vrais AppleDouble partent :
        nom `._X`, fichier régulier (pas de lien), en-tête 0x00051607."""
        debut = time.time()
        for racine, dossiers, fichiers in os.walk(self.eval_dir):
            dossiers[:] = [d for d in dossiers if d not in _DOSSIERS_IGNORES]
            for nom in fichiers:
                chemin = os.path.join(racine, nom)
                if not nom.startswith("._") or os.path.islink(chemin) or not os.path.isfile(chemin):
                    continue
                try:
                    with open(chemin, "rb") as lecteur:
                        if lecteur.read(4) != _ENTETE_APPLEDOUBLE:
                            continue
                        lecteur.seek(0)
                        contenu = lecteur.read()
                    os.remove(chemin)
                except OSError as erreur:
                    self.logger.warning(f"AppleDouble {chemin} non écarté : {erreur}")
                    continue
                self._appledouble[chemin] = contenu
        if self._appledouble:
            self.record_step(
                "Fichiers AppleDouble écartés",
                command="retrait des `._*` à en-tête AppleDouble (0x00051607) de la copie de travail",
                output="\n".join(sorted(os.path.relpath(c, self.eval_dir) for c in self._appledouble)),
                exit_code=0,
                duration=time.time() - debut,
                note=("Métadonnées macOS ajoutées par l'archivage, pas des fichiers de l'apprenant : "
                      "écartées sans pénalité avant le lancement, remises au nettoyage."),
            )

    def _remettre_appledouble(self) -> list:
        erreurs = []
        for chemin, contenu in self._appledouble.items():
            try:
                with open(chemin, "xb") as sortie:
                    sortie.write(contenu)
            except FileExistsError:
                pass
            except OSError as e:
                erreurs.append(f"{os.path.relpath(chemin, self.eval_dir)} non remis : {e}")
        self._appledouble = {}
        return erreurs

    def _retirer_rapatries(self) -> list:
        """Remettre le projet dans l'état livré : fichiers produits par la
        recette retirés, fichiers modifiés rendus. Rend les erreurs."""
        erreurs = []
        for chemin in reversed(self._fichiers_rapatries):
            try:
                os.remove(chemin)
            except FileNotFoundError:
                pass
            except OSError as e:
                erreurs.append(f"{os.path.relpath(chemin, self.eval_dir)} non retiré : {e}")
                continue
            dossier = os.path.dirname(chemin)
            while os.path.realpath(dossier).startswith(os.path.realpath(self.eval_dir) + os.sep):
                try:
                    os.rmdir(dossier)  # vide seulement
                except OSError:
                    break
                dossier = os.path.dirname(dossier)
        for chemin, contenu in self._originaux.items():
            try:
                with open(chemin, "wb") as f:
                    f.write(contenu)
            except OSError as e:
                erreurs.append(f"{os.path.relpath(chemin, self.eval_dir)} non restauré : {e}")
        self._fichiers_rapatries, self._originaux = [], {}
        return erreurs

    def _config_tel_que_livre(self, fichiers: list, env_files: list) -> tuple:
        """(code, extrait) de `docker compose config -q` sur la copie telle
        que livrée, avant que le moteur ne fabrique le .env. `config`
        interpole sans rien construire ni lancer ; l'extrait est masqué."""
        import subprocess
        argv = ["docker", "compose", "-p", self.project_name]
        for fichier in fichiers:
            argv += ["-f", fichier]
        for env in env_files:
            argv += ["--env-file", env]
        try:
            fini = subprocess.run(argv + ["config", "-q"], cwd=self.eval_dir, capture_output=True, text=True,
                                  timeout=30)
        except Exception as erreur:
            return None, f"docker compose config non joué ({erreur})"
        return fini.returncode, lancement.masquer_secrets((fini.stderr or fini.stdout or "").strip())[-800:]

    def _noter_reparations_de_lancement(self, compose_name: str) -> None:
        """Le compose du moteur à la place de la commande de l'énoncé : une
        réparation, consignée pour le barème (critère services/stack)."""
        tel = self._lancement
        if not tel or tel["code"] == 0 and tel["cause"] is None:
            return
        if tel.get("motif") == "variables_requises" or any(
                r["id"] == "compose_au_lieu_de_make" for r in self._reparations):
            return
        self._reparations.insert(0, {
            "id": "compose_au_lieu_de_make",
            "critere_touche": "services_debout",
            "tel_que_livre": {"commande": tel["commande"], "code": tel["code"], "extrait": tel["extrait"]},
            "applique": f"docker compose -f {compose_name} up --build lancé par le moteur à la place de "
                        f"`{tel['commande']}`",
        })

    def _avec_lancement(self, result: dict) -> dict:
        if isinstance(result, dict) and self._lancement is not None:
            result["lancement_tel_que_livre"] = {k: v for k, v in self._lancement.items() if k != "up"}
        if isinstance(result, dict):
            result["reparations"] = list(self._reparations)
            if self._date_reference is not None:
                result["date_reference"] = dict(self._date_reference)
            if self._reconstruction is not None:
                result["reconstruction_datee"] = dict(self._reconstruction)
        return result

    # --- Reconstruction datée (467094) -------------------------------------

    def _relever_date_reference(self) -> None:
        """La date de référence du rendu, relevée avant que le harnais n'écrive
        dans la copie de travail (.env, compose relâché, fichiers rapatriés) ;
        consignée après la résolution du fichier Compose."""
        debut = time.time()
        try:
            self._date_reference = reconstruction_datee.date_reference(self.eval_dir)
        except Exception as erreur:
            self._date_reference = {"date_reference": None, "fichier": None, "ignores": 0,
                                    "source": f"date de référence illisible : {erreur}"}
        self._duree_date_reference = time.time() - debut

    def _consigner_date_reference(self) -> None:
        ref = self._date_reference
        self.record_step(
            "Date de référence du rendu",
            command="mtime le plus récent des fichiers extraits (lstat, sans __MACOSX ni ._*)",
            output=f"{ref['date_reference'] or 'aucune'} — {ref['source']}",
            exit_code=0,
            duration=getattr(self, "_duree_date_reference", 0),
            note=("Borne inférieure de la date de rendu : un paquet publié avant existait au rendu. "
                  "Sert à la reconstruction datée si la stack tombe sur un conflit de versions."),
        )

    def _images_des_services_construits(self) -> dict:
        """{service: ID d'image} des services construits pour cette copie."""
        images = {}
        prefixe = self.project_name.lower()
        for conteneur in self._conteneurs_du_projet():
            service = conteneur.labels.get("com.docker.compose.service", conteneur.name)
            try:
                image = conteneur.image
                tags = image.tags or []
            except Exception:
                continue
            if any(t.lower().startswith(prefixe) for t in tags):
                images[service] = image.id
        return images

    def _freeze_image(self, image: str) -> str:
        """`pip freeze` dans l'image construite : conteneur jetable, sans réseau."""
        import docker
        client = docker.from_env()
        conteneur = client.containers.create(
            image, entrypoint=["sh", "-c", "python -m pip freeze 2>/dev/null || python3 -m pip freeze"],
            network_disabled=True, labels={"scriptorium.role": "freeze-reconstruction-datee"})
        try:
            conteneur.start()
            conteneur.wait(timeout=120)
            return conteneur.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        finally:
            try:
                conteneur.remove(force=True)
            except Exception:
                pass

    def _pypi_json(self, nom: str):
        import requests
        try:
            reponse = requests.get(f"https://pypi.org/pypi/{nom}/json", timeout=30)
            return reponse.json() if reponse.status_code == 200 else None
        except Exception:
            return None

    def _tenter_reconstruction_datee(self, compose_name: str, exc: Exception, verifier=None) -> str | None:
        """Rebâtir une fois la stack aux versions de la date du rendu.

        Seulement sur un conflit de versions entre dépendances (467094). Rend
        le nom du compose dérivé si la stack devient saine, l'évaluation
        continue alors dessus ; sinon None, et le classement initial reste.

        `verifier()` : après un `up` qui passe, les services encore en échec
        (liste vide si la stack est saine). Sans healthcheck bloquant, un
        `up` réussi ne prouve rien (461338 : bike-api en boucle).
        """
        cause = self._cause_demarrage
        if self._reconstruction is not None or not reconstruction_datee.doit_reconstruire(cause):
            return None
        debut = time.time()
        initiale = dict(cause)
        service_cause = initiale.get("service")
        preuve = ((initiale.get("services") or {}).get(service_cause) or {}).get("extrait") \
            or initiale.get("preuve", "")
        ref = self._date_reference or {}
        date_iso = ref.get("date_reference")
        self._reconstruction = {
            "date_reference": date_iso, "source": ref.get("source"),
            "methode": ("Dockerfile dérivé (dockerfile_inline d'un compose dérivé dans la copie de travail) : "
                        "ARG PIP_CONSTRAINT vers des contraintes `paquet!=version` (versions publiées après "
                        "la date, d'après l'API JSON de PyPI, pour les paquets installés non épinglés), "
                        "ARG PIP_UPLOADED_PRIOR_TO (pip ≥ 26) et ARG UV_EXCLUDE_NEWER (uv)"),
            "paquets_contraints": [], "resultat": "echec", "extrait": "", "preuve": preuve,
        }

        def terminer(extrait: str, code: int) -> None:
            self._reconstruction["extrait"] = lancement.masquer_secrets(extrait)[-1500:]
            self.record_step(
                "Reconstruction datée",
                command=f"docker compose -f {reconstruction_datee.COMPOSE_DATE} up --build "
                        f"(paquets contraints au {date_iso or '?'})",
                output=f"résultat : {self._reconstruction['resultat']}\n{self._reconstruction['extrait']}",
                exit_code=code,
                duration=time.time() - debut,
                note=("Une copie qui marchait à la date du rendu est notée sur une reconstruction aux "
                      "versions de l'époque ; ses dépendances non figées coûtent au plus un point."),
            )

        if not date_iso:
            terminer(f"non tentée : pas de date de référence ({ref.get('source')})", 2)
            return None
        restant = self.timeout - (time.time() - self._debut_evaluation)
        if restant < 180:
            terminer(f"non tentée : {restant:.0f}s de budget restant, trop peu pour un build", 2)
            return None
        try:
            import yaml
            images = self._images_des_services_construits()
            with open(os.path.join(self.eval_dir, compose_name), encoding="utf-8") as lecteur:
                doc = yaml.safe_load(lecteur) or {}
            if not images:
                terminer("non tentée : aucun service construit par la copie, rien à reconstruire", 2)
                return None
            date = reconstruction_datee.lire_iso(date_iso)
            cache = {}

            def pypi(nom):
                if nom not in cache:
                    cache[nom] = self._pypi_json(nom)
                return cache[nom]

            par_service, notes = {}, []
            services = (doc.get("services") or {})
            for service, image in sorted(images.items()):
                build = reconstruction_datee._build(services.get(service) or {}) or {}
                noms = reconstruction_datee.epingles(
                    reconstruction_datee.requirements_du_contexte(build, self.eval_dir))
                try:
                    freeze = self._freeze_image(image)
                except Exception as erreur:
                    notes.append(f"{service} : pip freeze impossible ({erreur})")
                    continue
                lignes, contraints, remarques = reconstruction_datee.contraintes(freeze, noms, pypi, date)
                par_service[service] = lignes
                notes += [f"{service} : {r}" for r in remarques]
                for paquet in contraints:
                    self._reconstruction["paquets_contraints"].append({"service": service, **paquet})
            derive = reconstruction_datee.compose_date(doc, self.eval_dir, date_iso, par_service)
            chemin = os.path.join(self.eval_dir, reconstruction_datee.COMPOSE_DATE)
            with open(chemin, "w", encoding="utf-8") as sortie:
                yaml.safe_dump(derive, sortie, sort_keys=False, allow_unicode=True)
            self._fichiers_harnais.append(chemin)
            # Même tag, nouvel ID après le rebuild : l'ancienne image deviendrait
            # orpheline et échapperait au ménage par nom.
            for image in images.values():
                self.record_exam_image(image)
            try:
                if self.compose is not None:
                    self.compose.stop()
            except Exception as erreur:
                notes.append(f"arrêt de la stack d'origine : {erreur}")
            self._cause_demarrage = None
            self.compose = _avec_echelles(
                DockerCompose(context=self.eval_dir, compose_file_name=reconstruction_datee.COMPOSE_DATE,
                              pull=False, build=True, env_file=None, profiles=self._profils_up or None),
                self._echelles_up)
            contraints = ", ".join(f"{p['service']} : {p['paquet']} {p['installe']} (exclues : {', '.join(p['exclues'])})"
                                   for p in self._reconstruction["paquets_contraints"]) or "aucun"
            resume_contraintes = f"paquets contraints : {contraints}" + (
                "\n" + "\n".join(notes) if notes else "")
            try:
                _DemarrageSansDemontage(self.compose, self._diagnostiquer_up_rate).__enter__()
                defaillants = verifier() if verifier is not None else []
                if defaillants:
                    self._cause_demarrage = initiale
                    terminer(f"{resume_contraintes}\nla stack reconstruite démarre mais reste en échec : "
                             f"{', '.join(defaillants)}", 1)
                    return None
            except Exception as second:
                nouvelle = self._cause_demarrage
                self._cause_demarrage = initiale
                terminer(f"{resume_contraintes}\nla stack reconstruite ne démarre pas non plus "
                         f"(cause : {(nouvelle or {}).get('cause', 'non classée')}) : {second}", 1)
                return None
        except Exception as erreur:
            self._cause_demarrage = initiale
            terminer(f"reconstruction interrompue : {erreur}", 2)
            return None
        self._retenir_images_de_la_copie()
        self._reconstruction["resultat"] = "saine"
        reessai = (self._cause_demarrage or {}).get("reessai")
        self._cause_demarrage = {
            "imputation": "copie_datee", "cause": "derive_dependance", "service": service_cause,
            "preuve": (f"la stack ne démarre plus avec les versions publiées après le rendu ({preuve}) ; "
                       f"saine reconstruite aux versions du {reconstruction_datee.date_lisible(date_iso)}"),
            "services": initiale.get("services"), "reessai": reessai, "cause_initiale": initiale,
        }
        etape = getattr(self, "_etape_diagnostic", None)
        if etape is not None:
            etape["exit_code"] = 0
            etape["output"] += ("\nreconstruction datée : la stack démarre aux versions du "
                                f"{reconstruction_datee.date_lisible(date_iso)} → imputation copie_datee.")
        self._reparations.append({
            "id": "reconstruction_datee",
            "critere_touche": "reproductibilite",
            "tel_que_livre": {"commande": f"docker compose -f {compose_name} up --build", "code": 1,
                              "extrait": lancement.masquer_secrets(f"{service_cause} : {preuve}")[-600:]},
            "applique": (f"stack reconstruite avec les paquets publiés au plus tard le {date_iso} "
                         f"({contraints})"),
        })
        terminer(resume_contraintes, 0)
        return reconstruction_datee.COMPOSE_DATE

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
        (le défaut de l'image). Jamais consignés : l'appelant ne les met pas
        dans une étape."""
        utilisateur, mot_de_passe = "admin", "admin"
        try:
            import yaml
            with open(fichier_compose(self.eval_dir), encoding="utf-8") as lecteur:
                services = (yaml.safe_load(lecteur) or {}).get("services") or {}
            # `${GRAFANA_ADMIN_PASSWORD}` se résout contre le .env du projet :
            # celui de la copie, ou celui que le harnais a écrit (469217),
            # encore présent tant que la stack tourne.
            env_fichier = sondes_prometheus.lire_env(os.path.join(self.eval_dir, ".env"))
            for nom, service in services.items():
                if "grafana" not in f"{nom} {service.get('image', '')}".lower():
                    continue
                env = service.get("environment") or {}
                if isinstance(env, list):
                    env = dict(e.split("=", 1) for e in env if isinstance(e, str) and "=" in e)
                u = sondes_prometheus.resoudre_variable(str(env.get("GF_SECURITY_ADMIN_USER") or ""), env_fichier)
                p = sondes_prometheus.resoudre_variable(str(env.get("GF_SECURITY_ADMIN_PASSWORD") or ""),
                                                        env_fichier)
                # Une variable sans valeur : Grafana démarre avec le défaut.
                if u:
                    utilisateur = u
                if p:
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

    @staticmethod
    def _candidats_api(sondes_racine: list) -> list:
        """Les URL hôte qui peuvent être l'API, quel que soit le code de leur
        sonde racine : l'API de 467439/466056 entraîne au démarrage et sa
        racine peut encore se taire. Le nom (api, bike, app) puis les ports
        habituels (8000, 5000, 8080) passent devant ; la stack de monitoring
        est écartée."""
        candidats = []
        for sonde in sondes_racine:
            url = str(sonde.get("url", ""))
            port = str(sonde.get("port", "")).split("/")[0]
            nom = str(sonde.get("service", "")).lower()
            if not url.startswith("http:") or port in PORTS_MONITORING or any(m in nom for m in MOTS_MONITORING):
                continue
            racine = url.rstrip("/")
            if racine in [c for _, c in candidats]:
                continue
            rang = 0 if any(m in nom for m in ("api", "bike", "app")) else 1 if port in ("8000", "5000", "8080") else 2
            candidats.append((rang, racine))
        return [racine for _, racine in sorted(candidats, key=lambda c: c[0])]

    def _trouver_api(self, sondes_racine: list):
        """(racine, sonde /metrics) de l'API : le service dont /metrics expose
        des métriques applicatives. À défaut, le premier candidat qui répond
        (son code dit l'absence de /metrics). (None, None) sinon."""
        candidats = self._candidats_api(sondes_racine)
        if not candidats:
            return None, None
        limite = time.time() + ATTENTE_API_S
        while True:
            repondu, en_attente = None, False
            for racine in candidats:
                sonde = sonder(f"{racine}/metrics", taille_extrait=TAILLE_CORPS_SONDE)
                code = sonde.get("code")
                if code == 200:
                    noms, _ = sondes_prometheus.familles_metriques(sonde.get("extrait", ""))
                    if any(sondes_prometheus.est_metrique_applicative(n) for n in noms):
                        return racine, sonde
                if not isinstance(code, int) or code >= 500:
                    en_attente = True
                elif repondu is None:
                    repondu = (racine, sonde)
            if not en_attente or time.time() >= limite:
                return repondu or (None, None)
            time.sleep(PAS_ATTENTE_S)

    def _lire_json(self, url: str, entetes: dict | None = None):
        """Le JSON d'une réponse 200 ; None sinon. Un corps tronqué ou
        illisible n'est jamais lu comme une liste vide."""
        import json
        sonde = sonder(url, entetes=entetes, taille_extrait=TAILLE_CORPS_SONDE)
        if sonde.get("code") != 200:
            return None
        return json.loads(sonde.get("extrait", ""))

    def _sonder_comportement_prometheus(self, sondes_racine: list) -> dict:
        """Les exigences prometheus-grafana de la grille, prouvées en marche
        (467439, 466056, grille du 15/10).

        Chaque champ est au mieux : celui qui ne peut pas se mesurer vaut
        None, jamais une exception. L'ordre compte : l'API d'abord (les
        cibles ne sont up qu'une fois l'API scrapée), la dérive en dernier
        (elle modifie les métriques que les autres sondes relèvent)."""
        import base64

        debut = time.time()
        c = dict.fromkeys(sondes_prometheus.CHAMPS)
        notes = []

        try:
            api, sonde_metrics = self._trouver_api(sondes_racine)
        except Exception:
            api, sonde_metrics = None, None
        if sonde_metrics is not None:
            noms, types = sondes_prometheus.familles_metriques(sonde_metrics.get("extrait", ""))
            c["metriques_api"] = {"code": sonde_metrics.get("code"), "noms": noms, "types": types}

        prometheus = self._racine_monitoring(sondes_racine, ("prometheus",), 9090, exclure="exporter")
        if prometheus:
            try:
                code = sonder(f"{prometheus}/-/ready").get("code")
                c["prometheus_pret"] = (code == 200) if isinstance(code, int) else None
            except Exception:
                c["prometheus_pret"] = None
            try:
                limite = time.time() + ATTENTE_CIBLES_S
                donnees = self._lire_json(f"{prometheus}/api/v1/targets")
                # Une cible pas encore scrapée, ou l'API montée pendant notre
                # attente : on laisse quelques intervalles de scrape.
                while donnees is not None and time.time() < limite \
                        and sondes_prometheus.resumer_cibles(donnees)["down"]:
                    time.sleep(PAS_ATTENTE_S)
                    donnees = self._lire_json(f"{prometheus}/api/v1/targets")
                if donnees is not None:
                    c["cibles"] = sondes_prometheus.resumer_cibles(donnees)
                    inconnues = sondes_prometheus.cibles_inconnues(donnees)
                    if inconnues:
                        notes.append(f"cibles jamais scrapées (unknown), comptées pas up : {', '.join(inconnues)}")
            except Exception:
                c["cibles"] = None
            try:
                donnees = self._lire_json(f"{prometheus}/api/v1/rules")
                c["regles"] = sondes_prometheus.resumer_regles(donnees) if donnees is not None else None
            except Exception:
                c["regles"] = None

        grafana = self._racine_monitoring(sondes_racine, ("grafana",), 3000)
        if grafana:
            try:
                code = sonder(f"{grafana}/api/health").get("code")
                c["grafana_sante"] = (code == 200) if isinstance(code, int) else None
            except Exception:
                c["grafana_sante"] = None
            try:
                jeton = base64.b64encode(self._identifiants_grafana().encode()).decode()
                auth = {"Authorization": f"Basic {jeton}"}
                sources = self._lire_json(f"{grafana}/api/datasources", auth)
                tableaux = self._lire_json(f"{grafana}/api/search?type=dash-db", auth)
                if isinstance(sources, list) and isinstance(tableaux, list):
                    requetes = []
                    for tableau in tableaux[:MAXI_TABLEAUX]:
                        uid = tableau.get("uid") if isinstance(tableau, dict) else None
                        if not uid or len(requetes) >= 100:
                            continue
                        try:
                            detail = self._lire_json(f"{grafana}/api/dashboards/uid/{uid}", auth)
                        except Exception:
                            detail = None
                        if isinstance(detail, dict):
                            for expr in sondes_prometheus.requetes_des_panels(detail, maxi=100):
                                if expr not in requetes and len(requetes) < 100:
                                    requetes.append(expr)
                    c["grafana"] = {
                        "datasources": len(sources),
                        "datasource_prometheus": any(isinstance(s, dict) and s.get("type") == "prometheus"
                                                     for s in sources),
                        "dashboards": len(tableaux),
                        "requetes_panels": requetes,
                        "alertes": None,
                    }
                    # Les alertes gérées par Grafana (provisioning/alerting) :
                    # 467439 y met son alerte RMSE, invisible de
                    # /api/v1/rules de Prometheus.
                    try:
                        regles = self._lire_json(f"{grafana}/api/v1/provisioning/alert-rules", auth)
                        if isinstance(regles, list):
                            c["grafana"]["alertes"] = sondes_prometheus.alertes_grafana(regles)
                    except Exception:
                        c["grafana"]["alertes"] = None
            except Exception:
                c["grafana"] = None

        try:
            declare = sondes_prometheus.endpoint_drift_declare(self.eval_dir)
            if api and declare:
                avant = sondes_prometheus.series_drift((sonde_metrics or {}).get("extrait", ""))
                reponse = self._appeler_derive(api, declare, notes)
                code = reponse.get("code")
                modifiee = None
                if isinstance(code, int) and 200 <= code < 300:
                    relu = sonder(f"{api}/metrics", taille_extrait=TAILLE_CORPS_SONDE)
                    if relu.get("code") == 200:
                        apres = sondes_prometheus.series_drift(relu.get("extrait", ""))
                        changees = sondes_prometheus.series_changees(avant, apres)
                        modifiee = bool(changees)
                        if changees:
                            notes.append("jauge(s) de dérive modifiée(s) : " + ", ".join(
                                f"{s} {avant.get(s, 'absente')} → {apres[s]}" for s in changees[:5]))
                # Sans réponse HTTP (délai dépassé : Evidently sur 2 vCPU),
                # rien n'est mesuré : le champ entier vaut null, le code du
                # contrat reste toujours un entier.
                if isinstance(code, int):
                    c["endpoint_drift"] = {"chemin": declare["chemin"], "code": code, "metrique_modifiee": modifiee}
                else:
                    notes.append(f"endpoint de dérive {declare['chemin']} sans réponse : "
                                 f"{reponse.get('erreur') or code}")
        except Exception:
            c["endpoint_drift"] = None

        self.record_step(
            "Sondes de comportement Prometheus",
            command=(f"GET <api>/metrics ({api or 'API introuvable'}), "
                     f"Prometheus {prometheus or 'non publié'} : /-/ready, /api/v1/targets, /api/v1/rules ; "
                     f"Grafana {grafana or 'non publié'} : /api/health, /api/datasources, /api/search, "
                     "/api/dashboards/uid, /api/v1/provisioning/alert-rules (identifiants du compose, non consignés) ; GET <api>/openapi.json (schéma du corps), "
                     "endpoint de dérive puis /metrics relu"),
            output=sondes_prometheus.resume_comportement(c) + ("\n" + "\n".join(notes) if notes else ""),
            exit_code=0,
            duration=time.time() - debut,
        )
        return c

    def _appeler_derive(self, api: str, declare: dict, notes: list) -> dict:
        """Appeler l'endpoint de dérive avec le corps le plus juste : celui
        du curl déclaré, sinon un corps tiré du schéma OpenAPI de l'API
        (/evaluate attend un lot de données, 467379), sinon sans corps.

        On ne passe au suivant que sur un refus 4xx : un délai dépassé ne se
        rejoue pas (DELAI_DERIVE_S chacun, budget de la VM)."""
        import json
        import urllib.parse

        chemin, methode = declare["chemin"], declare["methode"]
        essais = []
        if declare.get("corps"):
            essais.append((f"corps du curl déclaré ({declare.get('source')})", methode,
                           declare["corps"].encode(), {}))
        try:
            spec = self._lire_json(f"{api}/openapi.json")
            requete = sondes_prometheus.requete_depuis_openapi(spec, chemin, methode)
        except Exception:
            requete = None
        if requete:
            corps = None if requete["corps"] is None else json.dumps(requete["corps"]).encode()
            cles = ", ".join(f"{k} ({len(v)} ligne(s))" if isinstance(v, list) else k
                             for k, v in requete["corps"].items()) if isinstance(requete["corps"], dict) else ""
            detail = (f" : {cles}" if cles else "") + (
                f" ; paramètres {', '.join(requete['query'])}" if requete["query"] else "")
            essais.append(("corps généré depuis le schéma OpenAPI de l'API (/openapi.json)" + detail,
                           requete["methode"], corps, requete["query"]))

        for source, methode_essai, corps, query in essais:
            url = f"{api}{chemin}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
            entetes = {"Content-Type": "application/json"} if corps is not None else None
            reponse = sonder(url, methode=methode_essai, corps=corps, entetes=entetes, timeout=DELAI_DERIVE_S)
            code = reponse.get("code")
            notes.append(f"endpoint de dérive {chemin} ({methode_essai}) avec {source} → {code}")
            if not (isinstance(code, int) and 400 <= code < 500):
                return reponse

        reponse = sonder(f"{api}{chemin}", methode=methode, timeout=DELAI_DERIVE_S)
        if reponse.get("code") == 405 and methode != "GET":
            reponse = sonder(f"{api}{chemin}", methode="GET", timeout=DELAI_DERIVE_S)
        if essais:
            notes.append(f"endpoint de dérive {chemin} rappelé sans corps → {reponse.get('code')}")
        return reponse

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
                sante, _ = self._sante_du_conteneur(conteneur)
                lignes.append(f"{service} ({conteneur.name}) : {conteneur.status}"
                              + (f" ({code})" if conteneur.status != "running" else "")
                              + (f", {sante}" if sante else ""))
            parties.append("--- services ---\n" + "\n".join(lignes))
            deja_diagnostiques = set((self._cause_demarrage or {}).get("services") or {})
            for conteneur in conteneurs:
                sante, sonde = self._sante_du_conteneur(conteneur)
                # 467094 : « bike-api is unhealthy » est un conteneur `running`.
                # Seul un conteneur running dont la santé n'est pas tranchée
                # en bien (unhealthy, starting) garde ses logs ; sain ou sans
                # healthcheck, il reste omis.
                if conteneur.status == "running" and sante not in ("unhealthy", "starting"):
                    continue
                service = conteneur.labels.get("com.docker.compose.service", conteneur.name)
                if service in deja_diagnostiques:
                    # Logs, sondes et healthcheck déjà relevés (200 lignes) et
                    # classés : pas de seconde capture.
                    parties.append(f"--- {service} : santé, sondes et logs dans l'étape "
                                   "« Diagnostic des services non sains » ---")
                    continue
                if sonde is not None:
                    code_sonde, sortie_sonde = sonde
                    parties.append(f"--- dernière sonde de santé de {service} (code {code_sonde}) ---\n"
                                   f"{sortie_sonde[-1000:].strip()}")
                try:
                    logs = conteneur.logs(tail=40).decode("utf-8", errors="replace").strip()
                except Exception as erreur:
                    logs = f"logs illisibles : {erreur}"
                parties.append(f"--- logs de {service} (fin) ---\n{logs}")
        return "\n".join(parties)

    @staticmethod
    def _sante_du_conteneur(conteneur):
        """(état de santé, (code, sortie) de la dernière sonde), None si absent.

        Pas de healthcheck ou attrs sans `Health` : (None, None), jamais
        d'exception.
        """
        sante = (conteneur.attrs.get("State") or {}).get("Health") or {}
        statut = sante.get("Status") or None
        journal = sante.get("Log") or []
        sonde = None
        if journal:
            derniere = journal[-1] or {}
            sonde = (derniere.get("ExitCode"), str(derniere.get("Output") or ""))
        return statut, sonde

    def _avec_cause(self, result: dict) -> dict:
        self._avec_lancement(result)
        if self._cause_demarrage is not None and isinstance(result, dict):
            result["cause_demarrage"] = self._cause_demarrage
            # Un `up` relancé avec succès laisse l'évaluation imputer seule.
            reessai = self._cause_demarrage.get("reessai") or {}
            # Reconstruction datée saine : la stack a été évaluée, la copie
            # fonctionnait à la date du rendu, pas de faute à poser.
            reconstruite = (self._reconstruction or {}).get("resultat") == "saine"
            if reessai.get("resultat") != "sain" and not reconstruite:
                result.setdefault("fault", diagnostic_demarrage.FAUTE_DU_CONTRAT.get(
                    self._cause_demarrage["imputation"], "indetermine"))
        return result

    def _releve_sante(self) -> dict:
        """{service: (conteneur, attrs)} relus maintenant."""
        releve = {}
        for conteneur in self._conteneurs_du_projet():
            try:
                conteneur.reload()
            except Exception:
                pass
            service = conteneur.labels.get("com.docker.compose.service", conteneur.name)
            releve[service] = (conteneur, conteneur.attrs)
        return releve

    def _diagnostiquer_up_rate(self, exc: Exception, reessai_possible: bool) -> bool:
        """Relever et classer les services non sains d'un `up --wait` raté.

        467094 : bike-api était « running » (restart: always) mais unhealthy ;
        seuls les conteneurs arrêtés étaient relevés, la cause restait
        inconnue. Pour chaque service non sain ou sorti : ses logs, l'état et
        l'historique de son healthcheck, sa définition. La cause classée va
        dans le résultat (`cause_demarrage`).

        Rend True quand tous les services non sains attendaient seulement
        (healthcheck lent, pas de plantage) et sont devenus sains après une
        attente bornée : le `up` est alors relancé une fois. Ne lève jamais :
        l'exception d'origine doit rester celle qu'on voit.
        """
        debut = time.time()
        try:
            if not reessai_possible:
                if self._cause_demarrage and self._cause_demarrage.get("reessai"):
                    self._cause_demarrage["reessai"]["resultat"] = f"second up en échec : {exc}"[:300]
                    self._etape_diagnostic["exit_code"] = 2
                    self._etape_diagnostic["output"] += (
                        f"\nréessai : le second `up --wait` échoue aussi ({str(exc)[:200]}).")
                return False
            # Recensées ici aussi : sans `up` réussi, les images construites
            # restaient sur la machine après le ménage.
            self._retenir_images_de_la_copie()
            releve = self._releve_sante()
            # Sans healthcheck, un service running qui a redémarré n'est en
            # cause que si la boucle se voit : un redémarrage en attendant sa
            # dépendance, puis stable, n'est pas un plantage.
            a_observer = [s for s, (_, a) in releve.items()
                          if (a.get("RestartCount") or 0) > 0
                          and not (a.get("State") or {}).get("Health")]
            boucles = set()
            if a_observer:
                en_boucle = self._boucles_de_redemarrage(delai=20)
                boucles = {s for s, (c, _) in releve.items() if c.name in en_boucle}
            noms = diagnostic_demarrage.services_non_sains(
                {s: a for s, (_, a) in releve.items()}, boucles)
            if not noms:
                # 460059 : aucun conteneur, le build a échoué. Une panne réseau
                # (PyPI, apt, registre) est une panne d'infrastructure : le up
                # est relancé une fois, et si elle persiste la faute est au
                # système, jamais à l'apprenant.
                sortie_build = "\n".join(
                    contenu.decode("utf-8", errors="replace") if isinstance(contenu, bytes) else str(contenu)
                    for contenu in (getattr(exc, "stderr", None), getattr(exc, "stdout", None)) if contenu)
                reseau = classer_echec_build(sortie_build)
                if reseau is not None:
                    retenue = {"imputation": reseau["imputation"], "cause": reseau["cause"], "service": None,
                               "preuve": lancement.masquer_secrets(reseau["preuve"]), "services": {},
                               # Rendu « sain » si le second build passe ; la
                               # branche du second échec le réécrit.
                               "reessai": {"motif": "panne réseau pendant le build", "resultat": "sain"}}
                    self._cause_demarrage = retenue
                    self.record_step(
                        "Diagnostic des services non sains",
                        command="sortie du build (bloc de l'étape en échec, `failed to solve`)",
                        output=(f"cause retenue : reseau_build → imputation {retenue['imputation']}\n"
                                f"{retenue['preuve']}\nle build est relancé une fois."),
                        exit_code=2,
                        duration=time.time() - debut,
                    )
                    self._etape_diagnostic = self.steps[-1]
                    return True
            causes, retenue = self._consigner_diagnostic(
                releve, noms, "aucun service non sain relevé (le `up` a échoué avant ou sans "
                              f"healthcheck) : {str(exc)[:300]}", time.time() - debut)
            if not noms or not all(c["reessayable"] for c in causes.values()):
                return False
            return self._attendre_sante(noms, releve, retenue)
        except Exception as erreur:
            self.logger.warning(f"Diagnostic des services non sains impossible : {erreur}")
            return False

    def _consigner_diagnostic(self, releve: dict, noms: list, vide: str, duree: float) -> tuple:
        """Classer les services `noms` (logs, santé, healthcheck), consigner
        l'étape « Diagnostic des services non sains » et retenir la cause :
        (causes, retenue). Partagé par le `up` raté et la stack tombée après
        un `up` réussi (461338)."""
        causes, blocs = {}, []
        for service in noms:
            conteneur, attrs = releve[service]
            try:
                logs = conteneur.logs(tail=200).decode("utf-8", errors="replace")
            except Exception as erreur:
                logs = f"logs illisibles : {erreur}"
            logs = diagnostic_demarrage.masquer_secrets(logs, attrs)
            causes[service] = diagnostic_demarrage.classer_service(attrs, logs)
            if causes[service]["cause"] == "reseau_sortant" and service in self._dns_retires:
                causes[service]["preuve"] += (
                    f" (la copie déclarait {_decrire_dns(self._dns_retires[service])} "
                    "pour ce service ; retiré par le moteur)")
            blocs.append(diagnostic_demarrage.decrire_service(service, attrs, logs, causes[service]))
        retenue = diagnostic_demarrage.classer_demarrage(causes)
        retenue["services"] = causes
        retenue["reessai"] = None
        self._cause_demarrage = retenue
        sortie = (f"cause retenue : {retenue['cause']} ({retenue['service']}) → imputation "
                  f"{retenue['imputation']}\n{retenue['preuve']}\n\n" + "\n\n".join(blocs))
        if not noms:
            sortie = vide
        self.record_step(
            "Diagnostic des services non sains",
            command=("docker inspect (State, State.Health, Config.Healthcheck) et "
                     "docker logs --tail 200 de chaque service non sain ou sorti "
                     "(équivalent docker compose logs --no-color --tail 200 <service>)"),
            output=sortie,
            exit_code=2,
            duration=duree,
            note=("Imputation : « copie » = faute établie de la copie ; « non_mesure » = cause "
                  "de notre côté, la copie n'a pas été mesurée ; « indetermine » = ni l'un ni "
                  "l'autre n'est établi."),
        )
        self._etape_diagnostic = self.steps[-1]
        return causes, retenue

    def _attendre_sante(self, noms: list, releve: dict, retenue: dict) -> bool:
        """Attendre que des services seulement lents deviennent sains.

        Attente : start_period + retries × (interval + timeout) du plus lent,
        plafonnée (EXAM_REESSAI_SANTE_MAX_S) et dans le budget de l'évaluation.
        """
        restant = self.timeout - (time.time() - self._debut_evaluation)
        attente = min(max(diagnostic_demarrage.delai_de_reessai(releve[n][1], REESSAI_SANTE_MAX)
                          for n in noms), max(0.0, restant))
        debut = time.time()
        statuts = {}
        while True:
            actuel = self._releve_sante()
            statuts = {n: ((actuel.get(n, (None, {}))[1].get("State") or {}).get("Health") or {}).get("Status")
                       for n in noms}
            if all(s == "healthy" for s in statuts.values()) or time.time() - debut >= attente:
                break
            time.sleep(5)
        sains = all(s == "healthy" for s in statuts.values())
        retenue["reessai"] = {"attente_max_s": round(attente, 1),
                              "attendu_s": round(time.time() - debut, 1),
                              "resultat": "sain" if sains else "toujours non sain",
                              "statuts": statuts}
        if sains:
            # Le healthcheck de la copie était trop court pour cette machine :
            # pas une faute, et le `up` relancé reprend l'évaluation normale.
            retenue.update(cause="delai_depasse", imputation=diagnostic_demarrage.NON_MESURE,
                           preuve=("healthcheck devenu sain après une attente plus longue : "
                                   "délai trop court pour la machine de correction"))
        elif any(s == "starting" for s in statuts.values()):
            retenue.update(cause="delai_depasse", imputation=diagnostic_demarrage.NON_MESURE,
                           preuve=f"healthcheck toujours en démarrage après {attente:.0f}s (plafond)")
        else:
            retenue.update(cause="healthcheck_echoue", imputation=diagnostic_demarrage.INDETERMINE,
                           preuve=f"healthcheck toujours en échec après {attente:.0f}s d'attente "
                                  f"supplémentaire, sans plantage : {statuts}")
        self._etape_diagnostic["output"] += (
            f"\nréessai : attente de {retenue['reessai']['attendu_s']}s (plafond "
            f"{attente:.0f}s) → {retenue['reessai']['resultat']} {statuts}"
            + (" ; `up --wait` relancé une fois." if sains else "."))
        if sains:
            # Stack finalement saine : l'étape ne doit pas passer pour un échec
            # dont il faudrait établir la cause.
            self._etape_diagnostic["exit_code"] = 0
        return sains

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
        # interpoler le fichier pour démonter la stack (469217). Les fichiers
        # produits par la recette (rapatriés) suivent la même règle.
        errors.extend(self._retirer_rapatries())
        errors.extend(self._remettre_appledouble())
        for chemin in list(self._fichiers_harnais):
            try:
                os.remove(chemin)
                self._fichiers_harnais.remove(chemin)
            except FileNotFoundError:
                self._fichiers_harnais.remove(chemin)
            except OSError as e:
                errors.append(f"{os.path.basename(chemin)} du harnais non retiré : {e}")
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
