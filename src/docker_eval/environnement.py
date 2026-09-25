"""Les faits d'environnement du harnais qui peuvent décider d'une attribution.

461638 : les dépendances de la copie ont été installées en Python 3.11
(`uvx --python 3.11`) alors que son README prévoit `uv venv --python 3.12`.
L'échec « numpy==2.5.3 exige Python >= 3.12 » était vrai comme fait, faux
comme faute de l'apprenant, et rien dans la trace ne permettait de le voir
(scriptorium #322). Ce module dit quel Python tourne, lequel la copie
demande, sur quelle plateforme et avec quelles limites.

Dépendances d'environnement (scriptorium #336). 461451 imposait
`dns: 172.31.0.2`, le résolveur du VPC AWS de la machine de correction : en
production la copie passait, hors de ce réseau le DNS ne répond pas. Avant
toute exécution, on relève dans le compose (et les Dockerfile) ce qui lie la
copie à un réseau précis, et on dit si la machine courante le satisfait. Un
fait, jamais une faute : il sert au conseil de portabilité, et l'investigateur
impute `environnement` (jamais `apprenant`) à une hypothèse établie qui porte
sur une dépendance non satisfaite.

Vérifications, une par type, bornées dans le temps :
- `dns` vers une IP privée : une requête DNS UDP réelle vers <ip>:53 depuis
  l'hôte (2 s). Une réponse, même une erreur DNS, prouve que le résolveur est
  joignable ; les conteneurs sortent par l'hôte. L'appartenance au même /16
  serait moins fiable : 172.31.0.2 répond dans tout le VPC, pas seulement
  dans le /16 de l'instance, et un /16 local (bridge docker 172.17-31) peut
  coïncider sans rien router.
- IP privée en dur (environment, command, entrypoint, extra_hosts,
  Dockerfile) : l'IP est-elle dans le sous-réseau d'une interface locale
  (`ip -o addr`) ? Faute de port connu, on ne peut pas tester un service.
- `network_mode: host` : satisfait sur un Docker Linux natif, pas ailleurs
  (Docker Desktop n'a pas le réseau de la machine).
- téléchargement externe au démarrage (URL http(s) vers un hôte qui n'est ni
  un service du compose ni local, dans command, entrypoint ou environment) :
  connexion TCP vers l'hôte et le port de l'URL (3 s).
"""

import ipaddress
import os
import re
import socket
import subprocess
import sys

_DOSSIERS_IGNORES = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "site-packages"}
_READMES = {"readme.md", "readme.txt", "readme", "readme.rst"}
# `uv venv --python 3.12`, `uv run --python=3.12`, `Python 3.12`, `python>=3.12`
_README_PYTHON = re.compile(
    r"--python[= ]\s*(3\.\d+(?:\.\d+)?)|\bpython\s*(?:>=|==|≥|~=)?\s*(3\.\d+(?:\.\d+)?)", re.IGNORECASE)
_REQUIRES_PYTHON = re.compile(r"""^\s*requires-python\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE | re.MULTILINE)


def _fichiers(eval_dir: str, garder, profondeur_max: int = 4):
    """Chemins relatifs des fichiers retenus, dans un ordre stable."""
    trouves = []
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = sorted(d for d in dossiers if d not in _DOSSIERS_IGNORES)
        relatif = os.path.relpath(racine, eval_dir)
        if relatif != "." and relatif.count(os.sep) + 1 > profondeur_max:
            dossiers[:] = []
            continue
        for nom in sorted(fichiers):
            if garder(nom):
                trouves.append(os.path.normpath(os.path.join(relatif, nom)))
    return trouves


def _lire(eval_dir: str, relatif: str, taille: int = 20000) -> str:
    try:
        with open(os.path.join(eval_dir, relatif), encoding="utf-8", errors="replace") as lecteur:
            return lecteur.read(taille)
    except OSError:
        return ""


def versions_python_declarees(eval_dir: str) -> list:
    """(fichier, version, extrait) pour chaque version de Python que la copie
    déclare : .python-version, requires-python du pyproject, README."""
    declarees = []
    for relatif in _fichiers(eval_dir, lambda n: n == ".python-version"):
        version = _lire(eval_dir, relatif).strip().splitlines()
        if version and version[0].strip():
            declarees.append((relatif, version[0].strip(), version[0].strip()))
    for relatif in _fichiers(eval_dir, lambda n: n == "pyproject.toml"):
        trouve = _REQUIRES_PYTHON.search(_lire(eval_dir, relatif))
        if trouve:
            declarees.append((relatif, trouve.group(1), trouve.group(0).strip()))
    for relatif in _fichiers(eval_dir, lambda n: n.lower() in _READMES):
        for ligne in _lire(eval_dir, relatif).splitlines():
            trouve = _README_PYTHON.search(ligne)
            if trouve:
                declarees.append((relatif, trouve.group(1) or trouve.group(2), ligne.strip()[:120]))
                break
    return declarees


def images_de_base(eval_dir: str) -> list:
    """(image, Dockerfile) des FROM de la copie, sans les étapes nommées."""
    images = []
    for relatif in _fichiers(eval_dir, lambda n: n == "Dockerfile" or n.startswith("Dockerfile.")
                             or n.endswith(".Dockerfile")):
        etapes = set()
        for trouve in _FROM.finditer(_lire(eval_dir, relatif)):
            image, alias = trouve.group(1), trouve.group(2)
            if image.lower() not in etapes:
                images.append((relatif, image))
            if alias:
                etapes.add(alias.lower())
    return images


def version_python_resolue(version: str | None) -> str | None:
    """La version exacte que uv choisit pour `--python <version>` (ou sans)."""
    try:
        commande = ["uv", "python", "find"] + ([version] if version else [])
        chemin = subprocess.run(commande, capture_output=True, text=True, timeout=20)
        if chemin.returncode != 0 or not chemin.stdout.strip():
            return None
        exacte = subprocess.run(
            [chemin.stdout.strip(), "-c", "import platform; print(platform.python_version())"],
            capture_output=True, text=True, timeout=20)
        return exacte.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def arch_image(reference: str) -> str | None:
    """Architecture d'une image locale, dans le vocabulaire de Docker."""
    try:
        import docker
        return docker.from_env().images.get(reference).attrs.get("Architecture")
    except Exception:
        return None


def limites(attrs: dict) -> str:
    """Limites mémoire et CPU d'un conteneur, lues dans son HostConfig."""
    hote = (attrs or {}).get("HostConfig")
    if not hote:
        return "limites non lues"
    memoire, cpus = hote.get("Memory") or 0, hote.get("NanoCpus") or 0
    if not memoire and not cpus:
        return "aucune limite mémoire ni CPU"
    if memoire >= 1024 ** 3:
        memoire_txt = f"mémoire {memoire / 1024 ** 3:.1f} Gio"
    elif memoire:
        memoire_txt = f"mémoire {memoire // 1024 ** 2} Mio"
    else:
        memoire_txt = "mémoire sans limite"
    cpus_txt = f"CPU {cpus / 1e9:g}" if cpus else "CPU sans limite"
    return f"{memoire_txt}, {cpus_txt}"


def lignes_de_depart(eval_dir: str, python_tests: str | None, arch_machine: str,
                     qemu: bool, runner_installe: bool = True) -> list:
    """Ce qu'on sait avant de lancer quoi que ce soit."""
    if not runner_installe:
        python = "celui des images de la copie (le harnais n'installe rien)"
    elif python_tests:
        resolue = version_python_resolue(python_tests)
        python = f"{python_tests} (uvx --python {python_tests}" + (f", résolu en {resolue})" if resolue else ")")
    else:
        resolue = version_python_resolue(None)
        python = "Python par défaut de uv (uvx sans --python" + (f", résolu en {resolue})" if resolue else ")")
    lignes = [f"Python des tests et des dépendances de la copie : {python}"]
    # Seulement quand la copie déclare : la recherche ne lit ni bentofile.yaml
    # ni l'image, un « aucune » affirmerait plus que ce qu'on a lu.
    lignes += [f"version déclarée par la copie : {version} ({fichier} : {extrait})"
               for fichier, version, extrait in versions_python_declarees(eval_dir)]
    bases = images_de_base(eval_dir)
    if bases:
        lignes.append("images de base : " + ", ".join(f"{image} ({fichier})" for fichier, image in bases))
    lignes.append(f"machine : {arch_machine}, émulation QEMU {'disponible' if qemu else 'absente'}")
    return lignes


# --- dépendances d'environnement ---------------------------------------------

_RESEAUX_PRIVES = [ipaddress.ip_network(r) for r in
                   ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")]
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_URL = re.compile(r"(https?)://([A-Za-z0-9.-]+)(?::(\d+))?")
_HOTES_LOCAUX = {"localhost", "0.0.0.0", "127.0.0.1", "host.docker.internal"}
_LIGNE_DOCKERFILE = re.compile(r"^\s*(ENV|ARG|RUN|CMD|ENTRYPOINT)\b(.*)$", re.IGNORECASE | re.MULTILINE)
# Plafond de vérifications réseau par copie : chacune coûte jusqu'à 3 s.
VERIFICATIONS_MAX = 8
_DETAILS = {
    "dns": "résolveur DNS privé imposé",
    "ip_privee": "IP privée en dur",
    "extra_hosts": "hôte fixé sur une IP privée (extra_hosts)",
    "network_mode": "réseau de l'hôte (network_mode: host)",
    "telechargement": "téléchargement externe au démarrage",
}


def ip_privee(texte: str) -> bool:
    try:
        ip = ipaddress.ip_address(texte)
    except ValueError:
        return False
    return any(ip in reseau for reseau in _RESEAUX_PRIVES)


def _textes(valeur) -> list:
    """Les chaînes d'une valeur compose : chaîne, liste ou dictionnaire."""
    if valeur is None:
        return []
    if isinstance(valeur, dict):
        return [f"{k}={v}" if v is not None else str(k) for k, v in valeur.items()]
    if isinstance(valeur, (list, tuple)):
        return [str(v) for v in valeur]
    return [str(valeur)]


def _hote_extra(entree: str) -> tuple:
    """`hote:ip`, `hote=ip` ou `hote ip` : (hote, ip)."""
    for separateur in ("=", " ", ":"):
        if separateur in entree:
            hote, _, ip = entree.partition(separateur)
            return hote.strip(), ip.strip()
    return entree, ""


def dependances_environnement(eval_dir: str, compose_file: str | None = None) -> list:
    """Ce qui lie la copie à un réseau précis, lu sans rien exécuter.
    Chaque dépendance : {type, service (None pour un Dockerfile), valeur,
    source, detail}. Compose illisible : aucune."""
    try:
        import yaml
        with open(compose_file or os.path.join(eval_dir, "docker-compose.yml"),
                  encoding="utf-8", errors="replace") as lecteur:
            compose = yaml.safe_load(lecteur) or {}
        services = compose.get("services") or {}
        if not isinstance(services, dict):
            return []
    except Exception:
        return []
    noms = {str(n).lower() for n in services}
    deps = []

    def ajouter(type_, service, valeur, source):
        cle = (type_, service, valeur)
        if all((d["type"], d["service"], d["valeur"]) != cle for d in deps):
            deps.append({"type": type_, "service": service, "valeur": valeur,
                         "source": source, "detail": _DETAILS[type_]})

    def ips_et_urls(service, textes, source, urls=True):
        for texte in textes:
            for ip in _IPV4.findall(texte):
                if ip_privee(ip):
                    ajouter("ip_privee", service, ip, source)
            if not urls:
                continue
            for schema, hote, _port in _URL.findall(texte):
                hote = hote.lower()
                if hote in _HOTES_LOCAUX or hote in noms or ip_privee(hote) or "." not in hote:
                    continue
                ajouter("telechargement", service, f"{schema}://{hote}" + (f":{_port}" if _port else ""),
                        source)

    for nom, definition in services.items():
        if not isinstance(definition, dict):
            continue
        nom = str(nom)
        for ip in _textes(definition.get("dns")):
            if ip_privee(ip.strip()):
                ajouter("dns", nom, ip.strip(), "dns")
        if str(definition.get("network_mode", "")).strip() == "host":
            ajouter("network_mode", nom, "host", "network_mode")
        for entree in _textes(definition.get("extra_hosts")):
            _hote, ip = _hote_extra(entree)
            if ip_privee(ip):
                ajouter("extra_hosts", nom, ip, "extra_hosts")
        for source in ("environment", "command", "entrypoint"):
            ips_et_urls(nom, _textes(definition.get(source)), source)
    # Dockerfile : IP privées partout ; URL seulement au démarrage (CMD,
    # ENTRYPOINT), un RUN télécharge au build.
    for relatif in _fichiers(eval_dir, lambda n: n == "Dockerfile" or n.startswith("Dockerfile.")
                             or n.endswith(".Dockerfile")):
        for instruction, reste in _LIGNE_DOCKERFILE.findall(_lire(eval_dir, relatif)):
            demarrage = instruction.upper() in ("CMD", "ENTRYPOINT")
            ips_et_urls(None, [reste], relatif, urls=demarrage)
    return deps


def resolveur_repond(ip: str, port: int = 53, timeout: float = 2.0) -> bool:
    """Une requête DNS UDP (NS de la racine) : toute réponse au même id prouve
    que le résolveur est joignable depuis l'hôte."""
    requete = b"\x33\x6a\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + b"\x00\x00\x02\x00\x01"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(requete, (ip, port))
            reponse, _ = sock.recvfrom(512)
            return reponse[:2] == requete[:2]
    except OSError:
        return False


def sous_reseaux_locaux():
    """Les réseaux des interfaces de l'hôte, ou None si illisibles."""
    try:
        sortie = subprocess.run(["ip", "-o", "addr", "show"], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if sortie.returncode != 0:
        return None
    reseaux = []
    for ligne in sortie.stdout.splitlines():
        champs = ligne.split()
        if "inet" in champs:
            try:
                reseaux.append(ipaddress.ip_network(champs[champs.index("inet") + 1], strict=False))
            except (ValueError, IndexError):
                continue
    return reseaux


def hote_joignable(hote: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((hote, port), timeout=timeout):
            return True
    except OSError:
        return False


def verifier_dependance(dep: dict) -> dict:
    """Ajoute `satisfaite` (True, False, None = non vérifiable) et
    `verification` (le test mené, pour le relecteur)."""
    type_, valeur = dep["type"], dep["valeur"]
    if type_ == "dns":
        ok = resolveur_repond(valeur)
        dep["satisfaite"] = ok
        dep["verification"] = (f"requête DNS UDP vers {valeur}:53 depuis l'hôte (2 s) : "
                               + ("réponse reçue" if ok else "aucune réponse, résolveur d'un autre réseau"))
    elif type_ in ("ip_privee", "extra_hosts"):
        reseaux = sous_reseaux_locaux()
        if reseaux is None:
            dep["satisfaite"] = None
            dep["verification"] = "interfaces de l'hôte illisibles : non vérifiée"
        else:
            ip = ipaddress.ip_address(valeur)
            local = next((r for r in reseaux if ip.version == r.version and ip in r), None)
            dep["satisfaite"] = local is not None
            dep["verification"] = (f"{valeur} dans le sous-réseau local {local}" if local
                                   else f"{valeur} hors des sous-réseaux des interfaces de l'hôte")
    elif type_ == "network_mode":
        linux = sys.platform.startswith("linux")
        dep["satisfaite"] = linux
        dep["verification"] = ("Docker Linux natif : le réseau hôte est celui de la machine" if linux
                               else f"plateforme {sys.platform} : pas de réseau hôte partagé")
    elif type_ == "telechargement":
        schema, _, reste = valeur.partition("://")
        hote, _, port = reste.partition(":")
        port = int(port) if port else (443 if schema == "https" else 80)
        ok = hote_joignable(hote, port)
        dep["satisfaite"] = ok
        dep["verification"] = f"connexion TCP vers {hote}:{port} depuis l'hôte (3 s) : " + (
            "établie" if ok else "impossible")
    else:
        dep["satisfaite"], dep["verification"] = None, "type inconnu : non vérifiée"
    return dep


def verifier_dependances(deps: list) -> list:
    for rang, dep in enumerate(deps):
        if rang < VERIFICATIONS_MAX:
            verifier_dependance(dep)
        else:
            dep["satisfaite"], dep["verification"] = None, "non vérifiée (plafond de vérifications)"
    return deps


def _etat(dep: dict) -> str:
    return {True: "satisfaite", False: "non satisfaite"}.get(dep.get("satisfaite"), "non vérifiable")


def decrire_dependance(dep: dict) -> str:
    ou = dep["service"] or "Dockerfile"
    if dep["type"] == "dns":
        quoi = f"dns: {dep['valeur']}"
    elif dep["type"] == "network_mode":
        quoi = "network_mode: host"
    else:
        quoi = f"{dep['source']} : {dep['valeur']}"
    return f"{ou} : {quoi} ({dep['detail']})"


def lignes_dependances(deps: list) -> list:
    """Lignes de l'étape « Environnement du harnais »."""
    non = sum(1 for d in deps if d.get("satisfaite") is False)
    lignes = [f"dépendances d'environnement : {len(deps)} détectée(s), dont {non} "
              "non satisfaite(s) par cette machine (un fait pour la portabilité, pas une faute)"]
    lignes += [f"- {decrire_dependance(d)} — {_etat(d)} : {d.get('verification', '')}" for d in deps]
    return lignes
