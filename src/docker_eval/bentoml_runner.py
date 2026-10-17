"""
BentoML runner for evaluations.

Handles BentoML evaluations that use docker load + docker run pattern
with pre-built Docker images.
"""

import ast
import json
import logging
import os
import re
import shutil
import socket
import tarfile
from urllib.parse import urlparse
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import time
from typing import Dict, Any, Optional

import requests
from testcontainers.core.container import DockerContainer

from . import environnement
from .base_runner import BaseRunner, verrou_images
from .utils import find_credentials_in_texts, looks_like_test_file
from .config import BENTOML_PORT, BENTOML_READY_PATTERN, API_STARTUP_TIMEOUT, EMULATION_TIMEOUT_FACTOR, PYTHON_TESTS


def premier_nombre(donnee):
    """La première valeur numérique hors erreur et métadonnées HTTP.

    Le nom du champ de prédiction reste libre. Un code de statut n'est pas
    une prédiction et une réponse explicitement en erreur ne la prouve pas.
    """
    if isinstance(donnee, bool):
        return None
    if isinstance(donnee, (int, float)):
        return donnee
    if isinstance(donnee, dict):
        if any(donnee.get(cle) for cle in ("error", "errors")):
            return None
        donnee = [valeur for cle, valeur in donnee.items()
                  if cle not in {"status", "status_code", "code", "detail", "message"}]
    if isinstance(donnee, list):
        for element in donnee:
            trouve = premier_nombre(element)
            if trouve is not None:
                return trouve
    return None


# Noms de champ qui portent la prédiction, par priorité (sous-chaîne du nom,
# en minuscules) : `chance_of_admit` (469475) relève de « chance ». « score »
# vient en dernier, `gre_score` étant un nom d'entrée parfois renvoyé.
CHAMPS_PREDICTION = ("prediction", "predicted", "chance", "admit", "probab", "result",
                     "estimat", "output", "score")


def _nombre_de(valeur):
    if isinstance(valeur, bool):
        return None
    if isinstance(valeur, (int, float)):
        return valeur
    if isinstance(valeur, list):
        return next((n for n in (_nombre_de(v) for v in valeur) if n is not None), None)
    if isinstance(valeur, dict):
        return lire_prediction(valeur)
    return None


def reponse_en_erreur(donnee) -> bool:
    """Un JSON qui dit l'erreur : `error`/`errors`, ou `detail` ou un
    `status` ≥ 400 sans champ de prédiction (451711 : `200 {"error": ...,
    "status": 401}`)."""
    if not isinstance(donnee, dict):
        return False
    if any(donnee.get(cle) for cle in ("error", "errors")):
        return True
    porte_prediction = any(any(nom in str(cle).lower() for nom in CHAMPS_PREDICTION[:-1])
                           for cle in donnee)
    if porte_prediction:
        return False
    if donnee.get("detail"):
        return True
    for cle in ("status", "status_code", "code"):
        valeur = donnee.get(cle)
        if isinstance(valeur, int) and not isinstance(valeur, bool) and valeur >= 400:
            return True
    return False


def lire_prediction(donnee):
    """La prédiction d'une réponse /predict, ou None : champ dédié d'abord,
    puis repli sur le premier nombre hors métadonnées HTTP."""
    if reponse_en_erreur(donnee):
        return None
    if isinstance(donnee, dict):
        for nom in CHAMPS_PREDICTION:
            for cle, valeur in donnee.items():
                if nom in str(cle).lower():
                    trouve = _nombre_de(valeur)
                    if trouve is not None:
                        return trouve
    return premier_nombre(donnee)


# Champs du corps /predict qui transportent le jeton (451711 : `token`).
CHAMPS_JETON = ("token", "access_token", "jwt", "auth_token", "bearer", "authorization")


def substituer_jeton(corps, jeton: str):
    """Le corps avec le jeton du login à la place des placeholders : tout
    champ `token`/`access_token`/`jwt`... (« <TOKEN_FROM_LOGIN> »,
    « VOTRE_TOKEN », « {token} », un jeton d'exemple périmé), et toute
    occurrence de `{token}`. Un préfixe « Bearer » est conservé."""
    if isinstance(corps, dict):
        sortie = {}
        for cle, valeur in corps.items():
            if str(cle).lower() in CHAMPS_JETON and isinstance(valeur, str):
                prefixe = "Bearer " if valeur.lower().startswith("bearer ") else ""
                sortie[cle] = prefixe + jeton
            else:
                sortie[cle] = substituer_jeton(valeur, jeton)
        return sortie
    if isinstance(corps, list):
        return [substituer_jeton(v, jeton) for v in corps]
    if isinstance(corps, str):
        return corps.replace("{token}", jeton)
    return corps


def contient_jeton(corps, jeton: str) -> bool:
    if not jeton:
        return False
    if isinstance(corps, dict):
        return any(contient_jeton(v, jeton) for v in corps.values())
    if isinstance(corps, list):
        return any(contient_jeton(v, jeton) for v in corps)
    return isinstance(corps, str) and jeton in corps


def retirer_jeton(corps, jeton: str):
    """Le corps sans les champs qui portent le jeton."""
    if isinstance(corps, dict):
        return {k: retirer_jeton(v, jeton) for k, v in corps.items()
                if not (isinstance(v, str) and jeton in v)}
    if isinstance(corps, list):
        return [retirer_jeton(v, jeton) for v in corps]
    return corps


def alterer_jeton(corps, jeton: str):
    """Le corps avec un jeton falsifié à la place du vrai."""
    if isinstance(corps, dict):
        return {k: alterer_jeton(v, jeton) for k, v in corps.items()}
    if isinstance(corps, list):
        return [alterer_jeton(v, jeton) for v in corps]
    if isinstance(corps, str) and jeton in corps:
        return corps.replace(jeton, _alterer(jeton))
    return corps


# Profil médian d'admission.csv, pour les sondes /predict. L'énoncé impose
# « un modèle de régression linéaire », non borné : la chance d'admission
# (« notée sur 1 ») d'un profil extrême peut dépasser 1 sur une copie juste.
PROFIL_MEDIAN = {"gre": 316, "toefl": 107, "rating": 3, "sop": 3.5, "lor": 3.5,
                 "cgpa": 8.6, "research": 1}


def _variable_du_profil(nom: str) -> Optional[str]:
    """La variable d'admission que désigne un nom de champ (GRE_Score,
    gre, University Rating...), ou None. Les noms sont libres : l'énoncé
    laisse supprimer des variables et ne fixe pas le schéma."""
    n = re.sub(r"[^a-z]", "", nom.lower())
    if "toefl" in n:
        return "toefl"
    if n.startswith("gre"):
        return "gre"
    if "rating" in n or n.startswith("univ"):
        return "rating"
    if n.startswith("sop"):
        return "sop"
    if n.startswith("lor"):
        return "lor"
    if "gpa" in n:
        return "cgpa"
    if "research" in n:
        return "research"
    return None


def profil_median(noms) -> Dict[str, Any]:
    """Un corps /predict au profil médian sous les noms de champs donnés."""
    return {nom: PROFIL_MEDIAN[_variable_du_profil(nom)] for nom in noms}


def appliquer_profil_median(corps):
    """Le même corps, valeurs numériques reconnues remplacées par le profil
    médian (enveloppes comprises). Un corps documenté au README part tel
    quel au premier appel ; la sonde de contrat le ramène au profil médian."""
    if isinstance(corps, dict):
        sortie = {}
        for nom, valeur in corps.items():
            variable = _variable_du_profil(str(nom))
            if variable and isinstance(valeur, (int, float)) and not isinstance(valeur, bool):
                sortie[nom] = PROFIL_MEDIAN[variable]
            else:
                sortie[nom] = appliquer_profil_median(valeur)
        return sortie
    if isinstance(corps, list):
        return [appliquer_profil_median(v) for v in corps]
    return corps


def profil_reconnu(corps) -> bool:
    """Le corps porte-t-il au moins une variable d'admission reconnue ?"""
    if isinstance(corps, dict):
        return any((_variable_du_profil(str(nom)) and isinstance(v, (int, float)) and not isinstance(v, bool))
                   or profil_reconnu(v) for nom, v in corps.items())
    if isinstance(corps, list):
        return any(profil_reconnu(v) for v in corps)
    return False


# Provenance du corps /predict, exposée au barème (`corps_source`). Seuls
# les corps que la copie documente elle-même engagent l'apprenant : un 4xx
# sur un corps que nous avons deviné ou reconstruit depuis les types (avec
# nos valeurs d'exemple) n'est pas une faute de la copie.
CORPS_DOCUMENTES = ("readme", "tests", "openapi")

# Noms courants du paramètre d'une API BentoML (`def predict(self,
# input_data: T)`), essayés en enveloppe quand aucun nom n'a pu être lu.
ENVELOPPES_COURANTES = ("input_data", "data", "input")

# Erreurs de `docker load` qui tiennent à l'archive livrée elle-même.
IMAGE_LIVREE_INCOHERENTE = ("too many non-empty layers in History section",)

# Copie de travail de la construction depuis la source, dans le rendu.
DOSSIER_CONSTRUCTION = ".scriptorium_construction"


def _cle_corps(corps) -> str:
    return json.dumps(corps, sort_keys=True, default=str)


def source_du_corps(contrat: Dict[str, Any], corps) -> str:
    """D'où vient un corps /predict du contrat : readme, tests, image,
    source ou devine."""
    return (contrat.get("predict_body_sources") or {}).get(_cle_corps(corps), "devine")


def source_avec_enveloppe(contrat: Dict[str, Any], corps, enveloppe: Optional[str]) -> str:
    """Provenance de la requête /predict réellement acceptée : celle du
    corps, dégradée par celle de l'enveloppe ajoutée par le correcteur.

    Un corps du README qui ne passe qu'enveloppé dans un `input_data` que la
    copie ne documente pas n'est pas un corps documenté : l'enveloppe lue
    dans la signature du service donne `image`/`source`, une enveloppe
    essayée au hasard (`request`, noms courants) donne `devine`."""
    source = source_du_corps(contrat, corps)
    if not enveloppe or source not in CORPS_DOCUMENTES:
        return source
    return (contrat.get("predict_wrapper_sources") or {}).get(enveloppe, "devine")


def _provenance_documentee(chemin: str) -> str:
    """readme, tests, image ou source, d'après le fichier où un corps a été lu."""
    if "(dans l'image)" in chemin:
        return "image"
    if looks_like_test_file(chemin):
        return "tests"
    nom = os.path.basename(chemin).lower()
    if nom.startswith("readme") or nom.endswith((".md", ".txt", ".rst")):
        return "readme"
    return "source"


_LIBELLE_CORPS = re.compile(
    r"\b(?:request\s+body|body|payload|json|corps|requête|request|input)\s*[:=]\s*", re.I)
_ROUTE_SUIVANTE = re.compile(r"\b(?:POST|GET|PUT|PATCH|DELETE)\s+/|/login\b", re.I)


def corps_en_ligne_apres_predict(texte: str):
    """Les objets JSON écrits en ligne après « /predict », derrière un
    libellé (`body:`, `payload:`, `json:`...), jusqu'à la route suivante.

    468775 : `POST /predict, header Authorization: Bearer <token>, body:
    {"input_data": {...}} returns {"prediction": 0.xx}` — ni bloc de code ni
    `curl -d`. Le corps du login, juste au-dessus, n'est pas dans la fenêtre ;
    un exemple de réponse (`returns {...}`) n'a pas de libellé et ne porte
    aucune variable d'admission."""
    decodeur = json.JSONDecoder()
    for route in re.finditer(r"/predict\b", texte):
        debut = route.end()
        fenetre = texte[debut:debut + 600]
        suivante = _ROUTE_SUIVANTE.search(fenetre)
        zone = fenetre[:suivante.start()] if suivante else fenetre
        for libelle in _LIBELLE_CORPS.finditer(zone):
            position = debut + libelle.end()
            if position >= len(texte) or texte[position] != "{":
                continue
            try:
                corps, _ = decodeur.raw_decode(texte, position)
            except ValueError:
                continue
            if isinstance(corps, dict) and profil_reconnu(corps):
                yield corps


def _attend_un_succes(fonction: ast.AST) -> bool:
    """Le test vérifie-t-il un `status_code` à 200 (`== 200`, `in (200, 201)`) ?"""
    for noeud in ast.walk(fonction):
        if not isinstance(noeud, ast.Compare):
            continue
        termes = [noeud.left, *noeud.comparators]
        # Paire par paire : `status_code == 200` ou `in (200, 201)` ; `!=`
        # et `not in` attendent un refus.
        for gauche, operateur, droite in zip(termes, noeud.ops, termes[1:]):
            if not isinstance(operateur, (ast.Eq, ast.In)):
                continue
            paire = (gauche, droite)
            if not any(isinstance(t, ast.Attribute) and t.attr == "status_code" for t in paire):
                continue
            if any(isinstance(f, ast.Constant) and f.value == 200 and not isinstance(f.value, bool)
                   for t in paire for f in ast.walk(t)):
                return True
    return False


def _vise_predict(appel: ast.Call) -> bool:
    """Le premier argument de l'appel désigne-t-il la route /predict ?"""
    if not appel.args:
        return False
    for feuille in ast.walk(appel.args[0]):
        if isinstance(feuille, ast.Constant) and isinstance(feuille.value, str) \
                and re.search(r"/predict/?$", feuille.value):
            return True
    return False


def corps_documentes_par_les_tests(texte: str) -> list:
    """Les corps JSON que les tests envoient à /predict dans un test qui
    attend un succès (462113 : `client.post("/predict", json=VALID_INPUT_DATA)`
    dans `test_jwt_authentication_success`, `assert ... == 200`).

    Lecture statique (`ast`), rien n'est importé ni exécuté. Le corps est
    écrit en ligne ou dans une constante du module. Un test qui attend un
    refus envoie souvent une entrée invalide : il ne documente rien."""
    try:
        arbre = ast.parse(texte)
    except (SyntaxError, ValueError):
        return []
    constantes = {}
    for noeud in arbre.body:
        if isinstance(noeud, ast.Assign) and len(noeud.targets) == 1 and isinstance(noeud.targets[0], ast.Name):
            try:
                constantes[noeud.targets[0].id] = ast.literal_eval(noeud.value)
            except (ValueError, TypeError, SyntaxError, RecursionError):
                continue
    corps_lus = []
    for fonction in ast.walk(arbre):
        if not isinstance(fonction, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _attend_un_succes(fonction):
            continue
        for appel in ast.walk(fonction):
            if not (isinstance(appel, ast.Call) and isinstance(appel.func, ast.Attribute)
                    and appel.func.attr == "post" and _vise_predict(appel)):
                continue
            for mot in appel.keywords:
                if mot.arg != "json":
                    continue
                if isinstance(mot.value, ast.Name):
                    corps = constantes.get(mot.value.id)
                else:
                    try:
                        corps = ast.literal_eval(mot.value)
                    except (ValueError, TypeError, SyntaxError, RecursionError):
                        corps = None
                if isinstance(corps, dict) and profil_reconnu(corps) and corps not in corps_lus:
                    corps_lus.append(corps)
    return corps_lus


# uv refuse de lire le requirements.txt de la copie (468775 : « error:
# Unexpected '·', expected '-c', '-e', '-r' or the start of a requirement:
# requirements.txt:1:2 » ; « Couldn't parse requirement in `requirements.txt` »).
_REQUIREMENTS_ILLISIBLE = re.compile(
    r"^.*(?:error:[^\n]*requirements[^\n]*\.txt:\d+:\d+|Couldn't parse requirement[^\n]*requirements[^\n]*)$",
    re.M)
_MODULE_ABSENT = re.compile(r"No module named '([A-Za-z_][\w.]*)'")
# Ce qu'un passage pytest a rendu : sa ligne de bilan, ou l'erreur qui l'a arrêté.
_RESUME_OU_ERREUR = re.compile(
    r"^(?:=+ [^\n]*\b(?:passed|failed|errors?|no tests ran)\b[^\n]* =+|E\s+\w*Error: [^\n]+)$", re.M)
_IMPORTS = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*))", re.M)
# Module importé -> paquet à installer, quand les deux noms diffèrent.
PAQUET_DU_MODULE = {"jwt": "pyjwt", "yaml": "pyyaml", "sklearn": "scikit-learn",
                    "dotenv": "python-dotenv", "PIL": "pillow", "dateutil": "python-dateutil",
                    "bs4": "beautifulsoup4", "cv2": "opencv-python-headless"}
# Dossier du bento dans une image produite par `bentoml containerize`.
CODE_DU_BENTO = "/home/bentoml/bento/src"
# setuptools fournit encore `pkg_resources` en 81 (vérifié : 79, 80.9, 81.0
# l'importent ; 82.0 et 84 non).
SETUPTOOLS_AVEC_PKG_RESOURCES = "setuptools<82"


def modules_importes(dossier: str) -> Dict[str, set]:
    """{module de premier niveau: {sous-modules importés}} dans les .py d'un dossier."""
    trouves: Dict[str, set] = {}
    for racine, _, fichiers in os.walk(dossier):
        for nom in fichiers:
            if not nom.endswith(".py"):
                continue
            try:
                with open(os.path.join(racine, nom), encoding="utf-8", errors="ignore") as lecteur:
                    texte = lecteur.read(200_000)
            except OSError:
                continue
            for depuis, imports in _IMPORTS.findall(texte):
                for chemin in ([depuis] if depuis else [m.strip() for m in imports.split(",")]):
                    morceaux = chemin.split(".")
                    suite = trouves.setdefault(morceaux[0], set())
                    if len(morceaux) > 1:
                        suite.add(morceaux[1])
    return trouves


def fichier_parasite_macos(nom: str) -> bool:
    """Vrai pour un fichier AppleDouble (`._x.py`) : métadonnées binaires que
    macOS glisse dans une archive, jamais du code de l'apprenant (469871)."""
    return os.path.basename(nom).startswith("._")


def fichiers_de_tests(dossier: str) -> list:
    """Les `.py` d'un dossier de tests, sans AppleDouble ni `__MACOSX`."""
    trouves = []
    for racine, dossiers, fichiers in os.walk(dossier):
        dossiers[:] = [d for d in dossiers if d != "__MACOSX"]
        for nom in fichiers:
            if nom.endswith(".py") and not fichier_parasite_macos(nom):
                trouves.append(os.path.join(racine, nom))
    return trouves


# Que pytest ignore lui-même ces parasites : `._x_test.py` correspond au motif
# `*_test.py` et serait collecté.
# Les noms sous lesquels les tests des apprenants attendent l'adresse du
# service ; EXAM_TEST_ENV (pi_test_env) s'y ajoute.
NOMS_URL_PAR_DEFAUT = "BASE_URL,API_URL,SERVICE_URL,SERVICE_BASE_URL,API_BASE_URL,BENTO_URL"

IGNORER_PARASITES_MACOS = ["--ignore-glob=*/._*", "--ignore-glob=*/__MACOSX/*"]

# Les noms d'une partie facultative ou bonus d'un rendu (457338 : `optional_part`).
_PARTIE_FACULTATIVE = re.compile(r"optional|optionnel|facultati|bonus")


# uv n'a pas pu joindre l'index de paquets : notre réseau, pas la copie.
_RESEAU_UV = re.compile(
    r"^.*(?:Failed to fetch|error sending request|dns error|failed to lookup address|"
    r"tcp connect error|Connection reset by peer|operation timed out).*$", re.M | re.I)
_CONNEXION_REFUSEE = re.compile(r"ConnectionError|Connection refused|NewConnectionError|ConnectionRefused")
# Ce que pytest écrit quand des tests `async def` ne tournent pas faute de
# plugin configuré : la seule preuve que le mode auto a servi.
_ASYNC = re.compile(r"^.*(?:async def functions are not natively supported|"
                    r"PytestUnhandledCoroutineWarning|was never awaited).*$", re.M)
_JETON = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")
_LIGNES_UV = re.compile(r"\s*(Downloading|Downloaded|Installed \d|Resolved \d|Prepared \d|Built |Audited |Uninstalled )")


def _paquets_avec(commande) -> list:
    """Les paquets que la commande uvx injecte (`--with`), avant le lanceur."""
    avant = commande[:_position_pytest(commande)]
    return [avant[i + 1] for i, a in enumerate(avant[:-1]) if a == "--with"]


def _paquets_declares(requirements_path: str) -> list:
    """Noms des paquets du requirements.txt, normalisés (minuscules, `-`)."""
    try:
        with open(requirements_path, encoding="utf-8") as f:
            lignes = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    noms = []
    for ligne in lignes:
        ligne = ligne.split("#", 1)[0].strip()
        if not ligne or ligne.startswith("-"):
            continue
        nom = re.split(r"[\s<>=!~\[;@]", ligne, maxsplit=1)[0].lower().replace("_", "-")
        if nom:
            noms.append(nom)
    return noms


def _tests_async(tests_path: str) -> bool:
    for chemin in fichiers_de_tests(tests_path):
        try:
            with open(chemin, encoding="utf-8") as f:
                if re.search(r"^\s*async\s+def\s+test", f.read(), re.M):
                    return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def _nomme_dans_les_tests(tests_path: str, nom: str) -> bool:
    motif = re.compile(rf"\b{re.escape(nom)}\b")
    for chemin in fichiers_de_tests(tests_path):
        try:
            with open(chemin, encoding="utf-8") as f:
                if motif.search(f.read()):
                    return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


# `os.getenv("API_BASE_URL", "http://127.0.0.1:3001")` : le nom sous lequel
# les tests attendent l'adresse du service, quel que soit le port documenté.
_URL_LUE_PAR_LES_TESTS = re.compile(
    r'(?:getenv|environ\.get)\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*,\s*'
    r'f?["\']https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?')
_URL_LOCALE = re.compile(r"^https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(?:/|$)")
# Les fichiers où une copie documente son environnement : le README et les
# modèles de .env livrés pour être copiés.
_FICHIER_DOCUMENTATION = re.compile(
    r"^(?:readme(?:\.[a-z]+)?|\.?env\.(?:example|sample|template|dist)|example\.env)$", re.I)
_AFFECTATION_DOCUMENTEE = re.compile(
    r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s#]+)\s*(?:#.*)?$", re.M)


def _noms_d_url_lus_par_les_tests(tests_path: str) -> list:
    """Les variables d'environnement d'où les tests lisent l'adresse du
    service. 462853 lisait API_BASE_URL, absente de la convention du skill
    (`pi_test_env: BASE_URL,API_URL,SERVICE_URL`) : son défaut
    127.0.0.1:3001 ne répondait pas, 20/20 « API indisponible »."""
    noms = []
    for chemin in fichiers_de_tests(tests_path):
        try:
            with open(chemin, encoding="utf-8") as f:
                contenu = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for nom in _URL_LUE_PAR_LES_TESTS.findall(contenu):
            if nom not in noms:
                noms.append(nom)
    return noms


def _variables_documentees(racine: str) -> Dict[str, str]:
    """`export NOM=valeur` du README ou d'un .env d'exemple livré.

    Les exporter avant `pytest -v`, c'est suivre la copie : l'énoncé fait
    jouer les commandes du README telles quelles (1.6). Les consignes
    (`<VOTRE_CLE>`, `$AUTRE`) ne sont pas des valeurs."""
    documentees: Dict[str, str] = {}
    for dossier, sous_dossiers, fichiers in os.walk(racine):
        sous_dossiers[:] = [d for d in sous_dossiers
                            if d not in ("__MACOSX", ".git", "node_modules", ".venv", "venv")]
        for nom_fichier in sorted(fichiers):
            if nom_fichier.startswith("._") or not _FICHIER_DOCUMENTATION.match(nom_fichier):
                continue
            chemin = os.path.join(dossier, nom_fichier)
            try:
                if os.path.getsize(chemin) > 200_000:
                    continue
                with open(chemin, encoding="utf-8", errors="replace") as f:
                    contenu = f.read()
            except OSError:
                continue
            for nom, valeur in _AFFECTATION_DOCUMENTEE.findall(contenu):
                valeur = valeur.strip("\"'")
                if not valeur or any(c in valeur for c in "<>$"):
                    continue
                documentees.setdefault(nom, valeur)
    return documentees


_PIP_INSTALL = re.compile(
    r"^\s*(?:\$\s*)?(?:sudo\s+)?(?:python3?\s+-m\s+|uv\s+)?pip3?\s+install\s+(.+)$", re.M)
# Options de `pip install` suivies d'une valeur qui n'est pas un paquet.
_OPTIONS_A_VALEUR = {"-i", "--index-url", "--extra-index-url", "-f", "--find-links",
                     "--target", "-t", "--prefix", "--root", "--python", "--trusted-host"}


def _installations_du_readme(racine: str):
    """(paquets, fichiers de requirements) que le README fait installer avec
    `pip install`, dans l'ordre. 466697 : `pip install pytest requests`
    après `pip install -r requirements.txt`, requests absent du
    requirements.txt. Seules les lignes de commande comptent, pas la prose ;
    `pip` lui-même et les installations locales (`-e .`) sont écartés."""
    import shlex

    paquets, fichiers = [], []
    for dossier, sous_dossiers, noms in os.walk(racine):
        sous_dossiers[:] = [d for d in sous_dossiers
                            if d not in ("__MACOSX", ".git", "node_modules", ".venv", "venv")]
        for nom_fichier in sorted(noms):
            if nom_fichier.startswith("._") or not re.match(r"^readme(?:\.[a-z]+)?$", nom_fichier, re.I):
                continue
            chemin = os.path.join(dossier, nom_fichier)
            try:
                if os.path.getsize(chemin) > 200_000:
                    continue
                with open(chemin, encoding="utf-8", errors="replace") as f:
                    contenu = re.sub(r"\\\n\s*", " ", f.read())
            except OSError:
                continue
            for arguments in _PIP_INSTALL.findall(contenu):
                try:
                    jetons = shlex.split(arguments.split("#", 1)[0])
                except ValueError:
                    continue
                suivant_ignore = suivant_fichier = False
                for jeton in jetons:
                    if jeton in ("&&", ";", "||", "|"):
                        break
                    if suivant_fichier:
                        candidat = os.path.normpath(os.path.join(dossier, jeton))
                        if os.path.isfile(candidat) and candidat not in fichiers:
                            fichiers.append(candidat)
                        suivant_fichier = False
                        continue
                    if suivant_ignore:
                        suivant_ignore = False
                        continue
                    if jeton in ("-r", "--requirement"):
                        suivant_fichier = True
                        continue
                    if jeton in ("-e", "--editable", "-c", "--constraint") or jeton in _OPTIONS_A_VALEUR:
                        suivant_ignore = True
                        continue
                    if jeton.startswith("-") or jeton in (".", "..") or "/" in jeton:
                        continue
                    # pip lui-même ; une consigne (`<paquet>`, `$PAQUETS`).
                    if (re.match(r"^pip(?:[<>=!~\[]|$)", jeton, re.I)
                            or jeton.startswith("<") or "$" in jeton):
                        continue
                    if jeton not in paquets:
                        paquets.append(jeton)
    return paquets, fichiers


def _expurger(texte: str, valeurs=()) -> str:
    """Ni jeton, ni secret du conteneur dans ce qu'on expose."""
    texte = _JETON.sub("<jeton>", texte)
    texte = re.sub(r"(Bearer\s+)\S+", r"\1<jeton>", texte)
    for valeur in valeurs or ():
        texte = texte.replace(valeur, "<secret>")
    return texte


def _extrait(sortie: str, motif=None, valeurs=()) -> str:
    """Les lignes qui prouvent l'échec du premier passage : celles du motif,
    sinon la fin utile de la sortie (sans le bruit d'installation de uv)."""
    lignes = []
    if motif is not None:
        lignes = [m.group(0).strip() for m in motif.finditer(sortie or "")][:3]
    if not lignes:
        utiles = [l for l in (sortie or "").strip().splitlines() if l.strip() and not _LIGNES_UV.match(l)]
        lignes = utiles[-5:]
    return _expurger("\n".join(lignes), valeurs)[:600]


def _commande_lisible(commande) -> str:
    return " ".join(commande)


def _position_pytest(commande) -> int:
    """Index du lanceur `pytest` dans une commande uvx (pas l'argument d'un
    `--with pytest`)."""
    for i, morceau in enumerate(commande):
        if morceau == "pytest" and (i == 0 or commande[i - 1] != "--with"):
            return i
    return len(commande)


def _jetons_du_corps(donnee, profondeur: int = 0):
    """(jetons, clés) d'un corps de réponse /login : `token` et
    `access_token` d'abord, puis toute clé contenant « token », puis les clés
    d'API. `token_type` (« bearer ») et `expires_*` ne sont pas des jetons :
    les prendre ferait envoyer `Bearer bearer` à une copie JWT juste."""
    jetons, cles = [], []
    if not isinstance(donnee, dict) or profondeur > 2:
        return jetons, cles
    for nom in ("token", "access_token"):
        valeur = donnee.get(nom)
        if isinstance(valeur, str) and valeur:
            jetons.append((nom, valeur))
    for nom, valeur in donnee.items():
        bas = str(nom).lower()
        if isinstance(valeur, dict):
            sous_jetons, sous_cles = _jetons_du_corps(valeur, profondeur + 1)
            jetons += sous_jetons
            cles += sous_cles
            continue
        if not isinstance(valeur, str) or bas in ("token", "access_token"):
            continue
        if "type" in bas or "expire" in bas:
            continue
        if "token" in bas and len(valeur) >= 16:
            jetons.append((nom, valeur))
        elif "key" in bas.replace("-", "_") and len(valeur) >= 8:
            cles.append((nom, valeur))
    return jetons, cles


def acces_delivre(reponse) -> list:
    """Les façons de présenter l'accès qu'une réponse /login a délivré, à
    rejouer sur /predict, toutes exprimées en en-têtes.

    « vous êtes libres de choisir la méthode de sécurisation que vous
    voulez » (énoncé, 1.3) : jeton dans le corps (Bearer), cookie posé par
    Set-Cookie, ou clé d'API. Chaque variante : {mecanisme, entete, valeur,
    entetes}. Les faux objets de test n'ont pas toujours cookies/headers."""
    variantes = []
    try:
        donnee = reponse.json()
    except Exception:
        donnee = None
    jetons, cles = _jetons_du_corps(donnee)
    vus = set()
    for _, valeur in jetons:
        if valeur in vus:
            continue
        vus.add(valeur)
        variantes.append({"mecanisme": "jeton", "entete": "Authorization", "valeur": valeur,
                          "entetes": {"Authorization": f"Bearer {valeur}"}})
    for nom, valeur in cles:
        if valeur in vus:
            continue
        vus.add(valeur)
        noms_entete = ["X-API-Key"]
        if str(nom).lower() != "x-api-key":
            noms_entete.append(str(nom))
        for entete in noms_entete:
            variantes.append({"mecanisme": "cle", "entete": entete, "valeur": valeur,
                              "entetes": {entete: valeur}})
        variantes.append({"mecanisme": "cle", "entete": "Authorization", "valeur": valeur,
                          "entetes": {"Authorization": f"Bearer {valeur}"}})
    try:
        cookies = dict(getattr(reponse, "cookies", None) or {})
    except Exception:
        cookies = {}
    cookies = {str(k): str(v) for k, v in cookies.items() if v}
    if cookies:
        variantes.append({"mecanisme": "cookie", "entete": "Cookie",
                          "valeur": "; ".join(f"{k}={v}" for k, v in cookies.items()),
                          "cookies": cookies,
                          "entetes": {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}})
    return variantes


_LECTURE_ENV = re.compile(
    r'os\.(?:environ\.get|getenv)\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*'
    r'(?:,\s*([^)]*?))?\s*\)(\s*or\b)?')


def _lectures_env_sans_defaut(texte: str) -> set:
    """Variables lues par `os.environ.get`/`os.getenv` sans défaut utile :
    ni second argument (ou une chaîne vide, None), ni repli `or ...`."""
    noms = set()
    for nom, defaut, repli in _LECTURE_ENV.findall(texte or ""):
        if repli:
            continue
        if defaut.strip() not in ("", "''", '""', "None"):
            continue
        noms.add(nom)
    return noms


_CHAMPS_MOT_DE_PASSE = ("password", "passwd", "pwd", "mot_de_passe", "pass")
_CHAMPS_UTILISATEUR = ("username", "user", "login", "email", "user_name", "name")


def _champ(dico: dict, noms) -> Optional[str]:
    return next((cle for cle in dico if str(cle).lower() in noms), None)


def identifiants_du_login(corps, profondeur: int = 0) -> Optional[dict]:
    """Le dict du corps /login qui porte le mot de passe, quel que soit le
    nom de l'enveloppe (`credentials`, `data`, `input_data`...), ou None.
    461573 : seul `credentials` était reconnu."""
    if not isinstance(corps, dict) or profondeur > 3:
        return None
    if _champ(corps, _CHAMPS_MOT_DE_PASSE):
        return corps
    for valeur in corps.values():
        trouve = identifiants_du_login(valeur, profondeur + 1)
        if trouve is not None:
            return trouve
    return None


def tags_du_tar(chemin: str) -> list:
    """RepoTags d'une archive `docker save` (manifest.json), sinon noms
    d'image de l'index OCI. Lecture des seuls fichiers d'index : les tars
    font plusieurs centaines de Mo et rien n'est extrait sur le disque."""
    tags: list = []
    try:
        with tarfile.open(chemin) as archive:
            def lire(nom):
                try:
                    membre = archive.extractfile(nom)
                except KeyError:
                    return None
                if membre is None:
                    return None
                try:
                    return json.loads(membre.read(5_000_000).decode("utf-8"))
                except ValueError:
                    return None

            manifest = lire("manifest.json")
            for entree in manifest if isinstance(manifest, list) else []:
                for tag in (entree or {}).get("RepoTags") or []:
                    if isinstance(tag, str) and tag not in tags:
                        tags.append(tag)
            if not tags:
                index = lire("index.json")
                for entree in (index or {}).get("manifests") or [] if isinstance(index, dict) else []:
                    nom = ((entree or {}).get("annotations") or {}).get("io.containerd.image.name")
                    if isinstance(nom, str) and nom not in tags:
                        tags.append(nom)
    except (OSError, tarfile.TarError):
        return []
    return tags


def paire_dans_le_texte(texte: str, utilisateur: str, mot_de_passe: str, ecart: int = 3) -> bool:
    """L'utilisateur et le mot de passe figurent-ils ensemble : mots entiers,
    à `ecart` lignes au plus l'un de l'autre (même objet JSON, liste
    « utilisateur / mot de passe »). Deux sous-chaînes n'importe où dans le
    fichier ne suffisaient pas à prouver une paire (« admin » dans la prose)."""
    def lignes_de(valeur):
        motif = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(valeur)}(?![A-Za-z0-9_])")
        return [(n, m.start()) for n, ligne in enumerate(texte.splitlines())
                for m in motif.finditer(ligne)]

    occurrences_u = lignes_de(utilisateur)
    occurrences_p = occurrences_u if utilisateur == mot_de_passe else lignes_de(mot_de_passe)
    for u in occurrences_u:
        for p in occurrences_p:
            # Même valeur pour les deux : deux occurrences distinctes.
            if u != p and abs(u[0] - p[0]) <= ecart:
                return True
    return False


def _est_utilisateur(nom: str) -> bool:
    return "USER" in nom.upper() or nom.upper().endswith("_LOGIN")


def _est_mot_de_passe(nom: str) -> bool:
    return "PASS" in nom.upper() or "PWD" in nom.upper()


def _alterer(valeur: str) -> str:
    """Une valeur d'accès modifiée : même forme, derniers caractères changés."""
    if len(valeur) <= 8:
        return "faux"
    return valeur[:-4] + ("AAAA" if not valeur.endswith("AAAA") else "BBBB")


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
        self.credentials_source: Optional[str] = None
        self.credentials_in_tests = False
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
        # Le Python retenu pour les tests : celui que la copie déclare (461638).
        self.consigner_environnement(environnement.pythons_pour_les_tests(self.eval_dir, PYTHON_TESTS)[0])
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
                # Ce chemin (461638) rendait un résultat sans `steps` : la
                # trace, environnement compris, n'arrivait jamais au relecteur.
                resultat = self._run_image_only_evaluation()
                resultat.setdefault("steps", self.steps)
                return resultat

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
                        **self._champs_identifiants(),
                        **self._infos_image(),
                        **self._faute_du_chargement(),
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
                            try:
                                construit = self._build_bento_from_source(source_root)
                                bento_result = (self._auto_containerize_bento(
                                    bento_file_path=construit.get("bento"))
                                    if construit.get("success") else {})
                            finally:
                                self._ranger_construction()
                            if construit.get("success"):
                                if bento_result.get("success"):
                                    self.image_name = bento_result["image_name"]
                                    auto_containerized = True
                                    image_loaded = True
                                    self._image_construite = True
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
            for nom, valeur in self._env_required_by_image().items():
                self.container = self.container.with_env(nom, valeur)
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
            try:
                attrs = self.container.get_wrapped_container().attrs
            except Exception:
                attrs = {}
            self._completer_environnement_image(platform, attrs)

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

            # Investigation outillée : conteneur encore debout, étapes en échec
            # non annotées. Ne modifie jamais le verdict (calculé ensuite).
            self._investiguer_echecs()

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
                **self._infos_image(),
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
                **self._champs_identifiants(),
                **self._infos_image(),
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
                **self._champs_identifiants(),
                **self._infos_image(),
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

    def _investiguer_echecs(self) -> None:
        """Lance l'Investigator sur les étapes en échec, conteneur debout."""
        echecs = [s for s in self.steps if s.get("exit_code") not in (0, None)]
        if not echecs:
            return
        from .investigator import Investigator
        enqueteur = Investigator(self, self.eval_dir, [self.container_name])
        if not enqueteur.disponible():
            self.record_step(
                "Investigation non configurée",
                output="étapes en échec mais pas de gateway LLM "
                       "(LIORA_GATEWAY_URL/LIORA_API_KEY ou PI_CORRECTOR_INVESTIGATE_*)",
                exit_code=0,
            )
            return
        self.logger.info(f"Investigation outillée : {len(echecs)} étape(s) en échec")
        try:
            enqueteur.investiguer(echecs)
        except Exception as erreur:
            self.record_step("Investigation interrompue",
                             output=f"erreur interne : {erreur}", exit_code=1)

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
            # Charger puis retaguer au nom de la copie sous le même verrou :
            # une autre correction ne peut pas réécrire le tag entre les deux.
            with verrou_images():
                result = subprocess.run(
                    ["docker", "load", "-i", self.image_tar],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                output = (result.stdout or "") + "\n" + (result.stderr or "")
                if result.returncode != 0:
                    self.logger.error(f"docker load failed: {output.strip()}")
                    self._erreur_chargement = output.strip()
                    return None

                # Tous les « Loaded image », pas le premier seulement : un
                # tar à plusieurs tags (ou plusieurs images) laissait les
                # autres derrière lui et le barème ne les voyait pas.
                chargees = [m.strip() for m in re.findall(r"Loaded image:\s*(.+)", output)]
                if not chargees:
                    chargees = [m.strip() for m in re.findall(r"Loaded image ID:\s*(.+)", output)]
                self._images_chargees = chargees
                if chargees:
                    self.logger.info(f"✓ Image(s) loaded via docker CLI: {', '.join(chargees)}")
                    adoptees = [self.adopter_image(chargee) for chargee in chargees]
                    return adoptees[0]

            self.logger.error("docker load succeeded but no image tag/id found in output")
            return None
        except Exception as e:
            self.logger.error(f"docker load CLI error: {e}")
            return None

    def _faute_du_chargement(self) -> Dict[str, Any]:
        """fault/cause d'un `docker load` en échec, quand l'erreur dit que
        l'archive livrée est incohérente (460660 : « too many non-empty
        layers in History section », config de l'image qui ne correspond pas
        à ses couches). Les autres échecs (disque, démon) ne sont pas imputés."""
        erreur = getattr(self, "_erreur_chargement", "") or ""
        motif = next((m for m in IMAGE_LIVREE_INCOHERENTE if m in erreur), None)
        if not motif:
            return {}
        ligne = next((l.strip() for l in erreur.splitlines() if motif in l), motif)
        return {"fault": "apprenant",
                "cause": f"image livrée incohérente, refusée par docker load : {ligne[:300]}"}

    def _infos_image(self) -> Dict[str, Any]:
        """Tags réellement présents dans le tar (manifest.json, sinon
        index.json) et images annoncées par `docker load`. 462113 : le README
        lance `:latest`, le seul tag du tar est `:qdo3i3fmj6dwm7my`."""
        if getattr(self, "_image_tags", None) is None:
            self._image_tags = tags_du_tar(self.image_tar) if self.image_tar else []
        return {"image_tags": list(self._image_tags),
                "images_chargees": list(getattr(self, "_images_chargees", []) or []),
                # Distinct d'image_loaded : l'image n'a pas été livrée, nous
                # l'avons construite depuis la source du rendu.
                "image_construite_par_le_correcteur": bool(getattr(self, "_image_construite", False))}

    def _run_image_only_evaluation(self) -> Dict[str, Any]:
        """
        Fallback evaluator for submissions that only provide image + tests.
        Uses docker CLI: load image, run container, execute tests.
        """
        self._log_execution_start()

        debut = time.time()
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
                            **self._champs_identifiants(),
                            **self._infos_image(),
                            **self._faute_du_chargement(),
                        }
                    self.image_name = bento_result["image_name"]
                else:
                    return {
                        "success": False,
                        "error": "Failed to load Docker image with docker load -i",
                        "exit_code": 2,
                        "image_loaded": False,
                        **self._champs_identifiants(),
                        **self._infos_image(),
                        **self._faute_du_chargement(),
                    }
            image_loaded = True

            # Laisser Docker choisir le port hote, puis le relire. Choisir
            # nous-memes supposait de savoir ce qui est libre sur l'hote, ce
            # qu'un processus conteneurise ne peut pas savoir.
            port_mode = "ephemere"
            # Sans `--rm` : un conteneur mort au démarrage doit rester
            # lisible (logs, code de sortie) pour imputer l'échec. Le
            # nettoyage le supprime par son nom (_force_cleanup_container).
            run_cmd = [
                "docker",
                "run",
                "-d",
                "--name",
                self.container_name,
            ]
            platform = self._platform_for_image()
            if platform:
                self.logger.info(f"Image d'une autre architecture : execution en {platform}")
                run_cmd += ["--platform", platform]
                # Le chemin testcontainers posait ce drapeau, pas celui-ci :
                # les delais adaptes a l'emulation (_http_timeout, sonde de
                # demarrage) restaient aux valeurs natives en mode CLI.
                self._emulating = True
            for nom, valeur in self._env_required_by_image().items():
                run_cmd += ["-e", f"{nom}={valeur}"]
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
                    **self._champs_identifiants(),
                    **self._infos_image(),
                }

            self.cli_container_id = (run_result.stdout or "").strip() or self.container_name
            container_started = True
            self._completer_environnement_image(platform, self._attrs_conteneur_cli())
            published_host = self._resolve_published_host()
            base_url = f"http://{published_host}:{host_port}"
            self.logger.info(f"Container started via docker CLI at {base_url}")

            self._wait_for_api_ready(base_url)
            api_ready = True

            test_results = self._run_api_tests(base_url)
            pytest_results = self._run_pytest(base_url)

            # Investigation outillée : conteneur encore debout, étapes en échec
            # non annotées. Ne modifie jamais le verdict (calculé ensuite).
            self._investiguer_echecs()
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
                "credentials_source": self.credentials_source,
                "credentials_in_tests": self.credentials_in_tests,
                **self._infos_image(),
            }
            self._log_execution_end(result)
            return result

        except Exception as e:
            elapsed = time.time() - debut
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
                **self._champs_identifiants(),
                **self._infos_image(),
            }
            if container_started:
                # Même relevé que le chemin testcontainers : sans lui, un
                # conteneur mort au démarrage finissait en « cause non
                # établie » (469475).
                diagnostics = self._collect_diagnostics()
                died, code_sortie = self._container_exit_state()
                logs = self._capture_container_logs()
                if isinstance(e, TimeoutError) and died:
                    result["error"] = self._describe_container_death(code_sortie, elapsed)
                result.update({
                    "elapsed_seconds": round(elapsed, 1),
                    "container_exited": died,
                    "container_exit_code": code_sortie,
                    "diagnostics": diagnostics,
                    "container_logs": logs,
                })
                result["fault"] = self._attribute_fault(diagnostics, logs)
                result["cause"] = self._cause_de_la_faute(result["fault"], diagnostics, logs)
                self.record_step(
                    "L'evaluation s'interrompt",
                    output=_expurger(logs or result["error"], self._valeurs_a_masquer_conteneur()),
                    exit_code=code_sortie,
                    note=result["error"],
                    duration=elapsed,
                )
                self.logger.info(f"Attribution de la faute : {result['fault']}")
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

            # Charger puis retaguer au nom de la copie sous le même verrou.
            with verrou_images(), open(self.image_tar, "rb") as f:
                self.logger.info("Loading image (this may take a minute)...")
                images = client.images.load(f.read())

                if images:
                    # Tous les tags de toutes les images chargées : chacun
                    # est adopté (donc supprimé au ménage) et exposé.
                    chargees = [tag for image in images for tag in (image.tags or [image.id])]
                    self._images_chargees = chargees
                    self.logger.info(f"✓ Image(s) loaded: {', '.join(chargees)}")
                    adoptees = [self.adopter_image(chargee) for chargee in chargees]
                    return adoptees[0]
                else:
                    self.logger.error("No image found in tar file")
                    return None

        except Exception as e:
            self.logger.error(f"Error loading image: {e}")
            self._erreur_chargement = str(e)
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

            # Seulement une image construite pour CETTE copie : prendre
            # n'importe quelle image « bento » de la machine notait la copie
            # sur l'image d'une autre.
            copie = str(self.student_name).lower()
            for image in images:
                for tag in image.tags:
                    if "bento" in tag.lower() and copie in tag.lower():
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
        # Sous QEMU tout est plusieurs fois plus lent, demarrage compris :
        # 459845 emule repondait en ~52s en local et depassait les 60s sur la
        # VM. Le meme facteur que _http_timeout etend la fenetre de demarrage.
        startup_timeout = API_STARTUP_TIMEOUT * (
            EMULATION_TIMEOUT_FACTOR if getattr(self, "_emulating", False) else 1
        )
        self.logger.info(
            f"Waiting for API to be ready (timeout: {startup_timeout}s)..."
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

            if elapsed > startup_timeout:
                raise TimeoutError(f"API not ready after {startup_timeout}s")

            # Un conteneur déjà sorti ne répondra plus : inutile d'attendre la
            # fin du délai (469475 : mort en ~10 s, attente de 60 s).
            if getattr(self, "cli_container_id", None):
                mort, code_sortie = self._container_exit_state()
                if mort:
                    raise TimeoutError(
                        f"conteneur arrêté (code {code_sortie}) avant de répondre, après {elapsed:.0f}s")

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
            if tcp_ready and saw_startup_signal and elapsed > startup_timeout * 0.75:
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

    def _sample_value_for_field(self, field_name: str, field_type: Optional[str] = None) -> Any:
        """Valeur d'exemple d'un champ, au type annoncé : un champ `int`
        reçoit un entier (462113 : `CGPA: int` refusait 8.6 en 400)."""
        valeur = self._valeur_d_exemple(field_name)
        if (field_type or '').strip() == 'int' and isinstance(valeur, float):
            return int(round(valeur))
        return valeur

    def _valeur_d_exemple(self, field_name: str) -> Any:
        # Profil médian du jeu admission.csv. L'énoncé impose une régression
        # linéaire (« Créer un modèle de régression linéaire »), non bornée :
        # sur un profil extrême (GRE 330, CGPA 9) une copie correcte peut
        # rendre plus de 1 et perdre le critère de contrat.
        samples = {
            'gre_score': PROFIL_MEDIAN['gre'],
            'toefl_score': PROFIL_MEDIAN['toefl'],
            'university_rating': PROFIL_MEDIAN['rating'],
            'sop': PROFIL_MEDIAN['sop'],
            'lor': PROFIL_MEDIAN['lor'],
            'cgpa': PROFIL_MEDIAN['cgpa'],
            'research': PROFIL_MEDIAN['research'],
            'authorization': 'Bearer {token}',
            'token': '{token}',
        }
        normalized = field_name.lower()
        if normalized in samples:
            return samples[normalized]
        # Noms libres (`LOR_`, `GRE Score`) : la variable d'admission qu'ils désignent.
        variable = _variable_du_profil(field_name)
        if variable:
            return PROFIL_MEDIAN[variable]
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
                profil_median(('GRE', 'TOEFL', 'University_Rating', 'SOP', 'LOR', 'CGPA', 'Research')),
                profil_median(('gre_score', 'toefl_score', 'university_rating', 'sop', 'lor', 'cgpa', 'research')),
            ],
            'predict_body_sources': {},
            # Provenance de chaque enveloppe essayée autour du corps :
            # `request` est un essai à l'aveugle.
            'predict_wrapper_sources': {'request': 'devine'},
        }
        sources = contract['predict_body_sources']

        def provenance(corps, source):
            sources.setdefault(_cle_corps(corps), source)

        # Les identifiants sont cherches sur l'ensemble du rendu, le service
        # d'abord, les tests ensuite. Un apprenant qui les ecrit en dur dans son
        # test a quand meme droit a une correction -- et a le savoir.
        texts = list(self._iter_submission_texts())

        # Un rendu qui ne livre que l'image n'expose aucun source sur le disque.
        # Sans lui, ni les identifiants ni la forme des payloads ne sont
        # connus, et le correcteur teste a cote : mauvais noms de champs,
        # mauvais nom de parametre. On lit donc le service dans l'image.
        # Un `def predict(` dans un test est un helper qui appelle l'API, pas
        # le service : il ne dispense pas de lire l'image.
        if not any(re.search(r"def\s+predict\s*\(", text) for chemin, text in texts
                   if not looks_like_test_file(chemin)):
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
        def documenter(corps, source_path):
            contract['predict_bodies'].insert(0, corps)
            contract['predict_documented_source'] = source_path
            sources[_cle_corps(corps)] = _provenance_documentee(source_path)
            self.logger.info(f"Payload /predict lu dans {source_path}")

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
                    documenter(corps, source_path)
                break
            if contract.get('predict_documented_source'):
                break
            # Corps écrit en ligne dans la documentation (468775), pas dans
            # un `curl -d`. Documentation seulement : dans un test, un
            # `json={...}` après /predict peut être l'entrée invalide d'un
            # test d'échec.
            if _provenance_documentee(source_path) == "readme":
                corps = next(corps_en_ligne_apres_predict(text), None)
                if corps is not None:
                    documenter(corps, source_path)
                    break

        # Sans corps au README, celui que les tests de la copie envoient dans
        # un test qui attend un succès (462113 : `VALID_INPUT_DATA`). Les
        # tests livrés seulement : ceux lus dans l'image ne documentent rien.
        if not contract.get('predict_documented_source'):
            for source_path, text in texts:
                if ("(dans l'image)" in source_path or not source_path.endswith('.py')
                        or not looks_like_test_file(source_path)):
                    continue
                corps = next(iter(corps_documentes_par_les_tests(text)), None)
                if corps is not None:
                    documenter(corps, source_path)
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

        # Identifiants que nous avons fournis au conteneur (469475 : le
        # README fait générer API_USERNAME/API_PASSWORD) : le service ne
        # connaît qu'eux. Calculés avant le démarrage, donc déjà en cache.
        fournis = getattr(self, "_container_env", None) or {}
        utilisateur = next((v for n, v in sorted(fournis.items()) if _est_utilisateur(n)), None)
        mot_de_passe = next((v for n, v in sorted(fournis.items()) if _est_mot_de_passe(n)), None)
        if mot_de_passe:
            if utilisateur:
                contract['usernames'] = [utilisateur] + [u for u in contract['usernames'] if u != utilisateur]
            contract['passwords'] = [mot_de_passe] + [p for p in contract['passwords'] if p != mot_de_passe]
            if mot_de_passe in self._valeurs_generees():
                contract['credentials_source'] = (
                    "environnement fourni au conteneur (variables citées par le README, "
                    "valeurs générées par le correcteur)")
                contract['credentials_in_tests'] = False
                self.credentials_source = contract['credentials_source']
                self.credentials_in_tests = False

        signature_lue = False
        for source_path, text in texts:
            if contract['source'] == 'fallback':
                contract['source'] = source_path

            login_signature = re.search(r'def\s+login\s*\(\s*self\s*,\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*([a-zA-Z_][a-zA-Z0-9_]*)', text)
            if login_signature:
                login_wrapper = login_signature.group(1)
                if login_wrapper not in contract['login_wrappers']:
                    contract['login_wrappers'].insert(0, login_wrapper)

            # Forme BentoML `def predict(self, x: T)`, ou route FastAPI montée
            # dans BentoML `def predict(data: T, ...)` (458998). FastAPI attend
            # les champs d'un modèle unique à plat : pas d'enveloppe.
            predict_signature = re.search(r'def\s+predict\s*\(\s*(self\s*,\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*([a-zA-Z_][a-zA-Z0-9_]*)', text)
            if predict_signature:
                signature_lue = True
                route_fastapi = predict_signature.group(1) is None
                predict_wrapper = predict_signature.group(2)
                # Un parametre positionnel (marque par `/`) n'est PAS enveloppe
                # par BentoML : les champs vont a la racine du corps. Verifie
                # sur une copie reelle -- le corps nu rend 200, l'enveloppe 400.
                positional = re.search(
                    r'def\s+predict\s*\([\s\S]{0,300}?^\s*/\s*,', text, re.M
                )
                if not positional and not route_fastapi:
                    if predict_wrapper not in contract['predict_wrappers']:
                        contract['predict_wrappers'].insert(0, predict_wrapper)
                    contract['predict_wrapper_sources'][predict_wrapper] = (
                        "image" if "(dans l'image)" in source_path else "source")
                request_type = predict_signature.group(3)
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
                                    nested_name: self._sample_value_for_field(nested_name, nested_type)
                                    for nested_name, nested_type in nested_fields.items()
                                }
                                request_payload[field_name] = nested_body
                                continue
                        request_payload[field_name] = self._sample_value_for_field(field_name, field_type)
                    if request_payload:
                        # Reconstruit avec nos valeurs d'exemple : pas un
                        # corps documenté par la copie.
                        provenance(request_payload, "image" if "(dans l'image)" in source_path else "source")
                        if contract.get('predict_documented_source'):
                            # Le payload documente par l'apprenant prime sur
                            # celui reconstruit depuis les types.
                            contract['predict_bodies'].append(request_payload)
                        else:
                            contract['predict_bodies'] = [request_payload]

        # Aucune signature lue (image illisible, 468775) : les variantes à
        # plat et `request` échouent sur un service BentoML qui enveloppe au
        # nom de son paramètre. Les noms courants viennent après, pour ne
        # rien changer à l'ordre quand le nom a été lu.
        if not signature_lue:
            contract['predict_wrappers'] += [nom for nom in ENVELOPPES_COURANTES
                                             if nom not in contract['predict_wrappers']]
            for nom in ENVELOPPES_COURANTES:
                contract['predict_wrapper_sources'].setdefault(nom, "devine")

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

        login_ok = None
        predict_ok = None
        acces = []
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
                # « /login délivre un accès », quelle que soit sa forme : la
                # méthode de sécurisation est libre (énoncé, 1.3). Seul un
                # JSON {token|access_token} comptait : une copie à cookie de
                # session ou à clé d'API ratait /login, puis tout le reste.
                variantes = acces_delivre(login_response)
                if variantes:
                    acces = variantes
                    results["login_passed"] = True
                    results["token"] = variantes[0]["valeur"]
                    results["mecanisme_acces"] = variantes[0]["mecanisme"]
                    login_ok = payload
                    self.logger.info(
                        f"✓ Login successful ({variantes[0]['mecanisme']}) : "
                        f"{variantes[0]['valeur'][:20]}..."
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
        if acces:
            token = results["token"]
            predict_bodies = contract.get("predict_bodies", [])
            if not predict_bodies:
                predict_bodies = [
                    profil_median(("GRE", "TOEFL", "University_Rating", "SOP", "LOR", "CGPA", "Research")),
                    profil_median(("gre_score", "toefl_score", "university_rating", "sop", "lor", "cgpa", "research")),
                ]
            # Provenance du premier corps essayé : celle qui compte si tout
            # échoue (le corps documenté passe en premier). Remplacée par
            # celle du corps qui obtient un 200.
            if predict_bodies:
                results["corps_source"] = source_du_corps(contract, predict_bodies[0])
            for payload in predict_bodies:
                normalized_payload = substituer_jeton(payload, token)
                # L'accès obtenu au login doit accompagner la requete. Il ne
                # l'etait pas : les en-tetes partaient vides et /predict rendait
                # 401, ce qui etait compte contre l'apprenant alors que son
                # endpoint verifie correctement l'autorisation. Une variante
                # par façon de présenter l'accès (Bearer, cookie, clé).
                request_variants = []
                # Jeton porté par le corps (451711 : `input_data.token`) :
                # d'abord sans en-tête, pour savoir si le corps suffit.
                if contient_jeton(normalized_payload, token):
                    dans_le_corps = {"mecanisme": "jeton_corps", "entete": None,
                                     "valeur": token, "entetes": {}}
                    for wrapper in contract.get("predict_wrappers", [None, "request"]):
                        request_variants.append(({wrapper: normalized_payload} if wrapper else normalized_payload,
                                                 {}, dans_le_corps, wrapper))
                for variante in acces:
                    for wrapper in contract.get("predict_wrappers", [None, "request"]):
                        if wrapper:
                            request_variants.append(({wrapper: normalized_payload}, dict(variante["entetes"]),
                                                     variante, wrapper))
                        else:
                            request_variants.append((normalized_payload, dict(variante["entetes"]), variante, None))
                if isinstance(normalized_payload, dict) and 'authorization' in normalized_payload:
                    body = dict(normalized_payload)
                    auth_value = body.pop('authorization')
                    request_variants.append((body, {"Authorization": auth_value},
                                             {"mecanisme": "jeton", "entete": "Authorization",
                                              "valeur": str(auth_value).split(" ", 1)[-1]}, None))
                for body, headers, variante, enveloppe in request_variants:
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
                        # Le statut HTTP seul ne prouve pas une prédiction :
                        # certains services renvoient leurs erreurs en JSON 200.
                        results["predict_response"] = str(data)[:400]
                        if lire_prediction(data) is None:
                            self.logger.warning("HTTP 200 sans prédiction numérique : %s", str(data)[:200])
                            continue
                        results["predict_passed"] = True
                        predict_ok = (body, headers, data, variante)
                        results["mecanisme_acces"] = variante["mecanisme"]
                        # L'enveloppe gagnante compte autant que le corps :
                        # un corps du README à plat accepté seulement sous
                        # un `input_data` deviné n'est pas documenté.
                        results["corps_enveloppe"] = enveloppe
                        results["corps_source"] = source_avec_enveloppe(contract, payload, enveloppe)
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
            self.logger.info(f"Corps /predict : provenance {results.get('corps_source', 'devine')}")

        # Overall result
        results["all_passed"] = results["login_passed"] and results["predict_passed"]
        if login_ok is not None and predict_ok is not None:
            results.update(self._sonder_securite_et_contrat(base_url, login_ok, *predict_ok))
        if "identifiants_dans_la_copie" not in results:
            # /login ou /predict en échec (464154) : la présence de la paire
            # dans le README ou les tests reste une mesure statique. Paire
            # du login réussi, sinon celle lue dans la copie ; aucune paire
            # connue : non mesuré.
            paire = login_ok
            if paire is None and contract.get("credentials_source"):
                paire = {"username": contract["usernames"][0], "password": contract["passwords"][0]}
            results["identifiants_dans_la_copie"] = (
                self._identifiants_dans_la_copie(paire) if paire is not None else None)

        return results

    def _sonder_securite_et_contrat(self, base_url: str, login_ok: dict, corps: dict,
                                    entetes: dict, reponse, acces: Optional[dict] = None) -> Dict[str, Any]:
        """Ce que l'API refuse, et ce que /predict rend — sur la forme de
        requête qui a réussi, pour ne juger que le comportement.

        Les sondes nominales ne prouvaient que « ça répond » : une API sans
        aucune vérification de token avait la même note qu'une API sécurisée.
        Codes relevés tels quels ; le barème décide.

        `acces` dit comment l'accès a été présenté (jeton, cookie, clé) : la
        méthode de sécurisation est libre (énoncé, 1.3), la sonde « accès
        falsifié » modifie donc la valeur là où elle voyage. Les clés de
        résultat gardent leur nom (predict_token_falsifie...) pour que les
        rapports antérieurs se lisent toujours ; `mecanisme` précise."""
        def requete(methode, chemin, **kw):
            try:
                return requests.request(methode, f"{base_url}{chemin}",
                                        timeout=self._http_timeout(10), **kw)
            except Exception as erreur:  # une connexion coupée n'est pas un code
                return f"erreur : {type(erreur).__name__}"

        def code(methode, chemin, **kw):
            r = requete(methode, chemin, **kw)
            return r if isinstance(r, str) else r.status_code

        if acces is None:
            # Appel historique : un Bearer dans les en-têtes.
            acces = {"mecanisme": "jeton", "entete": "Authorization",
                     "valeur": (entetes.get("Authorization") or "").split(" ", 1)[-1]}
        porteurs = {"authorization", "cookie", "x-api-key", str(acces.get("entete", "")).lower()}
        sans_auth = {k: v for k, v in entetes.items() if k.lower() not in porteurs}

        valeur = str(acces.get("valeur") or "")
        mecanisme = acces.get("mecanisme", "jeton")
        if not valeur:
            entetes_falsifies = None  # rien à modifier : critère non mesuré
        elif mecanisme == "jeton_corps":
            entetes_falsifies = dict(sans_auth)  # le jeton voyage dans le corps
        elif mecanisme == "cookie" and acces.get("cookies"):
            entetes_falsifies = {**sans_auth, "Cookie": "; ".join(
                f"{k}={_alterer(v)}" for k, v in acces["cookies"].items())}
        elif mecanisme == "cookie":
            entetes_falsifies = {**sans_auth, "Cookie": _alterer(valeur)}
        elif acces.get("entete", "Authorization").lower() == "authorization":
            entetes_falsifies = {**sans_auth, "Authorization": f"Bearer {_alterer(valeur)}"}
        else:
            entetes_falsifies = {**sans_auth, acces["entete"]: _alterer(valeur)}

        mauvais_login = json.loads(json.dumps(login_ok))
        cible = identifiants_du_login(mauvais_login)
        if cible is not None:
            cible[_champ(cible, _CHAMPS_MOT_DE_PASSE)] = "mauvais-mot-de-passe-scriptorium"
        if mauvais_login == login_ok:
            # Aucun mot de passe à changer : renvoyer le bon corps mesurerait
            # un accès accepté qui n'en est pas un.
            mauvais_login = None
        invalide = json.loads(json.dumps(corps))
        cible_inv = next((v for v in invalide.values() if isinstance(v, dict)), invalide) if isinstance(invalide, dict) else {}
        for cle, val in list(cible_inv.items()) if isinstance(cible_inv, dict) else []:
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                cible_inv[cle] = "pas-un-nombre"
        # Jeton aussi (ou seulement) dans le corps (451711) : « sans accès »
        # le retire, « accès falsifié » l'altère, là où il voyage. Introuvable
        # alors qu'il doit y être : non mesuré.
        corps_sans, corps_falsifie = corps, corps
        if valeur and contient_jeton(corps, valeur):
            corps_sans, corps_falsifie = retirer_jeton(corps, valeur), alterer_jeton(corps, valeur)
        elif mecanisme == "jeton_corps":
            corps_sans = corps_falsifie = None
        refus_dans_un_200 = []

        def sonde(nom, chemin, corps_envoye, entetes_envoyes):
            if corps_envoye is None or entetes_envoyes is None:
                return None
            r = requete("POST", chemin, json=corps_envoye, headers=entetes_envoyes)
            if isinstance(r, str):
                return r
            if r.status_code == 200:
                # Code relevé tel quel ; le refus porté par le JSON est noté à
                # part, le barème décide.
                try:
                    if reponse_en_erreur(r.json()):
                        refus_dans_un_200.append(nom)
                except Exception:
                    pass
            return r.status_code

        securite = {
            "predict_sans_token": sonde("predict_sans_token", "/predict", corps_sans, sans_auth),
            "predict_token_falsifie": sonde("predict_token_falsifie", "/predict", corps_falsifie,
                                            entetes_falsifies),
            "login_mauvais_identifiants": sonde("login_mauvais_identifiants", "/login", mauvais_login, {}),
            "mecanisme": mecanisme,
        }
        if refus_dans_un_200:
            securite["refus_dans_un_200"] = refus_dans_un_200

        # La prédiction jugée vient du profil médian : la régression linéaire
        # imposée n'est pas bornée, un profil extrême sort de [0, 1] sur une
        # copie juste. Si le service refuse le corps médian, repli sur la
        # réponse du premier appel.
        prediction = lire_prediction(reponse)
        profil = "nominal"
        median = appliquer_profil_median(corps)
        if median != corps:
            r = requete("POST", "/predict", json=median, headers=entetes)
            if not isinstance(r, str) and r.status_code == 200:
                try:
                    valeur_mediane = lire_prediction(r.json())
                except Exception:
                    valeur_mediane = None
                if valeur_mediane is not None:
                    prediction, profil = valeur_mediane, "median"
        elif prediction is not None and profil_reconnu(corps):
            # Corps déjà au profil médian. Aucun champ reconnu (liste
            # positionnelle, noms inconnus) : le profil reste « nominal ».
            profil = "median"
        contrat = {
            "prediction": prediction,
            "predict_entree_invalide": code("POST", "/predict", json=invalide, headers=entetes),
        }
        self.record_step(
            "Sondes de sécurité et de contrat",
            command=f"POST /predict sans accès, avec un accès falsifié ({mecanisme}), avec une "
                    "entrée invalide, au profil médian ; POST /login avec un mauvais mot de passe",
            output="\n".join(f"{k} : {v}" for k, v in {**securite, **contrat, "profil": profil}.items()),
            exit_code=0,
        )
        return {"securite": securite, "contrat": contrat,
                "identifiants_dans_la_copie": self._identifiants_dans_la_copie(login_ok)}

    def _identifiants_dans_la_copie(self, login_ok: dict) -> bool:
        """Les identifiants qui ont ouvert /login figurent-ils dans le README
        ou les tests de la copie ? Sinon le harnais les a devinés (ou lus dans
        le code du service), et un tiers ne saurait pas relancer les tests.

        README et tests seulement : c'est ce que l'énoncé demande de livrer
        (« README.md avec les commandes strictes et ordonnées pour [...]
        lancer les tests », « Le dossier tests/ ») ; des identifiants écrits
        dans src/service.py ne documentent rien pour qui relance pytest."""
        cible = identifiants_du_login(login_ok) or {}
        champ_utilisateur = _champ(cible, _CHAMPS_UTILISATEUR)
        champ_mot_de_passe = _champ(cible, _CHAMPS_MOT_DE_PASSE)
        utilisateur = str(cible.get(champ_utilisateur, "")) if champ_utilisateur else ""
        mot_de_passe = str(cible.get(champ_mot_de_passe, "")) if champ_mot_de_passe else ""
        if not utilisateur or not mot_de_passe:
            return False
        if mot_de_passe in self._valeurs_generees():
            # Mot de passe tiré par nous pour le conteneur (469475 : le
            # README le fait générer) : sa présence dans la copie ne se
            # mesure pas.
            return None
        for racine, dossiers, fichiers in os.walk(self.eval_dir):
            dossiers[:] = [d for d in dossiers if d not in (".git", ".venv", "venv", "node_modules",
                                                            "__pycache__", ".pytest_cache")]
            dans_tests = "tests" in os.path.relpath(racine, self.eval_dir).replace("\\", "/").split("/")
            for nom in fichiers:
                bas = nom.lower()
                readme = bas.startswith("readme")
                test = dans_tests or (bas.endswith(".py") and (bas.startswith("test_") or bas.endswith("_test.py")
                                                              or bas == "conftest.py"))
                if not (readme or test):
                    continue
                try:
                    with open(os.path.join(racine, nom), encoding="utf-8", errors="ignore") as lecteur:
                        texte = lecteur.read(200_000)
                except OSError:
                    continue
                if paire_dans_le_texte(texte, utilisateur, mot_de_passe):
                    return True
        return False

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

        candidats = []
        for root, dossiers, files in os.walk(self.eval_dir):
            dossiers[:] = sorted(d for d in dossiers if d != "__MACOSX")
            if any(
                (f.startswith("test_") and f.endswith(".py")) or f.endswith("_test.py")
                for f in files
                if not fichier_parasite_macos(f)
            ):
                candidats.append(root)
        if not candidats:
            return None

        # Un rendu en deux parties (457338 : `mandatory_part/`, `optional_part/`)
        # porte deux suites de tests. Le premier dossier rendu par `os.walk`
        # dépend du système de fichiers ; on lançait parfois les tests de la
        # passerelle facultative contre l'image notée. On prend la suite la plus
        # proche de l'image évaluée, puis celle hors partie facultative, puis la
        # moins profonde.
        image = os.path.dirname(os.path.abspath(self.image_tar)) if self.image_tar else None

        def rang(dossier):
            absolu = os.path.abspath(dossier)
            proximite = 0
            if image:
                try:
                    proximite = len(os.path.commonpath([absolu, image]))
                except ValueError:
                    proximite = 0
            relatif = os.path.relpath(absolu, os.path.abspath(self.eval_dir)).lower()
            facultatif = bool(_PARTIE_FACULTATIVE.search(relatif))
            return (-proximite, facultatif, relatif.count(os.sep), relatif)

        return min(candidats, key=rang)

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

        adresse_du_service = re.compile(
            rf"http://(?:127\.0\.0\.1|localhost|0\.0\.0\.0):{BENTOML_PORT}(?![0-9])")

        for filepath in fichiers_de_tests(dst_tests_path):
            try:
                with open(filepath, encoding="utf-8") as handle:
                    content = handle.read()
            except (UnicodeDecodeError, OSError) as exc:
                # Pas du texte UTF-8 : on ne sait pas le réécrire, on le
                # laisse tel quel. La préparation ne doit jamais lever.
                self.logger.warning(
                    f"Fichier de tests illisible en UTF-8, laissé tel quel : {filepath} ({exc})")
                continue

            new_content = base_url_pattern.sub(
                r'\1os.getenv("BENTOML_BASE_URL", \2http://127.0.0.1:3000\3)',
                content,
            )
            # Toute autre forme qui vise le port du service : un défaut
            # d'os.environ.get sous un nom qu'on n'exporte pas (459845 :
            # SERVICE_BASE_URL, repli localhost:3000, 8 tests en connexion
            # refusée), une URL en dur dans une fixture. Le service tourne
            # sur un port éphémère : l'adresse documentée ne répond pas.
            new_content = adresse_du_service.sub(base_url, new_content)
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
                    f"Rewrote service URL in tests for dynamic port: {filepath}"
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
        """Run pytest suite against the running API.

        Ne lève jamais : les appelants rangent ce résultat à côté de
        /login et /predict, déjà obtenus. Une exception ici (469871 :
        UnicodeDecodeError sur un fichier AppleDouble) les faisait perdre, et
        l'évaluation entière sortait en échec critique. La catégorie Tests
        sort « pytest non exécuté », avec la cause, et rien d'autre ne bouge.
        """
        try:
            return self._executer_pytest(base_url)
        except Exception as exc:
            cause = f"{type(exc).__name__}: {exc}"
            self.logger.error(f"pytest non exécuté : {cause}", exc_info=True)
            return {
                "executed": False,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "total": 0,
                "pass_rate": 0,
                "tests_path": None,
                "output_file": None,
                "tests_present": True,
                "evaluable": False,
                "reason": f"pytest non exécuté : {cause}",
            }

    def _executer_pytest(self, base_url: str) -> Dict[str, Any]:
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

        pythons = environnement.pythons_pour_les_tests(self.eval_dir, PYTHON_TESTS)
        command = [
            "uvx",
            "--python",
            pythons[0],
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
        # Le passage réparé garde ce que le README fait installer.
        paquets_readme, fichiers_readme = _installations_du_readme(self.eval_dir)
        for fichier in fichiers_readme:
            if fichier != os.path.normpath(requirements_path):
                command.extend(["--with-requirements", fichier])
        for paquet in paquets_readme:
            if paquet not in _paquets_avec(command):
                command.extend(["--with", paquet])
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
                # Ni .pytest_cache ni __pycache__ déposés dans la copie par
                # notre propre run : la grille compte les caches livrés
                # (« N'envoyez jamais les caches locaux », énoncé 1.6) et
                # pénalisait ceux que le correcteur venait de créer.
                "-p",
                "no:cacheprovider",
                "-o",
                "asyncio_mode=auto",
                *IGNORER_PARASITES_MACOS,
                f"--junit-xml={junit_path}",
            ]
        )

        env = os.environ.copy()
        env["BENTOML_BASE_URL"] = base_url
        env["BENTOML_PORT"] = base_url.rsplit(":", 1)[-1]
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Les apprenants propres lisent une variable d'environnement — mais
        # chacun la sienne. 459404 lisait BASE_URL, on n'exportait que
        # BENTOML_BASE_URL : son repli localhost:3000 partait en connexion
        # refusée et ses 7 tests « échouaient ». Exporter les conventions
        # courantes ne coûte rien et note ce que les tests valent vraiment.
        # Convention d'examen declarable (pi_test_env, #49) : elle s'ajoute à
        # la liste par défaut, elle ne la remplace pas. 462853 : le skill
        # déclarait BASE_URL,API_URL,SERVICE_URL, API_BASE_URL disparaissait
        # et les 20 tests visaient 127.0.0.1:3001.
        noms_env = []
        for n in (NOMS_URL_PAR_DEFAUT + "," + (os.environ.get("EXAM_TEST_ENV") or "")).split(","):
            if n.strip() and n.strip() not in noms_env:
                noms_env.append(n.strip())
        for nom in noms_env:
            env.setdefault(nom, base_url)
        # Le nom que les tests lisent vraiment, en plus de la convention : une
        # convention déclarée remplace la liste par défaut et peut l'omettre
        # (462853 : API_BASE_URL). Affecté, pas setdefault : une valeur
        # laissée dans l'environnement de la machine viserait un autre service.
        noms_url = _noms_d_url_lus_par_les_tests(patched_tests_path)
        for nom in noms_url:
            env[nom] = base_url
        # Les tests importent le code de l'apprenant, qui vit dans le projet.
        env["PYTHONPATH"] = os.pathsep.join(
            [p for p in (projet, env.get("PYTHONPATH")) if p]
        )
        # Le meme secret que le conteneur : des tests qui forgent leur propre
        # jeton (459884 : JWT_SECRET_KEY partage via .env) doivent signer avec
        # la cle que le service verifie.
        secrets_image = self._env_required_by_image()
        # Ce que la copie documente (README, .env d'exemple) et que ses tests
        # lisent : l'exporter, c'est suivre la copie, pas la réparer (462853 :
        # le README exporte API_USERNAME, API_PASSWORD, JWT_SECRET avant
        # `pytest -v`). La valeur du conteneur quand il en a une : il a été
        # lancé avec la valeur documentée. L'adresse reste la nôtre.
        documentees = {}
        for nom, valeur in _variables_documentees(self.eval_dir).items():
            if (nom in noms_url or nom in noms_env or nom.startswith("BENTOML_")
                    or _URL_LOCALE.match(valeur)
                    or not _nomme_dans_les_tests(patched_tests_path, nom)):
                continue
            documentees[nom] = secrets_image.get(nom, valeur)
        # Variables que le README fait générer (469475 : `.env.runtime`) et
        # que les tests lisent : le premier passage les reçoit, avec la
        # valeur donnée au conteneur. Suivre le README n'est pas réparer.
        for nom in sorted(getattr(self, "_env_cite_readme", set())):
            if nom in secrets_image and _nomme_dans_les_tests(patched_tests_path, nom):
                documentees.setdefault(nom, secrets_image[nom])
        self._noms_documentes = sorted(documentees)
        results["variables_documentees"] = self._noms_documentes
        # L'environnement du premier passage, tel que livré : l'adresse du
        # service (port éphémère), ce que la copie documente, rien d'autre.
        env_livre = dict(env)
        env_livre.update(documentees)
        for nom, valeur in secrets_image.items():
            env.setdefault(nom, valeur)
        for nom, valeur in documentees.items():
            env.setdefault(nom, valeur)
        self._valeurs_a_masquer = [str(v) for v in list(secrets_image.values()) + list(documentees.values())
                                   if v and len(str(v)) >= 4]
        self._noms_secrets_image = sorted(secrets_image)

        def lancer(commande, environnement_pytest=None):
            # Un rapport laissé par un passage précédent ne doit jamais être
            # relu comme celui de ce passage.
            if os.path.exists(junit_path):
                os.remove(junit_path)
            return subprocess.run(commande, cwd=projet, capture_output=True, text=True,
                                  env=environnement_pytest or env)

        # « On note ce qui a été livré, on mesure le reste avec la
        # réparation » (responsable, 16/10). La checklist de l'énoncé demande
        # « pytest -v passe » avec la seule archive livrée : premier passage
        # sans rien de notre part, puis les réparations comme avant.
        result, tel_que_livre = self._pytest_tel_que_livre(
            pythons, requirements_path, patched_tests_path, junit_path, env_livre, lancer)
        results["pytest_tel_que_livre"] = {k: v for k, v in tel_que_livre.items() if k != "sortie"}
        if tel_que_livre.get("derive_setuptools"):
            # Adaptation à l'écosystème, hors `reparations[]`.
            results["derive_setuptools"] = tel_que_livre["derive_setuptools"]
        if tel_que_livre["passe"]:
            # Rien à réparer : le second passage n'apprendrait rien et coûte
            # une installation de plus sur la VM.
            results["python"] = tel_que_livre["python"]
            results["pythons_essayes"] = tel_que_livre["pythons_essayes"]
            results["paquets_injectes"] = []
            results["reparations"] = []
        else:
            result = self._pytest_repare(command, pythons, patched_tests_path, projet, junit_path,
                                         env, lancer, results)
            results["paquets_injectes"] = _paquets_avec(command)
            results["reparations"] = self._reparations(
                tel_que_livre, command, requirements_path, patched_tests_path, projet, results)

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

    def _compter_tests(self, sortie: str, junit_path: str):
        """(passed, failed, errors, total) : le rapport JUnit, sinon la prose
        de pytest (qui ment quand la collecte échoue)."""
        compte = self._read_junit_report(junit_path)
        if compte is not None:
            return compte
        passed = sum(int(m) for m in re.findall(r"(\d+)\s+passed", sortie))
        failed = sum(int(m) for m in re.findall(r"(\d+)\s+failed", sortie))
        errors = sum(int(m) for m in re.findall(r"(\d+)\s+error", sortie))
        return passed, failed, errors, passed + failed + errors

    def _pytest_tel_que_livre(self, pythons, requirements_path, tests_path, junit_path, env, lancer):
        """`pytest -v` avec le seul requirements.txt de la copie.

        Aucun paquet injecté, pas d'`asyncio_mode=auto`, pas de code lu dans
        l'image, pas de secret du conteneur que la copie ne documente pas, pas
        de relance. Les variables exportées par son README (ou un .env
        d'exemple) et lues par ses tests sont fournies : c'est suivre la copie.
        Restent à nous : l'adresse du service (réécrite, port éphémère, sous
        chaque nom que les tests lisent), le choix du Python
        (essais successifs, comme le passage réparé) et les options du
        harnais (rapport JUnit, ni cache ni fichiers AppleDouble). uvx fournit
        le lanceur pytest : la checklist fait lancer `pytest -v` au correcteur.

        Rend (résultat du dernier passage, constat) ; le constat dit si le
        passage a réussi, et sinon à qui l'échec est imputable : la copie, ou
        notre outillage (critère non mesuré)."""
        commande = ["uvx", "--python", pythons[0]]
        if os.path.isfile(requirements_path):
            commande += ["--with-requirements", requirements_path]
        # Les `pip install` du README, joués comme il les écrit (466697 :
        # `pip install pytest requests`) : les suivre n'est pas réparer.
        paquets_readme, fichiers_readme = _installations_du_readme(self.eval_dir)
        principal = os.path.normpath(requirements_path)
        for fichier in fichiers_readme:
            if fichier != principal:
                commande += ["--with-requirements", fichier]
        for paquet in paquets_readme:
            commande += ["--with", paquet]
        commande += ["pytest", tests_path, "-v", "--tb=short", "-p", "no:cacheprovider",
                     *IGNORER_PARASITES_MACOS, f"--junit-xml={junit_path}"]
        self.logger.info("pytest tel que livré (requirements de la copie seul)...")
        result = lancer(commande, env)
        essais = [pythons[0]]
        for suivant in pythons[1:]:
            sortie = (result.stdout or "") + (result.stderr or "")
            if (result.returncode in (0, 5) or os.path.exists(junit_path)
                    or not environnement.installation_impossible(sortie)):
                break
            commande[2] = suivant
            essais.append(suivant)
            result = lancer(commande, env)
        sortie = (result.stdout or "") + "\n" + (result.stderr or "")
        # Dérive de l'écosystème, pas faute de la copie (décision du 16/10,
        # même principe que la reconstruction datée) : bentoml importe
        # `pkg_resources`, que setuptools >= 82, publié après le rendu, ne
        # fournit plus. Une relance avec `setuptools<82`, dont le résultat
        # reste celui du passage tel que livré : adaptation, ni réparation
        # ni pénalité.
        derive = None
        if "No module named 'pkg_resources'" in sortie:
            derive = {
                "applique": SETUPTOOLS_AVEC_PKG_RESOURCES,
                "motif": "pkg_resources absent : setuptools >= 82 publié après le rendu (dérive de l'écosystème)",
                "extrait": _extrait(sortie, _MODULE_ABSENT, self._valeurs_a_masquer),
            }
            position = _position_pytest(commande)
            commande[position:position] = ["--with", SETUPTOOLS_AVEC_PKG_RESOURCES]
            self.logger.info("pkg_resources absent au passage tel que livré (dérive de setuptools) : relance avec "
                             + SETUPTOOLS_AVEC_PKG_RESOURCES)
            result = lancer(commande, env)
            sortie = (result.stdout or "") + "\n" + (result.stderr or "")
        rapport = os.path.exists(junit_path)
        passed, failed, errors, total = self._compter_tests(sortie, junit_path)
        passe = result.returncode == 0 and total > 0
        imputable = cause = None
        if not passe:
            imputable, cause = self._imputer_tel_que_livre(
                sortie, result.returncode, rapport, passed, failed, errors, total, essais)
        constat = {
            "passe": passe,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "total": total,
            "code": result.returncode,
            "cause": cause,
            "imputable": imputable,
            "commande": _commande_lisible(commande),
            "python": commande[2],
            "pythons_essayes": essais,
            "variables_documentees": list(getattr(self, "_noms_documentes", [])),
            "paquets_readme": list(paquets_readme),
            "sortie": sortie,
        }
        if derive:
            constat["derive_setuptools"] = derive
        self.record_step(
            "Lancer pytest tel que livré (requirements de la copie seul)",
            command=constat["commande"],
            output=_extrait(sortie, None, self._valeurs_a_masquer),
            exit_code=result.returncode,
            note=("pytest -v passe tel que livré." if passe else
                  f"pytest -v ne passe pas tel que livré ({cause})."
                  + (" Notre outillage en cause : critère non mesuré." if imputable == "outillage" else ""))
                 + (" Variables exportées comme le documente la copie : "
                    + ", ".join(constat["variables_documentees"]) + "."
                    if constat["variables_documentees"] else "")
                 + (" Paquets installés comme le fait le README : "
                    + ", ".join(constat["paquets_readme"]) + "."
                    if constat["paquets_readme"] else "")
                 + (f" Dérive de l'écosystème (pkg_resources retiré de setuptools 82, publié après le rendu) : "
                    f"rejoué avec {SETUPTOOLS_AVEC_PKG_RESOURCES}, sans pénalité." if derive else ""),
        )
        return result, constat

    def _imputer_tel_que_livre(self, sortie, code, rapport, passed, failed, errors, total, essais):
        """("copie" | "outillage", cause) d'un premier passage en échec.
        Jamais un zéro pour notre absence de mesure : réseau, harnais et
        installation impossible pour tous nos Python sont à nous."""
        if not rapport and _RESEAU_UV.search(sortie):
            ligne = _RESEAU_UV.search(sortie).group(0).strip()
            return "outillage", f"index de paquets injoignable (réseau) : {ligne[:200]}"
        if total and passed == 0 and failed + errors == total and _CONNEXION_REFUSEE.search(sortie):
            return "outillage", (f"les {total} tests échouent tous en erreur de connexion : "
                                 "harnais en cause (URL/port), pas la copie")
        illisible = _REQUIREMENTS_ILLISIBLE.search(sortie)
        if illisible:
            return "copie", f"requirements.txt inutilisable : {illisible.group(0).strip()}"
        if not rapport and environnement.installation_impossible(sortie):
            return "outillage", ("dépendances de la copie non installables avec les Python essayés ("
                                 + ", ".join(essais) + ") : imputation incertaine")
        absent = _MODULE_ABSENT.search(sortie)
        if absent:
            return "copie", f"module absent de l'environnement livré : {absent.group(0)}"
        if code == 5:
            return "copie", "pytest ne collecte aucun test (code 5)"
        if total == 0:
            return "copie", f"collecte des tests en échec (code {code})"
        return "copie", f"{failed + errors}/{total} tests en échec (code {code})"

    def _pytest_repare(self, command, pythons, tests_path, projet, junit_path, env, lancer, results):
        """Le passage d'avant la politique du 16/10 : outillage standard,
        `asyncio_mode=auto`, relance sur fichiers explicites, puis les replis
        (requirements illisible, code lu dans l'image)."""
        self.logger.info("Running pytest against running API...")
        result = lancer(command)
        # uv n'a pas pu installer les dépendances de la copie pour ce Python
        # (rien n'a tourné, pas de rapport) : le suivant, consigné.
        essais = [pythons[0]]
        position = command.index("--python") + 1
        for suivant in pythons[1:]:
            sortie = (result.stdout or "") + (result.stderr or "")
            if (result.returncode in (0, 5) or os.path.exists(junit_path)
                    or not environnement.installation_impossible(sortie)):
                break
            self.logger.info(
                f"Dépendances des tests non installables en Python {command[position]} : essai en {suivant}")
            command[position] = suivant
            essais.append(suivant)
            result = lancer(command)
        results["python"] = command[position]
        results["pythons_essayes"] = essais

        if result.returncode == 5:
            # Pytest exit code 5 means nothing collected; retry with explicit python files.
            discovered_files = fichiers_de_tests(tests_path)
            if discovered_files:
                # Le lanceur pytest, pas l'argument de `--with pytest` : le
                # premier « pytest » de la commande est un paquet à installer.
                prefixe = command[: _position_pytest(command)]
                retry_command = prefixe + [
                    "pytest",
                    *discovered_files,
                    "-v",
                    "--tb=short",
                    "-p",
                    "no:cacheprovider",
                    "-o",
                    "asyncio_mode=auto",
                    *IGNORER_PARASITES_MACOS,
                    f"--junit-xml={junit_path}",
                ]
                self.logger.info(
                    "Pytest collected no tests by default pattern; retrying with explicit test files"
                )
                result = lancer(retry_command)
                results["relance_fichiers_explicites"] = True

        return self._replis_pytest(result, command, env, lancer, tests_path, projet,
                                   junit_path, results)

    def _reparations(self, tel_que_livre, command, requirements_path, tests_path, projet, results):
        """Les réparations qui ont servi, chacune avec la preuve du premier
        passage. Seulement celles dont on voit la raison : le feedback leur
        attache des phrases imposées, une entrée de trop serait un reproche
        faux."""
        sortie = tel_que_livre.get("sortie", "")
        masque = self._valeurs_a_masquer
        reparations = []

        def reparation(ident, applique, motif=None):
            reparations.append({
                "id": ident,
                "critere_touche": "taux_pytest",
                "tel_que_livre": {
                    "commande": tel_que_livre["commande"],
                    "code": tel_que_livre["code"],
                    "extrait": _extrait(sortie, motif, masque),
                },
                "applique": applique,
            })

        if results.get("requirements_inutilisable"):
            reparation("requirements_illisible", results.get("environnement_de_repli", ""),
                       _REQUIREMENTS_ILLISIBLE)
        if results.get("code_lu_dans_l_image"):
            reparation("code_lu_dans_l_image", results["code_lu_dans_l_image"], _MODULE_ABSENT)
        if not results.get("requirements_inutilisable"):
            # Seulement les paquets dont l'absence a fait échouer le premier
            # passage : un paquet non déclaré mais tiré par une dépendance
            # (httpx par bentoml) n'a rien coûté à la copie.
            absents = {PAQUET_DU_MODULE.get(m.split(".")[0], m.split(".")[0]).lower()
                       for m in _MODULE_ABSENT.findall(sortie)}
            injectes = {p.lower() for p in _paquets_avec(command)}
            declares = _paquets_declares(requirements_path)
            factice = [a for p in declares for a in ("--with", p)] + ["pytest"]
            non_declares = [p for p in self._paquets_des_imports(tests_path, projet, factice)
                            if p in injectes and p in absents and p != "pytest"]
            if non_declares:
                reparation("paquets_non_declares",
                           "paquets importés par les tests, absents du requirements.txt, installés par le correcteur : "
                           + ", ".join(non_declares), _MODULE_ABSENT)
        if "asyncio_mode=auto" in command and _tests_async(tests_path) and _ASYNC.search(sortie):
            reparation("asyncio_mode_auto",
                       "tests `async def` exécutés par pytest-asyncio (fourni par le correcteur) en mode auto "
                       "(-o asyncio_mode=auto)", _ASYNC)
        # Une variable documentée par la copie a été fournie dès le premier
        # passage : la suivre n'a rien coûté.
        documentes = set(getattr(self, "_noms_documentes", []))
        noms = [n for n in getattr(self, "_noms_secrets_image", [])
                if n not in documentes and _nomme_dans_les_tests(tests_path, n)]
        if noms:
            # Les tests lisent une variable que seul le conteneur définit
            # (459884 : JWT_SECRET_KEY). Le nom, jamais la valeur.
            reparation("secret_du_conteneur",
                       "variable(s) d'environnement du conteneur fournies aux tests par le correcteur : "
                       + ", ".join(noms))
        if results.get("relance_fichiers_explicites"):
            reparation("relance_fichiers_explicites",
                       "aucun test collecté par la découverte par défaut : relance avec les fichiers de tests listés")
        return reparations

    def _replis_pytest(self, result, command, env, lancer, tests_path, projet, junit_path, results):
        """Mesurer les tests de la copie quand leur environnement livré ne
        le permet pas, et le consigner.

        - requirements.txt illisible pour uv (468775) : rien ne s'installe,
          rien n'est collecté. Le fichier cassé est une faute de la copie
          (exposée pour le critère Reproductibilité) ; les tests se lancent
          dans un environnement minimal : l'outillage de base plus les
          imports des tests.
        - les tests importent du code que l'énoncé interdit de livrer
          (466786 : `from src.auth import ...`) : il est lu dans l'image du
          bento et mis sur PYTHONPATH, hors de la copie, supprimé ensuite.

        Les tests tournent comme les autres, avec le même lanceur."""
        def sortie_de(resultat):
            return (resultat.stdout or "") + "\n" + (resultat.stderr or "")

        def rien_de_collecte():
            return (self._read_junit_report(junit_path) or (0, 0, 0, 0))[3] == 0

        erreur = _REQUIREMENTS_ILLISIBLE.search(sortie_de(result))
        if erreur and rien_de_collecte() and "--with-requirements" in command:
            i = command.index("--with-requirements")
            base = command[:i] + command[i + 2:]
            ajouts = self._paquets_des_imports(tests_path, projet, base)
            position = _position_pytest(base)
            repli = base[:position] + [a for p in ajouts for a in ("--with", p)] + base[position:]
            self.logger.warning(
                f"requirements.txt inutilisable ({erreur.group(0).strip()}) : "
                "tests lancés dans un environnement minimal")
            result = lancer(repli)
            if ajouts and rien_de_collecte() and environnement.installation_impossible(sortie_de(result)):
                # Un import des tests ne correspond à aucun paquet installable.
                repli = base
                result = lancer(repli)
            command[:] = repli
            paquets = [repli[j + 1] for j, a in enumerate(repli[:_position_pytest(repli)]) if a == "--with"]
            results["requirements_inutilisable"] = True
            results["requirements_erreur"] = erreur.group(0).strip()
            results["environnement_de_repli"] = (
                "requirements.txt inutilisable : tests lancés dans un environnement minimal ("
                + ", ".join(paquets) + ")")
            self.record_step(
                "Lancer les tests sans le requirements.txt de la copie",
                command=" ".join(repli[:2] + ["..."] + repli[_position_pytest(repli):][:2]),
                output=erreur.group(0).strip(),
                exit_code=result.returncode,
                note=results["environnement_de_repli"]
                + ". Le requirements.txt illisible reste une faute de la copie.",
            )

        env_du_passage = env
        absent = _MODULE_ABSENT.search(sortie_de(result))
        if absent and self.image_name:
            module = absent.group(1).split(".")[0]
            dossier = tempfile.mkdtemp(prefix="scriptorium-code-image-")
            try:
                racine = self._code_de_l_image(module, modules_importes(tests_path).get(module, set()), dossier)
                if racine:
                    env_image = dict(env)
                    env_image["PYTHONPATH"] = os.pathsep.join(
                        [p for p in (projet, racine, env.get("PYTHONPATH")) if p])
                    self.logger.info(
                        f"Les tests importent {module}, absent du rendu : code lu dans l'image ({CODE_DU_BENTO})")
                    result = lancer(command, env_image)
                    env_du_passage = env_image
                    result = self._relancer_sans_pkg_resources(result, command, env_image, lancer, results)
                    results["code_lu_dans_l_image"] = (
                        f"les tests importent du code non livré ({module}) ; lu dans l'image ({CODE_DU_BENTO})")
                    # Ce que ce passage a rendu, pas l'erreur du premier
                    # (464154 : « No module named 'src' » affiché alors que src
                    # s'importait et que pkg_resources manquait).
                    self.record_step(
                        "Lancer les tests avec le code lu dans l'image",
                        command=f"docker cp {self.container_name}:{CODE_DU_BENTO} <temporaire>",
                        output=_extrait(sortie_de(result), _RESUME_OU_ERREUR, self._valeurs_a_masquer),
                        exit_code=result.returncode,
                        note=results["code_lu_dans_l_image"].capitalize()
                        + ". L'énoncé ne fait pas livrer src/ : les tests en dépendent quand même."
                        + (f" pkg_resources absent du setuptools injecté : relancé avec {results['setuptools_borne']}."
                           if results.get("setuptools_borne") else ""),
                    )
            finally:
                shutil.rmtree(dossier, ignore_errors=True)
        else:
            result = self._relancer_sans_pkg_resources(result, command, env_du_passage, lancer, results)
        return result

    def _relancer_sans_pkg_resources(self, result, command, env, lancer, results):
        """bentoml importe `pkg_resources` (via `fs`) ; setuptools ne le
        fournit plus depuis la version 82, et le `setuptools` que nous
        injectons sans borne résout à la dernière (464154 : 84). Notre
        environnement, pas la copie : relance avec `setuptools<82`."""
        sortie = (result.stdout or "") + "\n" + (result.stderr or "")
        if "No module named 'pkg_resources'" not in sortie or "setuptools" not in command:
            return result
        i = command.index("setuptools")
        if i == 0 or command[i - 1] != "--with":
            return result
        command[i] = SETUPTOOLS_AVEC_PKG_RESOURCES
        self.logger.info("pkg_resources absent du setuptools injecté : relance avec "
                         + SETUPTOOLS_AVEC_PKG_RESOURCES)
        results["setuptools_borne"] = SETUPTOOLS_AVEC_PKG_RESOURCES
        return lancer(command, env)

    def _paquets_des_imports(self, tests_path: str, projet: str, commande) -> list:
        """Paquets à installer pour les imports des tests : ni la
        bibliothèque standard, ni les modules locaux, ni ce que la commande
        installe déjà."""
        deja = {commande[i + 1].lower() for i, a in enumerate(commande[:-1]) if a == "--with"}
        locaux = {"src", "tests", "conftest", "__future__"}
        for dossier in (tests_path, projet):
            try:
                locaux.update(os.path.splitext(nom)[0] for nom in os.listdir(dossier))
            except OSError:
                pass
        paquets = []
        for module in sorted(modules_importes(tests_path)):
            if module in sys.stdlib_module_names or module in locaux:
                continue
            paquet = PAQUET_DU_MODULE.get(module, module).lower()
            if paquet not in deja:
                deja.add(paquet)
                paquets.append(paquet)
        return paquets

    def _code_de_l_image(self, module: str, sous_modules: set, dossier: str) -> Optional[str]:
        """Copier le bento de l'image (sans rien y exécuter) et rendre le
        dossier à mettre sur PYTHONPATH pour que `module` s'importe, ou None.

        `docker cp` lit le conteneur qui tourne déjà sous `container_name`
        (les deux modes le nomment ainsi). Deux agencements de bento :
        `bento/src/src/auth.py` (include de `src/`) et `bento/src/auth.py`."""
        destination = os.path.join(dossier, os.path.basename(CODE_DU_BENTO))
        try:
            copie = subprocess.run(
                ["docker", "cp", f"{self.container_name}:{CODE_DU_BENTO}", destination],
                capture_output=True, text=True, timeout=120)
        except Exception as exc:
            self.logger.debug(f"Code de l'image illisible : {exc}")
            return None
        if copie.returncode != 0 or not os.path.isdir(destination):
            return None
        candidats = []
        for racine, dossiers, _ in os.walk(dossier):
            profondeur = os.path.relpath(racine, dossier).count(os.sep)
            if profondeur > 4:
                dossiers[:] = []
                continue
            paquet = os.path.join(racine, module)
            if os.path.isdir(paquet):
                if not sous_modules or any(os.path.isfile(os.path.join(paquet, f"{s}.py"))
                                           or os.path.isdir(os.path.join(paquet, s)) for s in sous_modules):
                    candidats.append((profondeur, racine))
            elif not sous_modules and os.path.isfile(paquet + ".py"):
                candidats.append((profondeur, racine))
        return min(candidats)[1] if candidats else None

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
        # Une variable que le README fait fournir et que nous n'avons pas
        # fournie : le service a raison de refuser de démarrer.
        if self._variables_documentees_absentes(logs):
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

    def _variables_documentees_absentes(self, logs: str) -> list:
        """Noms de variables (avec `_`) cités à la fois par les logs du
        conteneur et par le README, que nous n'avons pas fournis."""
        # Seules les lignes d'erreur comptent : une variable citée dans un log
        # ordinaire ne dit pas qu'elle manque.
        erreurs = "\n".join(l for l in (logs or "").splitlines()
                            if re.search(r"error|exception|missing|required|not set|must be|\bset\b",
                                         l, re.IGNORECASE))
        noms = set(re.findall(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]*_[A-Z0-9_]+)(?![A-Za-z0-9_])", erreurs))
        if not noms:
            return []
        fournis = set(getattr(self, "_container_env", None) or {})
        try:
            documentation = "\n".join(
                texte for chemin, texte in self._iter_submission_texts()
                if os.path.basename(chemin).lower().startswith("readme"))
        except Exception:
            return []
        return sorted(nom for nom in noms - fournis
                      if not nom.startswith("BENTOML_")
                      and re.search(rf"(?<![A-Za-z0-9_]){re.escape(nom)}(?![A-Za-z0-9_])", documentation))

    def _cause_de_la_faute(self, faute: str, diag: Dict[str, Any], logs: str) -> str:
        """La cause, en clair, qui accompagne `fault`."""
        absentes = self._variables_documentees_absentes(logs)
        if faute == "systeme" and absentes:
            return ("variables d'environnement exigées par le README non fournies au conteneur : "
                    + ", ".join(absentes))
        code = diag.get("container_exit_code")
        derniere = next((l.strip() for l in reversed((logs or "").splitlines()) if l.strip()), "")
        derniere = _expurger(derniere, self._valeurs_a_masquer_conteneur())[:300]
        if faute == "apprenant":
            return f"le service de l'image s'arrête au démarrage (code {code}) : {derniere}"
        if faute == "systeme":
            return "l'environnement de correction n'a pas pu exécuter l'image"
        return "cause non établie"

    def _valeurs_a_masquer_conteneur(self) -> list:
        return [str(v) for v in (getattr(self, "_container_env", None) or {}).values()
                if v and len(str(v)) >= 4]

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

        # Le marqueur peut coller à la dernière ligne d'un fichier sans saut
        # de ligne final (462113) : découpage sur le marqueur, pas par ligne.
        morceaux = re.split(r"===FILE:([^\n]*)\n?", result.stdout or "")
        texts = [(f"{chemin} (dans l'image)", contenu.rstrip("\n"))
                 for chemin, contenu in zip(morceaux[1::2], morceaux[2::2])]
        return texts

    def _env_required_by_image(self) -> Dict[str, str]:
        """Variables d'environnement que le service exige pour demarrer.

        459884 : service.py fait os.environ["JWT_SECRET_KEY"] a l'import —
        lance sans la variable, le conteneur meurt avant d'ouvrir le port et
        une copie qui fonctionne est notee muette. Le nom se lit dans le code
        du service (deja extrait de l'image), la valeur dans le rendu
        (README/.env d'exemple), sinon une valeur aleatoire : le service veut
        une cle, pas une cle particuliere.
        """
        if getattr(self, "_container_env", None) is not None:
            return self._container_env

        requis = set()
        lues_sans_defaut = set()
        for _, texte in self._read_service_from_image():
            requis.update(re.findall(
                r'os\.environ\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*\]', texte))
            lues_sans_defaut.update(_lectures_env_sans_defaut(texte))
        # 469475 : `os.environ.get('API_USERNAME', '')` puis RuntimeError au
        # démarrage, et un README qui fait générer ces valeurs (`--env-file`).
        # Lecture sans défaut (ou défaut vide) ET nom cité par la
        # documentation de la copie : la fournir, c'est suivre le README.
        if lues_sans_defaut - requis:
            documentation = "\n".join(
                texte for chemin, texte in self._iter_submission_texts()
                if os.path.basename(chemin).lower().startswith("readme"))
            cites = {nom for nom in lues_sans_defaut - requis
                     if re.search(rf"(?<![A-Za-z0-9_]){re.escape(nom)}(?![A-Za-z0-9_])", documentation)}
            self._env_cite_readme = cites
            requis.update(cites)

        env: Dict[str, str] = {}
        if requis:
            declares: Dict[str, str] = {}
            for _, texte in self._iter_submission_texts():
                for nom, valeur in re.findall(
                        r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*([^\s#]+)\s*$',
                        texte, re.M):
                    valeur = valeur.strip('"\'')
                    # « JWT_SECRET_KEY=<YOUR_GENERATED_SECRET> » est une
                    # consigne, pas une valeur.
                    if any(c in valeur for c in "<>$"):
                        continue
                    declares.setdefault(nom, valeur)
            import secrets
            generees = set()
            for nom in sorted(requis):
                env[nom] = declares.get(nom) or secrets.token_hex(32)
                if not declares.get(nom):
                    generees.add(nom)
            self._env_genere = generees
            self.logger.info(
                "Variables exigees par le service : " + ", ".join(sorted(requis)))

        self._container_env = env
        return env

    def _champs_identifiants(self) -> Dict[str, Any]:
        """credentials_source, credentials_in_tests et identifiants_dans_la_copie
        pour un résultat où /login n'a pas été appelé (image non chargée,
        conteneur mort) : lecture statique du rendu, sans l'image (460660)."""
        if not self.credentials_source:
            try:
                textes = sorted(self._iter_submission_texts(), key=lambda t: looks_like_test_file(t[0]))
                trouve = find_credentials_in_texts(textes)
            except Exception:
                trouve = None
            if trouve:
                self.credentials_source, self.credentials_in_tests = trouve["source"], trouve["in_tests"]
                self._paire_statique = {"username": trouve["username"], "password": trouve["password"]}
        paire = getattr(self, "_paire_statique", None)
        return {
            "credentials_source": self.credentials_source,
            "credentials_in_tests": self.credentials_in_tests,
            "identifiants_dans_la_copie": self._identifiants_dans_la_copie(paire) if paire else None,
        }

    def _valeurs_generees(self) -> set:
        """Valeurs que nous avons tirées au hasard pour le conteneur (aucune
        valeur documentée) : elles ne figurent pas dans la copie."""
        env = getattr(self, "_container_env", None) or {}
        return {env[nom] for nom in getattr(self, "_env_genere", set()) if nom in env}

    def _completer_environnement_image(self, platform: Optional[str], attrs: dict) -> None:
        """L'image réellement exécutée, son architecture, et les limites du
        conteneur lues dans son HostConfig."""
        from .environnement import limites
        arch = self._image_arch()
        execution = f"exécutée sous émulation {platform}" if platform else "exécutée en natif"
        self.completer_environnement([
            f"image de l'apprenant : {self.image_name} ({arch or 'architecture non lue'}), {execution}",
            f"limites du conteneur : {limites(attrs)}",
        ])

    def _attrs_conteneur_cli(self) -> dict:
        try:
            import docker
            return docker.from_env().containers.get(self.cli_container_id).attrs
        except Exception as exc:
            self.logger.debug(f"Conteneur illisible : {exc}")
            return {}

    def _image_arch(self) -> Optional[str]:
        """Architecture pour laquelle l'image a ete construite, si lisible."""
        import docker

        try:
            client = docker.from_env()
            return client.images.get(self.image_name).attrs.get("Architecture")
        except Exception as exc:
            self.logger.debug(f"Architecture de l'image illisible : {exc}")
            return None

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
                       if d not in {".git", ".venv", "venv", "__pycache__", ".bentoml_home",
                                    DOSSIER_CONSTRUCTION}]
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
            dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__", "node_modules",
                                                    DOSSIER_CONSTRUCTION}]
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

        # Construire sur une copie du rendu : prepare_data, train_model et
        # `bentoml build` écrivent (data/, models/, __pycache__, .bento), et
        # ce qui atterrissait dans la copie lui était reproché ensuite. La
        # copie vit dans le rendu (dossier caché) : un moteur conteneurisé
        # monte ce chemin-là chez le démon Docker, pas un /tmp à lui.
        copie = os.path.join(self.eval_dir, DOSSIER_CONSTRUCTION)
        shutil.rmtree(copie, ignore_errors=True)
        shutil.copytree(source_root, copie, symlinks=True, ignore=shutil.ignore_patterns(
            DOSSIER_CONSTRUCTION, ".git", ".venv", "venv", "__pycache__", "node_modules", "*.tar"))
        self._dossier_construction = copie
        source_root = copie

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
            ["docker", "run", "--rm", "-e", "PYTHONDONTWRITEBYTECODE=1",
             "-v", f"{source_root}:/src", image, "sh", "-c", script],
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

    def _ranger_construction(self) -> None:
        """Supprimer la copie de travail de la construction depuis la source."""
        dossier = getattr(self, "_dossier_construction", None)
        if dossier:
            shutil.rmtree(dossier, ignore_errors=True)
            self._dossier_construction = None

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
