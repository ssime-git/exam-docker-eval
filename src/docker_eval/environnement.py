"""Les faits d'environnement du harnais qui peuvent décider d'une attribution.

461638 : les dépendances de la copie ont été installées en Python 3.11
(`uvx --python 3.11`) alors que son README prévoit `uv venv --python 3.12`.
L'échec « numpy==2.5.3 exige Python >= 3.12 » était vrai comme fait, faux
comme faute de l'apprenant, et rien dans la trace ne permettait de le voir
(scriptorium #322). Ce module dit quel Python tourne, lequel la copie
demande, sur quelle plateforme et avec quelles limites.
"""

import os
import re
import subprocess

# --- l'environnement que voit le code de la copie ---------------------------
# pi-corrector lance le moteur avec ses clés de fournisseurs LLM. Tout ce que
# le moteur lance hérite de `os.environ` : testcontainers appelle `docker
# compose` sans `env=`, `bentoml build` importe le service de la copie, le
# pytest de la copie tourne sur l'hôte. Un `${GROQ_API_KEY}` dans un compose
# d'apprenant recevait la vraie clé. On garde donc ce que Docker, compose et
# uv exigent, et les conventions d'examen ; le reste est mis de côté.
VARIABLES_SYSTEME = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TEMP", "TMP", "VIRTUAL_ENV", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    # L'hôte où joindre les ports publiés quand le correcteur tourne en conteneur.
    "PI_CORRECTOR_DOCKER_HOST",
})
PREFIXES_GARDES = ("DOCKER_", "COMPOSE_", "BUILDKIT_", "BUILDX_", "TESTCONTAINERS_",
                   "UV_", "XDG_", "LC_", "SSL_CERT_", "EXAM_")
# Le crochet d'agent est l'affaire du moteur, pas de la copie : sa commande et
# le chemin de ses clés n'ont rien à faire dans une interpolation compose.
PREFIXES_PRIVES = ("EXAM_AGENT_",)

_mis_de_cote: dict = {}


def _garde(nom: str) -> bool:
    if nom.startswith(PREFIXES_PRIVES):
        return False
    return nom in VARIABLES_SYSTEME or nom.startswith(PREFIXES_GARDES)


def isoler_environnement() -> list:
    """Retirer de `os.environ` tout ce que le code de la copie n'a pas à voir.

    Les variables retirées restent lisibles par `variable_correcteur` (crochet
    d'agent, investigateur). Idempotent : un runner imbriqué ne retire rien de
    plus. Rend les noms retirés, jamais les valeurs."""
    retires = sorted(nom for nom in os.environ if not _garde(nom))
    for nom in retires:
        _mis_de_cote[nom] = os.environ.pop(nom)
    return retires


def variable_correcteur(nom: str, defaut=None):
    """Une variable du correcteur, qu'elle ait été mise de côté ou non."""
    if nom in os.environ:
        return os.environ[nom]
    return _mis_de_cote.get(nom, defaut)


def environnement_correcteur() -> dict:
    """L'environnement complet du correcteur, pour ses propres outils (l'agent)."""
    return {**_mis_de_cote, **os.environ}


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


VERSIONS_PRISES_EN_CHARGE = ("3.10", "3.11", "3.12", "3.13")


def pythons_pour_les_tests(eval_dir: str, defaut: str) -> list:
    """Les Python à essayer pour les tests, dans l'ordre : celui que la copie
    déclare, le défaut du harnais, puis les plus récents.

    461638 : requirements.txt fixait numpy==2.5.3 (Python >= 3.12), le README
    disait `uv venv --python 3.12`, et uvx tournait en 3.11 : aucun test
    collecté, catégorie hors barème. La déclaration passe d'abord ; une copie
    qui ne déclare rien mais exige un Python récent se rattrape au repli."""
    candidats = []
    for _, version, _ in versions_python_declarees(eval_dir):
        trouve = re.search(r"3\.(\d+)", version)
        if trouve and f"3.{trouve.group(1)}" in VERSIONS_PRISES_EN_CHARGE:
            candidats.append(f"3.{trouve.group(1)}")
            break
    for version in (defaut, *VERSIONS_PRISES_EN_CHARGE):
        if version in VERSIONS_PRISES_EN_CHARGE and version >= defaut and version not in candidats:
            candidats.append(version)
    return candidats


# uv n'a pas pu installer les dépendances pour ce Python : un autre peut.
_ECHEC_INSTALLATION_UV = re.compile(
    r"No solution found when resolving|requires-python|Requires-Python|Failed to (?:build|run tool|install)"
    r"|only has wheels for|has no wheels with a matching Python")


def installation_impossible(sortie: str) -> bool:
    return bool(_ECHEC_INSTALLATION_UV.search(sortie or ""))


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
