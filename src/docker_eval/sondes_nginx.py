"""Ce que les sondes de comportement nginx lisent et concluent.

La grille nginx du 15/10 (469145) donne 35 points à des exigences prouvées en
marche : routage A/B, limitation de débit qui se déclenche, répliques v1 qui
reçoivent du trafic, 401 sur mauvais identifiants, exporter et cible
Prometheus UP. Une directive présente dans le nginx.conf ne prouve rien de
tout ça : on lit la configuration seulement pour savoir quoi envoyer (en-tête
du map, upstream par défaut), puis on regarde ce que la stack répond.

L'énoncé fait foi (Learn_Content, nginx_fr/06_nginx_fr.md) : l'en-tête A/B
`X-Experiment-Group: debug` est imposé et toujours envoyé, celui du map de la
copie n'est qu'une information ; `make test`, la validation finale, se joue
dans un conteneur jetable (`commande_make_test`, `redirections_make_test`) ;
un POST /predict se construit depuis le modèle Pydantic de l'API quand la
copie n'en déclare aucun qui passe (`corps_depuis_pydantic`).

Ce module garde les lectures et les conclusions pures ; les requêtes passent
par le runner (`ComposeRunner._sonder_comportement_nginx`). Les deux sondes
TLS ouvrent elles-mêmes leur socket : `sonder` ne sait pas fixer la version
du protocole ni rendre le certificat.
"""

import os
import re
import shutil
import socket
import ssl
import subprocess
import time

_DOSSIERS_IGNORES = (".git", ".venv", "__pycache__", "node_modules")


def fichier_nginx_conf(eval_dir: str) -> str | None:
    """Le premier nginx.conf de la copie, s'il y en a un."""
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = [d for d in dossiers if d not in _DOSSIERS_IGNORES]
        for nom in fichiers:
            if nom.lower() == "nginx.conf":
                return os.path.join(racine, nom)
    return None


def lire_nginx_conf(eval_dir: str) -> str | None:
    chemin = fichier_nginx_conf(eval_dir)
    if not chemin:
        return None
    try:
        with open(chemin, encoding="utf-8", errors="replace") as lecteur:
            return lecteur.read()
    except OSError:
        return None


def _sans_commentaires(texte: str) -> str:
    return re.sub(r"#[^\n]*", "", texte)


def routage_ab_declare(texte: str) -> dict | None:
    """L'en-tête et la valeur qui font basculer le routage A/B.

    `map $http_x_experiment_group $api_backend { default apis-v1; debug
    apis-v2; }` (469145) : l'en-tête est X-Experiment-Group, la variante
    `debug`. nginx expose l'en-tête HTTP `X-Experiment-Group` en
    `$http_x_experiment_group` : on refait le chemin inverse. Les clés
    regex (`~…`) ne se rejouent pas sans deviner une chaîne : ignorées.
    """
    for bloc in re.finditer(r"\bmap\s+\$http_(\w+)\s+\$(\w+)\s*\{([^}]*)\}", _sans_commentaires(texte)):
        defaut, variante = None, None
        for cle, valeur in re.findall(r"^\s*(\S+)\s+([^;\s]+)\s*;", bloc.group(3), re.MULTILINE):
            cle, valeur = cle.strip("'\""), valeur.strip("'\"")
            if cle == "default":
                defaut = valeur
            elif cle not in ("hostnames", "volatile", "include") and not cle.startswith("~") \
                    and variante is None:
                variante = (cle, valeur)
        if variante:
            return {
                "entete": "-".join(p.capitalize() for p in bloc.group(1).split("_")),
                "valeur": variante[0],
                "upstream_defaut": defaut,
                "upstream_variante": variante[1],
            }
    return None


def serveurs_upstream(texte: str, nom: str | None) -> list:
    """Les hôtes d'un bloc `upstream <nom>` : les services Compose visés."""
    if not nom:
        return []
    bloc = re.search(r"\bupstream\s+" + re.escape(nom) + r"\s*\{([^}]*)\}", _sans_commentaires(texte))
    if not bloc:
        return []
    return [h for h in re.findall(r"\bserver\s+([^\s:;]+)", bloc.group(1))]


# Les répliques v1 par leur nom de service quand l'upstream ne les nomme pas :
# api-v1, api_v1, apis-v1, apiv1 — jamais api-v2 ni api-v10.
_MOTIF_V1 = re.compile(r"apis?[-_]?v1(?!\d)", re.IGNORECASE)


def est_replique_v1(service: str, services_v1: list) -> bool:
    if services_v1:
        return service in services_v1
    return bool(_MOTIF_V1.search(service))


def compter_requetes(logs: bytes, chemin: str) -> int:
    """Les lignes d'accès d'un conteneur API qui citent le chemin sondé."""
    texte = logs.decode("utf-8", "replace") if isinstance(logs, bytes) else str(logs)
    return sum(1 for ligne in texte.splitlines() if chemin in ligne)


def redirection_vers_https(sondes_racine: list):
    """La sonde racine du port 80 de nginx a-t-elle été renvoyée vers https ?

    Sur 469145, nginx répond 301 vers `https://127.0.0.1/` : le 443 du
    conteneur, publié ailleurs sur l'hôte, donc injoignable tel quel. La
    sonde s'arrête en « redirection active » : le premier saut suffit à
    prouver la redirection, la cible injoignable n'est pas une faute."""
    du_port_80 = [
        s for s in sondes_racine
        if str(s.get("port", "")).split("/")[0] == "80"
        and s.get("url", "").startswith("http:")
        and "nginx" in str(s.get("service", "")).lower()
        and "exporter" not in str(s.get("service", "")).lower()
    ]
    if not du_port_80:
        return None
    for sonde in du_port_80:
        chaine = sonde.get("chaine") or []
        if chaine and str(chaine[0].get("location", "")).startswith("https://"):
            return True
    if any(isinstance(s.get("code"), int) for s in du_port_80):
        return False
    return None


# Refus côté client : l'OpenSSL du harnais n'offre pas TLS 1.1, la sonde ne
# prouve rien sur le serveur.
_REFUS_LOCAUX = ("NO_PROTOCOLS_AVAILABLE", "NO_CIPHERS_AVAILABLE", "NO_SUITABLE",
                 "no protocols available", "no ciphers available")


def issue_tls_ancien(erreur) -> bool | None:
    """Ce que prouve une poignée de main plafonnée à TLS 1.1.

    Aboutie : le serveur accepte un TLS ancien (False). Interrompue par le
    serveur (alerte protocol_version, connexion coupée) : refus (True). Le
    client local qui n'offre pas TLS 1.1, ou un serveur injoignable : on ne
    sait pas (None) — ne pas deviner."""
    if erreur is None:
        return False
    if isinstance(erreur, ssl.SSLError):
        texte = str(erreur)
        if any(marque in texte for marque in _REFUS_LOCAUX):
            return None
        return True
    if isinstance(erreur, (ConnectionResetError, BrokenPipeError)):
        return True
    return None


def tls_ancien_refuse(hote: str, port: int, timeout: float = 10) -> bool | None:
    """Tenter une poignée de main en TLS 1.0/1.1 seulement.

    OpenSSL 3 n'offre TLS 1.0/1.1 qu'au niveau de sécurité 0 : sans
    `@SECLEVEL=0`, c'est le client qui refuse, et le serveur passerait pour
    conforme sans avoir été interrogé."""
    try:
        contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        contexte.check_hostname = False
        contexte.verify_mode = ssl.CERT_NONE
        contexte.minimum_version = ssl.TLSVersion.TLSv1
        contexte.maximum_version = ssl.TLSVersion.TLSv1_1
        contexte.set_ciphers("DEFAULT:@SECLEVEL=0")
    except (ValueError, ssl.SSLError, AttributeError):
        return None
    try:
        brut = socket.create_connection((hote, port), timeout=timeout)
    except OSError:
        return None
    try:
        with contexte.wrap_socket(brut, server_hostname=hote):
            pass
    except Exception as erreur:
        return issue_tls_ancien(erreur)
    finally:
        brut.close()
    return issue_tls_ancien(None)


def certificat_pem_valide(pem: str) -> bool | None:
    """Le certificat est-il dans sa période de validité ?

    Auto-signé, c'est attendu à l'examen : on ne juge que les dates.
    `cryptography` n'est pas une dépendance du moteur ; `openssl x509` lit
    les dates. Sans openssl ou sur un PEM illisible : None."""
    if not shutil.which("openssl"):
        return None
    try:
        sortie = subprocess.run(["openssl", "x509", "-noout", "-dates"], input=pem,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if sortie.returncode != 0:
        return None
    dates = dict(re.findall(r"^(notBefore|notAfter)=(.+)$", sortie.stdout, re.MULTILINE))
    try:
        debut = ssl.cert_time_to_seconds(dates["notBefore"].strip())
        fin = ssl.cert_time_to_seconds(dates["notAfter"].strip())
    except (KeyError, ValueError):
        return None
    return debut <= time.time() <= fin


def certificat_valide(hote: str, port: int, timeout: float = 10) -> bool | None:
    try:
        pem = ssl.get_server_certificate((hote, port), timeout=timeout)
    except Exception:
        return None
    return certificat_pem_valide(pem)


# L'en-tête A/B que l'énoncé impose (06_nginx_fr.md, objectif 6) : « Nginx
# devra router le trafic vers `api-v2` uniquement si la requête contient
# l'en-tête HTTP `X-Experiment-Group: debug` ». Le map de la copie n'est
# qu'une information de plus.
ENTETE_AB_IMPOSE = ("X-Experiment-Group", "debug")

# Identifiants du cours (04_nginx_fr.md : `htpasswd -c … admin`, admin/admin) :
# publics, pas un secret de l'examen. L'énoncé n'en fixe aucun.
IDENTIFIANTS_DU_COURS = "admin:admin"

PHRASE_EXEMPLE = "Oh yeah, that was soooo cool!"
_EXEMPLES_PAR_TYPE = {"str": PHRASE_EXEMPLE, "int": 1, "float": 1.0, "bool": True}


def _modeles_pydantic(texte: str) -> dict:
    """{classe: {champ: type}} des `class X(BaseModel):` d'un module."""
    modeles = {}
    for bloc in re.finditer(r"^class\s+(\w+)\s*\(\s*(?:pydantic\.)?BaseModel\s*\)\s*:\s*\n((?:[ \t]+.*\n?|\s*\n)*)",
                            texte, re.MULTILINE):
        champs = {}
        for nom, genre in re.findall(r"^[ \t]+(\w+)\s*:\s*([\w\[\], .|]+?)\s*(?:=.*)?$", bloc.group(2), re.MULTILINE):
            champs[nom] = genre.strip()
        modeles[bloc.group(1)] = champs
    return modeles


def _exemple_pour(genre: str):
    base = re.sub(r"^(Optional|typing\.Optional)\[(.*)\]$", r"\2", genre).split("|")[0].strip()
    liste = re.match(r"^(list|List)\[(\w+)\]$", base)
    if liste:
        return [_EXEMPLES_PAR_TYPE.get(liste.group(2), PHRASE_EXEMPLE)]
    return _EXEMPLES_PAR_TYPE.get(base, PHRASE_EXEMPLE)


def corps_depuis_pydantic(eval_dir: str) -> dict | None:
    """Le corps JSON que l'API v1 attend sur POST /predict, lu dans son modèle
    Pydantic (le schéma de l'énoncé : « une API FastAPI […] qui prend une
    phrase en entrée »). Le modèle du paramètre de la route d'abord, sinon le
    premier BaseModel du module. None sans main.py lisible."""
    candidats = []
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = [d for d in dossiers if d not in _DOSSIERS_IGNORES]
        for nom in fichiers:
            if nom.endswith(".py"):
                chemin = os.path.join(racine, nom)
                # api/v1 d'abord : l'A/B ne doit pas dépendre du schéma de v2.
                candidats.append((0 if re.search(r"v1", os.path.relpath(chemin, eval_dir)) else 1, chemin))
    for _, chemin in sorted(candidats):
        try:
            with open(chemin, encoding="utf-8", errors="replace") as lecteur:
                texte = lecteur.read()
        except OSError:
            continue
        route = re.search(r"@\w+\.post\(\s*[\"']/predict/?[\"'][^)]*\)\s*\n\s*(?:async\s+)?def\s+\w+\s*\(([^)]*)\)", texte)
        if not route:
            continue
        modeles = _modeles_pydantic(texte)
        modele = next((m for m in re.findall(r"\w+\s*:\s*(\w+)", route.group(1)) if m in modeles), None)
        champs = modeles.get(modele) if modele else next(iter(modeles.values()), None)
        if champs:
            return {nom: _exemple_pour(genre) for nom, genre in champs.items()}
    return None


def redirections_make_test(texte_compose: str, env: dict | None = None) -> list:
    """(port hôte, service, port conteneur) de chaque port que la copie publie.

    run_tests.sh vise `https://localhost/predict`, donc le 443 que la copie
    publie sur SA machine. Le moteur relâche ces ports (Docker en choisit
    d'autres) : le conteneur jetable de `make test` relaie localhost:<hôte>
    vers <service>:<conteneur>, et le script tourne tel qu'il est écrit. Un
    port sans hôte fixe (« 5000 ») n'était joignable qu'au hasard : ignoré.
    Lecture des ports partagée avec le rejeu des requêtes déclarées
    (`publications_hote`, 467379) : variables `env` et plages comprises."""
    from .compose_runner import publications_hote  # compose_runner importe ce module
    return [(hote, *cibles[0]) for hote, cibles in publications_hote(texte_compose, env or {}).items()]


def _recettes_makefile(texte: str) -> dict:
    """{cible: (prérequis, lignes de recette)} d'un Makefile, sans exécuter."""
    cibles = {}
    lignes = texte.replace("\\\n", " ").splitlines()
    for i, ligne in enumerate(lignes):
        m = re.match(r"^([A-Za-z0-9_.%/ -]+?)\s*::?(?!=)(.*)$", ligne)
        if not m or ligne.startswith(("\t", " ", "#")):
            continue
        prerequis = m.group(2).split("#")[0].split(";")[0].split()
        recette = []
        for suite in lignes[i + 1:]:
            if not (suite.startswith("\t") or not suite.strip()):
                break
            recette.append(suite.strip())
        for cible in m.group(1).split():
            cibles[cible] = (prerequis, recette)
    return cibles


_APPEL_DOCKER = re.compile(r"\bdocker\b|\bdocker-compose\b|\$[({]COMPOSE[)}]|\bpodman\b", re.IGNORECASE)


def commande_make_test(eval_dir: str) -> tuple:
    """(commande, note) pour jouer la validation finale dans un conteneur
    jetable, ou (None, raison) quand c'est impossible.

    Le conteneur n'a pas le démon Docker (jamais de socket monté) : une cible
    `test` qui lance la stack (`test: start-project`, `docker compose …`) ne
    peut pas y tourner, on joue alors tests/run_tests.sh — la stack, le
    moteur l'a déjà démarrée. Un script qui appelle docker lui-même : non
    mesuré, ne pas deviner."""
    makefile = next((os.path.join(eval_dir, n) for n in ("Makefile", "makefile", "GNUmakefile")
                     if os.path.isfile(os.path.join(eval_dir, n))), None)
    if not makefile:
        return None, "pas de Makefile"
    try:
        with open(makefile, encoding="utf-8", errors="replace") as lecteur:
            recettes = _recettes_makefile(lecteur.read())
    except OSError:
        return None, "Makefile illisible"
    if "test" not in recettes:
        return None, "pas de cible `test` dans le Makefile"
    vues, a_voir, appels_docker = set(), ["test"], []
    while a_voir:
        cible = a_voir.pop()
        if cible in vues or cible not in recettes:
            continue
        vues.add(cible)
        prerequis, recette = recettes[cible]
        a_voir.extend(prerequis)
        if any(_APPEL_DOCKER.search(l) and not l.lstrip("@-+ ").startswith("#") for l in recette):
            appels_docker.append(cible)
    script = os.path.join(eval_dir, "tests", "run_tests.sh")
    if os.path.isfile(script):
        try:
            with open(script, encoding="utf-8", errors="replace") as lecteur:
                code = [l for l in lecteur.read().splitlines() if not l.lstrip().startswith("#")]
        except OSError:
            code = []
        if any(_APPEL_DOCKER.search(l) for l in code):
            return None, "tests/run_tests.sh appelle docker : non jouable sans le démon, à trancher en revue"
    if not appels_docker:
        return "make test", ""
    if os.path.isfile(script):
        return "bash tests/run_tests.sh", (
            f"la cible test appelle docker (via {', '.join(sorted(appels_docker))}) : stack déjà démarrée "
            "par le moteur, tests/run_tests.sh joué directement")
    return None, "la cible test appelle docker et il n'y a pas de tests/run_tests.sh"


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def nettoyer_sortie(texte, maxi: int = 1500) -> str:
    """La fin de la sortie d'un script, sans ses couleurs ANSI."""
    if isinstance(texte, bytes):
        texte = texte.decode("utf-8", "replace")
    propre = _ANSI.sub("", str(texte)).replace("\r", "").strip()
    return propre if len(propre) <= maxi else propre[-maxi:]


def _oui_non(valeur, oui="oui", non="non", inconnu="non mesuré") -> str:
    return inconnu if valeur is None else (oui if valeur else non)


def resume_comportement(c: dict) -> str:
    """L'étape citable : chaque champ de `nginx_comportement` en clair."""
    lignes = []
    ab = c.get("ab")
    if ab is None:
        lignes.append("routage A/B : non mesuré (pas de base TLS)")
    else:
        lignes.append(
            f"routage A/B (en-tête de l'énoncé) : POST sans en-tête → {ab['code_defaut']}, avec {ab['entete']}: "
            f"{ab['valeur']} → {ab['code_variante']} ; corps différents : {_oui_non(ab['corps_differents'])}")
        copie = ab.get("entete_copie")
        if copie:
            lignes.append(
                f"routage A/B (en-tête du map de la copie) : avec {copie['entete']}: {copie['valeur']} → "
                f"{copie['code_variante']} ; corps différents : {_oui_non(copie['corps_differents'])}")
    acceptes = c.get("identifiants_acceptes")
    if acceptes is False:
        lignes.append("identifiants : aucun identifiant accepté (ceux de la copie, puis admin:admin du cours)")
    elif acceptes:
        source = c.get("identifiants_source")
        lignes.append("identifiants : " + {
            "copie": "ceux que la copie écrit (tests, README, Makefile) sont acceptés",
            "énoncé": "seuls les identifiants de l'énoncé passent ; la copie ne les écrit nulle part",
            "cours": "seuls les identifiants du cours (admin:admin) passent ; la copie ne les écrit nulle part",
        }.get(source, "acceptés (source non relevée)"))
    rafale = c.get("rafale")
    if rafale is None:
        lignes.append("rafale : non mesurée")
    else:
        codes = ", ".join(f"{n}×{code}" for code, n in sorted(rafale["codes"].items()))
        lignes.append(
            f"rafale : {rafale['envoyees']} POST authentifiés simultanés → {codes} ; limitation déclenchée "
            f"(429/503) : {_oui_non(rafale['limite_declenchee'])} ; 2xx après une pause : "
            f"{_oui_non(rafale['retour_2xx_apres_pause'])}")
    mauvais = c.get("mauvais_identifiants")
    lignes.append(f"mauvais identifiants : {'non mesuré' if mauvais is None else mauvais}")
    sans = c.get("sans_identifiants")
    lignes.append(f"POST sans identifiants : {'non mesuré' if sans is None else sans}")
    repartition = c.get("repartition")
    if repartition is None:
        lignes.append("répliques API v1 : non mesuré (aucun conteneur v1 identifié ou logs illisibles)")
    else:
        detail = ", ".join(f"{nom} : {n} requête(s)" for nom, n in repartition["conteneurs"].items())
        lignes.append(f"répliques API v1 : {repartition['recoivent']} sur {len(repartition['conteneurs'])} "
                      f"reçoivent du trafic ({detail})")
    m = c.get("monitoring") or {}
    lignes.append(f"exporter nginx_up = 1 : {_oui_non(m.get('exporter_nginx_up'))}")
    lignes.append("Prometheus cibles : "
                  + ("non mesuré" if m.get("prometheus_cibles_up") is None else
                     f"{m['prometheus_cibles_up']} up, {m['prometheus_cibles_down']} down"))
    lignes.append("Grafana provisionné : "
                  + ("non mesuré" if m.get("grafana_datasources") is None else
                     f"{m['grafana_datasources']} source(s) de données")
                  + ("" if m.get("grafana_dashboards") is None else f", {m['grafana_dashboards']} tableau(x) de bord"))
    lignes.append(f"redirection 80 → 443 : {_oui_non(c.get('redirection_80_443'))}")
    tls = c.get("tls") or {}
    lignes.append(f"TLS 1.1 refusé : {_oui_non(tls.get('tls_ancien_refuse'))} ; "
                  f"certificat dans sa période de validité : {_oui_non(tls.get('certificat_valide'))}")
    make_test = c.get("make_test")
    if not make_test:
        lignes.append("make test : non mesuré")
    elif make_test.get("code") is None:
        lignes.append(f"make test : non mesuré ({make_test.get('sortie', '')[:200]})")
    else:
        derniere = (make_test.get("sortie") or "").strip().splitlines()[-1:] or [""]
        lignes.append(f"make test : code {make_test['code']}" + (f" — {derniere[0][:200]}" if derniere[0] else ""))
    return "\n".join(lignes)
