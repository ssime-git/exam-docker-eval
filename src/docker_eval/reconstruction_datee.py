"""Reconstruction datée : rebâtir une stack aux versions de la date du rendu.

467094 : bike-api plantait sur « pyarrow requires NumPy 2.0 or newer, found
1.26.4 ». pyarrow, transitif et non épinglé, est sorti en 26.0 le 09/10 ; les
fichiers de l'archive datent du 03/10. La copie marchait à son rendu : elle
est notée sur une reconstruction aux versions de l'époque, et son vrai défaut
(dépendances transitives non figées) coûte au plus un point au barème.

Ce module ne parle ni à Docker ni au réseau. Il rend :

- la date de référence : le mtime le plus récent des fichiers du rendu, que
  unzip, bsdtar et tar restaurent à l'extraction. Un fichier ne peut pas être
  rendu avant sa dernière modification : c'est une borne inférieure certaine
  de la date de rendu, et tout paquet publié avant existait au rendu ;
- les contraintes pip : pour chaque paquet installé non épinglé par la copie
  et publié après cette date, une ligne `nom!=version` par version
  postérieure (même sens que `--uploaded-prior-to`, sans ordonner les
  versions) ;
- le compose dérivé : chaque service construit reçoit un `dockerfile_inline`,
  son Dockerfile d'origine avec, après chaque FROM, des `ARG` lus par pip et
  uv pendant le build (un ARG est dans l'environnement des RUN, il ne reste
  pas dans l'image) et le fichier de contraintes écrit par un `COPY <<EOF`.

Pourquoi pas `PIP_UPLOADED_PRIOR_TO` seul : l'option date de pip 26, et
python:3.12-slim embarque pip 25.0.1 (3.11 : 24.0), qui ignore la variable
sans rien dire. Elle est quand même posée : sans effet sur un pip ancien, elle
couvre une image à pip récent. `UV_EXCLUDE_NEWER` couvre uv.
"""

import copy
import datetime as dt
import os
import re
import stat

CONTRAINTES_DANS_L_IMAGE = "/tmp/scriptorium-contraintes-datees.txt"
COMPOSE_DATE = "docker-compose.datee.yml"
CAUSES_DECLENCHANTES = {"conflit_versions_dependances"}

# Une date avant 2020 vient d'un zip sans horodatage (1980, époque DOS) ou
# d'une horloge déréglée ; une date future, d'une horloge en avance. Ni
# l'une ni l'autre n'est une borne fiable : le fichier est ignoré.
PLANCHER = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
TOLERANCE_FUTUR = dt.timedelta(days=1)

# Écrits par le harnais dans la copie de travail, jamais par l'apprenant.
_FICHIERS_HARNAIS = re.compile(r"(^|/)(docker-compose\.datee\.yml|[^/]*\.eval\.ya?ml|\.pi-evidence/.*"
                               r"|\.bento_extracted_[^/]*/.*)$")


def _iso(instant: dt.datetime) -> str:
    return instant.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lire_iso(texte: str) -> dt.datetime:
    return dt.datetime.fromisoformat(texte.replace("Z", "+00:00"))


def date_reference(dossier: str, maintenant: dt.datetime | None = None) -> dict:
    """{date_reference: ISO UTC | None, source, fichier, ignores}.

    Fichiers réguliers seulement (lstat : un lien ne compte pas, il pourrait
    viser hors du rendu) ; `__MACOSX/` et `._*` (AppleDouble) écartés ; un
    mtime futur ou antérieur à 2020 ignoré. Le maximum d'un sous-ensemble de
    mtimes honnêtes reste une borne inférieure de la date de rendu.
    """
    maintenant = maintenant or dt.datetime.now(dt.timezone.utc)
    meilleur, fichier, retenus, ignores = None, None, 0, 0
    racine = os.path.abspath(dossier)
    for courant, dossiers, fichiers in os.walk(racine, followlinks=False):
        dossiers[:] = [d for d in dossiers if d != "__MACOSX"]
        for nom in fichiers:
            if nom.startswith("._") or nom == ".DS_Store":
                continue
            chemin = os.path.join(courant, nom)
            relatif = os.path.relpath(chemin, racine).replace(os.sep, "/")
            if _FICHIERS_HARNAIS.search(relatif):
                continue
            try:
                info = os.lstat(chemin)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            instant = dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc)
            if instant < PLANCHER or instant > maintenant + TOLERANCE_FUTUR:
                ignores += 1
                continue
            retenus += 1
            if meilleur is None or instant > meilleur:
                meilleur, fichier = instant, relatif
    if meilleur is None:
        return {"date_reference": None, "fichier": None, "ignores": ignores,
                "source": f"aucun fichier du rendu à date plausible (après 2020, pas dans le futur) ; "
                          f"{ignores} ignoré(s)"}
    return {"date_reference": _iso(meilleur), "fichier": fichier, "ignores": ignores,
            "source": f"mtime le plus récent des fichiers extraits du rendu : {fichier} "
                      f"({retenus} fichier(s) retenu(s), {ignores} ignoré(s) pour date future "
                      "ou antérieure à 2020, AppleDouble écartés)"}


def doit_reconstruire(cause: dict | None) -> bool:
    return bool(cause) and cause.get("cause") in CAUSES_DECLENCHANTES


def _normaliser(nom: str) -> str:
    return re.sub(r"[-_.]+", "-", nom).lower()


_EPINGLE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*===?\s*[^\s;#,]+\s*(?:[;#].*)?$")


def epingles(textes: list) -> set:
    """Noms normalisés épinglés par `==`/`===` dans des requirements."""
    noms = set()
    for texte in textes:
        for ligne in (texte or "").splitlines():
            trouve = _EPINGLE.match(ligne)
            if trouve:
                noms.add(_normaliser(trouve.group(1)))
    return noms


def lire_freeze(texte: str) -> dict:
    """{nom: version} depuis `pip freeze` ; lignes -e, URL et @ ignorées."""
    paquets = {}
    for ligne in (texte or "").splitlines():
        trouve = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)\s*$", ligne.strip())
        if trouve:
            paquets[trouve.group(1)] = trouve.group(2)
    return paquets


def _publication(fichiers: list) -> dt.datetime | None:
    instants = [lire_iso(f["upload_time_iso_8601"]) for f in fichiers or []
                if f.get("upload_time_iso_8601")]
    return min(instants) if instants else None


def versions_posterieures(projet: dict, date: dt.datetime) -> list:
    """Versions dont le premier fichier a été publié après `date`, dans l'ordre de publication."""
    tardives = []
    for version, fichiers in (projet.get("releases") or {}).items():
        publiee = _publication(fichiers)
        if publiee is not None and publiee > date:
            tardives.append((publiee, version))
    return [v for _, v in sorted(tardives)]


def contraintes(freeze: str, noms_epingles: set, pypi, date: dt.datetime) -> tuple:
    """(lignes de contraintes, paquets contraints, notes).

    `pypi(nom)` rend le JSON de https://pypi.org/pypi/<nom>/json, ou None.
    Seuls les paquets installés dans une version publiée après la date et
    non épinglés par la copie sont contraints.
    """
    lignes, contraints, notes = [], [], []
    for nom, installe in sorted(lire_freeze(freeze).items(), key=lambda kv: kv[0].lower()):
        if _normaliser(nom) in noms_epingles:
            continue
        projet = pypi(nom)
        if not projet:
            notes.append(f"{nom} : métadonnées PyPI indisponibles, non contraint")
            continue
        tardives = versions_posterieures(projet, date)
        if installe not in tardives:
            continue
        lignes += [f"{nom}!={v}" for v in tardives]
        contraints.append({"paquet": nom, "installe": installe, "exclues": tardives})
    return lignes, contraints, notes


_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.IGNORECASE)


def dockerfile_date(texte: str, date_iso: str, lignes_contraintes: list) -> str:
    """Le Dockerfile d'origine, avec la contrainte de date après chaque FROM."""
    sortie = []
    for ligne in texte.splitlines():
        sortie.append(ligne)
        trouve = _FROM.match(ligne)
        if not trouve or trouve.group(1).lower() == "scratch":
            continue
        sortie += [f"ARG UV_EXCLUDE_NEWER={date_iso}", f"ARG PIP_UPLOADED_PRIOR_TO={date_iso}"]
        if lignes_contraintes:
            sortie += [f"ARG PIP_CONSTRAINT={CONTRAINTES_DANS_L_IMAGE}",
                       f"COPY <<SCRIPTORIUM_CONTRAINTES {CONTRAINTES_DANS_L_IMAGE}",
                       *lignes_contraintes, "SCRIPTORIUM_CONTRAINTES"]
    return "\n".join(sortie) + "\n"


def _build(service: dict) -> dict | None:
    build = service.get("build")
    if isinstance(build, str):
        return {"context": build}
    return dict(build) if isinstance(build, dict) else None


def chemin_dockerfile(build: dict, dossier: str) -> str | None:
    """Chemin sur disque du Dockerfile d'un build local, None pour un contexte distant."""
    contexte = str(build.get("context") or ".")
    if re.match(r"^(https?|git|ssh)://|^git@", contexte):
        return None
    nom = build.get("dockerfile") or "Dockerfile"
    return os.path.normpath(os.path.join(dossier, contexte, nom))


def compose_date(doc: dict, dossier: str, date_iso: str, contraintes_par_service: dict) -> dict:
    """Le compose dérivé : `dockerfile_inline` daté pour chaque service construit.

    Le contenu inline est interpolé par compose comme le reste du YAML : les
    `$` du Dockerfile sont doublés. Le document reçu n'est pas modifié.
    """
    derive = copy.deepcopy(doc)
    for nom, service in (derive.get("services") or {}).items():
        if not isinstance(service, dict):
            continue
        build = _build(service)
        if build is None:
            continue
        if "dockerfile_inline" in build:
            texte = str(build["dockerfile_inline"]).replace("$$", "$")
        else:
            chemin = chemin_dockerfile(build, dossier)
            if chemin is None or not os.path.isfile(chemin):
                continue
            with open(chemin, encoding="utf-8", errors="replace") as lecteur:
                texte = lecteur.read()
        build.pop("dockerfile", None)
        build["dockerfile_inline"] = dockerfile_date(
            texte, date_iso, contraintes_par_service.get(nom, [])).replace("$", "$$")
        service["build"] = build
    return derive


def requirements_du_contexte(build: dict, dossier: str, profondeur_max: int = 2) -> list:
    """Textes des requirements*/constraints* du contexte de build."""
    chemin = chemin_dockerfile({**build, "dockerfile": "."}, dossier)
    if chemin is None or not os.path.isdir(chemin):
        return []
    textes = []
    for courant, dossiers, fichiers in os.walk(chemin, followlinks=False):
        if os.path.relpath(courant, chemin).count(os.sep) >= profondeur_max:
            dossiers[:] = []
        dossiers[:] = [d for d in dossiers if not d.startswith(".") and d not in ("node_modules", "__MACOSX")]
        for nom in fichiers:
            if re.search(r"(requirements|constraints)[^/]*\.(txt|in)$", nom, re.IGNORECASE):
                complet = os.path.join(courant, nom)
                if os.path.isfile(complet) and not os.path.islink(complet):
                    with open(complet, encoding="utf-8", errors="replace") as lecteur:
                        textes.append(lecteur.read())
    return textes


def date_lisible(date_iso: str) -> str:
    return lire_iso(date_iso).strftime("%d/%m/%Y")
