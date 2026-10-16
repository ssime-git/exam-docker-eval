"""`pytest -v` d'abord tel que livré, puis nos réparations, chacune consignée.

    uv run --with pytest pytest -q test_pytest_tel_que_livre.py

Politique du responsable (16/10) : « On note ce qui a été livré, on mesure le
reste avec la réparation. » Le premier passage n'utilise que le
requirements.txt de la copie : aucun paquet injecté, pas de `src` lu dans
l'image, pas d'`asyncio_mode=auto`, pas de secret du conteneur. Seules
l'adresse du service (port éphémère) et le choix du Python restent à nous.
Le barème lit `pytest_tel_que_livre` et `reparations[]` pour le critère
« taux de réussite pytest ».

466786 : conftest `from src.auth import ...`, src/ non livré (énoncé 1.6) :
tel que livré, ImportError à la collecte (code 4) ; 12/12 après lecture du
code dans l'image.
468775 : requirements.txt illisible pour uv (code 2, rien ne tourne) ; 8/8
dans l'environnement minimal.
"""
import logging
import os
import sys
import types

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner  # noqa: E402
from docker_eval.bentoml_runner import BentoMLRunner  # noqa: E402

ERREUR_UV_468775 = ("error: Unexpected '·', expected '-c', '-e', '-r' or the start of a requirement: "
                    "requirements.txt:1:2\n")
CONFTEST_466786 = """import os
import pytest
import requests
import jwt

from src.auth import create_jwt_token

BASE_URL = "http://localhost:3000"
"""
IMPORT_ERROR_466786 = ("ImportError while loading conftest '/w/tests/conftest.py'.\n"
                       "tests/conftest.py:6: in <module>\n    from src.auth import create_jwt_token\n"
                       "E   ModuleNotFoundError: No module named 'src'\n")


def ecrire(chemin, contenu=""):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


class _Resultat:
    def __init__(self, code, sortie=""):
        self.returncode, self.stdout, self.stderr = code, sortie, ""


def junit(commande, total, echecs=0):
    chemin = next(a.split("=", 1)[1] for a in commande if a.startswith("--junit-xml="))
    ecrire(chemin, f'<testsuite tests="{total}" failures="{echecs}" errors="0" skipped="0"></testsuite>')


def tel_que_livre(commande) -> bool:
    """La commande du premier passage : rien d'autre que le requirements de
    la copie avant le lanceur pytest."""
    avant = commande[:bentoml_runner._position_pytest(commande)]
    return "--with" not in avant


def avec_uvx(monkeypatch):
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")


def test_466786_tel_que_livre_echoue_sur_src_puis_repare(tmp_path, monkeypatch):
    ecrire(tmp_path / "requirements.txt", "bentoml\npyjwt\nrequests\npytest\n")
    ecrire(tmp_path / "tests" / "__init__.py", "")
    ecrire(tmp_path / "tests" / "conftest.py", CONFTEST_466786)
    ecrire(tmp_path / "tests" / "test_auth.py", "def test_a():\n    assert True\n")
    runner = BentoMLRunner("466786", str(tmp_path), 300, logging.getLogger("t"))
    runner.image_name = "eval_466786/admission_prediction_service:latest"
    runner._env_required_by_image = lambda: {"JWT_SECRET_KEY": "secret-du-conteneur"}
    avec_uvx(monkeypatch)
    passages = []

    def faux_run(commande, **kwargs):
        if commande[:2] == ["docker", "cp"]:
            dest = commande[3]
            ecrire(os.path.join(dest, "src", "auth.py"), "def create_jwt_token():\n    return 't'\n")
            return _Resultat(0, "")
        if commande[0] == "docker":
            return _Resultat(0, "")
        env = kwargs.get("env") or {}
        passages.append((list(commande), dict(env)))
        chemins = env.get("PYTHONPATH", "").split(os.pathsep)
        if not any(c and os.path.isfile(os.path.join(c, "src", "auth.py")) for c in chemins):
            return _Resultat(4, IMPORT_ERROR_466786)
        junit(commande, 12)
        return _Resultat(0, "12 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)

    resultat = runner._run_pytest("http://localhost:32768")

    premier, env_premier = passages[0]
    assert tel_que_livre(premier), premier
    assert "--with-requirements" in premier
    assert "asyncio_mode=auto" not in premier
    assert "JWT_SECRET_KEY" not in env_premier or env_premier["JWT_SECRET_KEY"] != "secret-du-conteneur"
    # Les réparations tournent ensuite, comme avant.
    assert resultat["passed"] == 12 and resultat["total"] == 12, resultat
    assert "code_lu_dans_l_image" in resultat

    tql = resultat["pytest_tel_que_livre"]
    assert tql["passed"] == 0 and tql["total"] == 0 and tql["code"] == 4, tql
    assert tql["passe"] is False and tql["imputable"] == "copie", tql
    assert "src" in tql["cause"], tql

    reparations = {r["id"]: r for r in resultat["reparations"]}
    assert "code_lu_dans_l_image" in reparations, resultat["reparations"]
    src = reparations["code_lu_dans_l_image"]
    assert src["critere_touche"] == "taux_pytest"
    assert src["tel_que_livre"]["code"] == 4
    assert "No module named 'src'" in src["tel_que_livre"]["extrait"]
    assert src["tel_que_livre"]["commande"].startswith("uvx")
    assert "/home/bentoml/bento/src" in src["applique"]
    # Pas de test async : aucune réparation asyncio inventée. requests et
    # jwt sont déclarés : aucun paquet non déclaré.
    assert "asyncio_mode_auto" not in reparations
    assert "paquets_non_declares" not in reparations
    # Le fait brut : ce que la commande de réparation a injecté.
    assert {"pytest", "requests", "pyjwt", "httpx"} <= set(resultat["paquets_injectes"])


def test_468775_requirements_illisible_tel_que_livre_puis_repli(tmp_path, monkeypatch):
    racine = tmp_path / "examen_bentoml_ready_for_submition"
    ecrire(racine / "requirements.txt", "1·bentoml==1.4.35\n")
    ecrire(racine / "tests" / "test_api.py",
           "import os\nimport requests\nimport jwt\n\ndef test_a():\n    assert True\n")
    runner = BentoMLRunner("468775", str(tmp_path), 300, logging.getLogger("t"))
    avec_uvx(monkeypatch)
    commandes = []

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        commandes.append(list(commande))
        if "--with-requirements" in commande:
            return _Resultat(2, ERREUR_UV_468775)
        junit(commande, 8)
        return _Resultat(0, "8 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)

    resultat = runner._run_pytest("http://localhost:32770")
    assert tel_que_livre(commandes[0]) and "--with-requirements" in commandes[0]
    assert resultat["passed"] == 8 and resultat["requirements_inutilisable"] is True
    tql = resultat["pytest_tel_que_livre"]
    assert tql["code"] == 2 and tql["total"] == 0 and tql["passe"] is False, tql
    assert tql["imputable"] == "copie", tql
    assert "requirements.txt:1:2" in tql["cause"], tql
    reparations = {r["id"]: r for r in resultat["reparations"]}
    assert "requirements_illisible" in reparations, resultat["reparations"]
    assert "requirements.txt:1:2" in reparations["requirements_illisible"]["tel_que_livre"]["extrait"]
    assert reparations["requirements_illisible"]["critere_touche"] == "taux_pytest"


def test_tel_que_livre_vert_aucune_reparation_ni_second_passage(tmp_path, monkeypatch):
    ecrire(tmp_path / "requirements.txt", "requests==2.32.5\npytest==8.3.3\n")
    ecrire(tmp_path / "tests" / "test_api.py", "import requests\n\ndef test_ok():\n    assert True\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    avec_uvx(monkeypatch)
    uvx = []

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        uvx.append(list(commande))
        junit(commande, 3)
        return _Resultat(0, "3 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)

    resultat = runner._run_pytest("http://localhost:32771")
    assert len(uvx) == 1 and tel_que_livre(uvx[0]), uvx
    assert resultat["passed"] == 3 and resultat["total"] == 3 and resultat["evaluable"] is True
    tql = resultat["pytest_tel_que_livre"]
    assert tql["passe"] is True and tql["passed"] == 3 and tql["code"] == 0 and tql["imputable"] is None
    assert resultat["reparations"] == [] and resultat["paquets_injectes"] == []


def _runner_deux_passages(tmp_path, monkeypatch, requirements, test, sortie_livree, code_livre=1):
    ecrire(tmp_path / "requirements.txt", requirements)
    ecrire(tmp_path / "tests" / "test_api.py", test)
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    avec_uvx(monkeypatch)

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        if tel_que_livre(commande):
            if code_livre == 1:
                junit(commande, 2, echecs=1)
            return _Resultat(code_livre, sortie_livree)
        junit(commande, 2)
        return _Resultat(0, "2 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    return runner


def test_paquet_non_declare_dont_l_absence_a_fait_echouer(tmp_path, monkeypatch):
    runner = _runner_deux_passages(
        tmp_path, monkeypatch, "requests==2.32.5\n",
        "import httpx\nimport requests\n\ndef test_a():\n    assert True\n",
        "E   ModuleNotFoundError: No module named 'httpx'\n", code_livre=4)
    resultat = runner._run_pytest("http://localhost:32772")
    reparations = {r["id"]: r for r in resultat["reparations"]}
    assert set(reparations) == {"paquets_non_declares"}, resultat["reparations"]
    assert "httpx" in reparations["paquets_non_declares"]["applique"]
    # requests est déclaré : pas reproché.
    assert "requests" not in reparations["paquets_non_declares"]["applique"]
    assert "No module named 'httpx'" in reparations["paquets_non_declares"]["tel_que_livre"]["extrait"]
    assert resultat["pytest_tel_que_livre"]["imputable"] == "copie"


def test_paquet_non_declare_mais_present_aucune_reparation(tmp_path, monkeypatch):
    """httpx importé, non déclaré, mais tiré par une dépendance déclarée :
    le premier passage échoue sur une assertion, pas sur httpx."""
    runner = _runner_deux_passages(
        tmp_path, monkeypatch, "bentoml==1.4.35\n",
        "import httpx\n\ndef test_a():\n    assert False\n",
        "E   AssertionError: assert False\n1 failed, 1 passed")
    resultat = runner._run_pytest("http://localhost:32772")
    assert resultat["reparations"] == [], resultat["reparations"]


def test_asyncio_seulement_sur_le_signal_de_pytest(tmp_path, monkeypatch):
    runner = _runner_deux_passages(
        tmp_path, monkeypatch, "requests==2.32.5\n",
        "async def test_async():\n    assert True\n\ndef test_b():\n    assert False\n",
        "PytestUnhandledCoroutineWarning: async def functions are not natively supported.\n1 failed, 1 skipped")
    resultat = runner._run_pytest("http://localhost:32772")
    reparations = {r["id"]: r for r in resultat["reparations"]}
    assert set(reparations) == {"asyncio_mode_auto"}, resultat["reparations"]
    assert "not natively supported" in reparations["asyncio_mode_auto"]["tel_que_livre"]["extrait"]


def test_tests_async_sans_signal_aucune_reparation(tmp_path, monkeypatch):
    """La copie déclare et configure pytest-asyncio : ses tests async
    tournent tel que livré, l'échec vient d'ailleurs."""
    runner = _runner_deux_passages(
        tmp_path, monkeypatch, "pytest-asyncio==0.24.0\n",
        "async def test_async():\n    assert False\n",
        "plugins: asyncio-0.24.0\nE   AssertionError\n1 failed, 1 passed")
    resultat = runner._run_pytest("http://localhost:32772")
    assert resultat["reparations"] == [], resultat["reparations"]


def test_secret_du_conteneur_nomme_sans_sa_valeur(tmp_path, monkeypatch):
    """459884 : les tests signent leur jeton avec JWT_SECRET_KEY, que seul le
    conteneur définit. Tel que livré, KeyError ; réparé, le secret est fourni."""
    runner = _runner_deux_passages(
        tmp_path, monkeypatch, "requests==2.32.5\npyjwt==2.10.1\n",
        "import os\nSECRET = os.environ['JWT_SECRET_KEY']\n\ndef test_a():\n    assert True\n",
        "E   KeyError: 'JWT_SECRET_KEY'\n", code_livre=2)
    runner._env_required_by_image = lambda: {"JWT_SECRET_KEY": "valeur-secrete-du-conteneur"}
    resultat = runner._run_pytest("http://localhost:32772")
    reparations = {r["id"]: r for r in resultat["reparations"]}
    assert set(reparations) == {"secret_du_conteneur"}, resultat["reparations"]
    assert "JWT_SECRET_KEY" in reparations["secret_du_conteneur"]["applique"]
    assert "valeur-secrete-du-conteneur" not in str(resultat)


def test_tel_que_livre_bloque_par_notre_outillage(tmp_path, monkeypatch):
    """L'index de paquets injoignable : le premier passage n'a rien mesuré.
    Ce n'est pas un échec de la copie, le barème ne doit rien retirer."""
    ecrire(tmp_path / "requirements.txt", "requests==2.32.5\n")
    ecrire(tmp_path / "tests" / "test_api.py", "def test_ok():\n    assert True\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    avec_uvx(monkeypatch)

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        if tel_que_livre(commande):
            return _Resultat(2, "error: Failed to fetch: `https://pypi.org/simple/requests/`\n"
                                "  Caused by: error sending request for url (https://pypi.org/simple/requests/)\n")
        junit(commande, 1)
        return _Resultat(0, "1 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)

    resultat = runner._run_pytest("http://localhost:32773")
    tql = resultat["pytest_tel_que_livre"]
    assert tql["imputable"] == "outillage" and tql["passe"] is False, tql
    assert "réseau" in tql["cause"] or "index" in tql["cause"], tql


def test_tel_que_livre_connexion_refusee_partout_est_notre_harnais(tmp_path, monkeypatch):
    ecrire(tmp_path / "requirements.txt", "requests==2.32.5\n")
    ecrire(tmp_path / "tests" / "test_api.py", "def test_ok():\n    assert True\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    avec_uvx(monkeypatch)

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        if tel_que_livre(commande):
            junit(commande, 2, echecs=2)
            return _Resultat(1, "E   requests.exceptions.ConnectionError: Connection refused\n2 failed")
        junit(commande, 2)
        return _Resultat(0, "2 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    resultat = runner._run_pytest("http://localhost:32774")
    assert resultat["pytest_tel_que_livre"]["imputable"] == "outillage", resultat["pytest_tel_que_livre"]


def test_echec_reel_des_tests_impute_a_la_copie_et_extrait_sans_jeton(tmp_path, monkeypatch):
    ecrire(tmp_path / "requirements.txt", "requests==2.32.5\n")
    ecrire(tmp_path / "tests" / "test_api.py", "def test_ko():\n    assert False\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    avec_uvx(monkeypatch)

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        junit(commande, 2, echecs=1)
        return _Resultat(1, "E   AssertionError: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.c2lnbmF0dXJl\n"
                            "1 failed, 1 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    resultat = runner._run_pytest("http://localhost:32775")
    tql = resultat["pytest_tel_que_livre"]
    assert tql["passed"] == 1 and tql["total"] == 2 and tql["imputable"] == "copie", tql
    assert "eyJhbGci" not in str(resultat["pytest_tel_que_livre"]) + str(resultat["reparations"])


# --- B4 : l'enveloppe du corps /predict qui a obtenu le 200 ------------------

class _Reponse:
    def __init__(self, code, donnee=None):
        self.status_code, self._donnee = code, donnee
        self.cookies, self.headers = {}, {}

    def json(self):
        return self._donnee


JETON = "eyJhbGciOiJIUzI1NiJ9.e30.signature-valide"
CORPS_A_PLAT = {"gre_score": 337, "toefl_score": 118, "university_rating": 4, "sop": 4.5,
                "lor": 4.5, "cgpa": 9.65, "research": 1}
README_A_PLAT = ("curl -X POST http://localhost:3000/login -d '{\"username\": \"user123\", "
                 "\"password\": \"password123\"}'\n\n"
                 "curl -X POST http://localhost:3000/predict -H 'Authorization: Bearer $T' "
                 "-d '" + __import__("json").dumps(CORPS_A_PLAT) + "'\n")


def brancher_service(monkeypatch, accepte):
    def post(url, json=None, headers=None, **_):
        if url.endswith("/login"):
            if (json or {}).get("username") == "user123" and (json or {}).get("password") == "password123":
                return _Reponse(200, {"token": JETON})
            return _Reponse(401, {})
        if (headers or {}).get("Authorization") != f"Bearer {JETON}":
            return _Reponse(401, {})
        return _Reponse(200, {"prediction": 0.72}) if accepte(json) else _Reponse(400, {"error": "validation"})

    def request(methode, url, json=None, headers=None, **_):
        return post(url, json=json, headers=headers)
    monkeypatch.setattr(bentoml_runner, "requests", types.SimpleNamespace(
        post=post, request=request, exceptions=requests.exceptions))


def enveloppe_input_data(corps):
    return isinstance(corps, dict) and set(corps) == {"input_data"} and corps["input_data"].get("gre_score") == 337


def test_corps_du_readme_qui_ne_passe_qu_avec_une_enveloppe_devinee(tmp_path, monkeypatch):
    ecrire(tmp_path / "README.md", README_A_PLAT)
    ecrire(tmp_path / "tests" / "test_api.py", "def test_x():\n    assert True\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    runner._read_service_from_image = lambda: []
    brancher_service(monkeypatch, enveloppe_input_data)
    resultats = runner._run_api_tests("http://localhost:32770")
    assert resultats["predict_passed"], resultats
    assert resultats["corps_enveloppe"] == "input_data", resultats
    assert resultats["corps_source"] == "devine", resultats


def test_enveloppe_lue_dans_la_signature_de_l_image(tmp_path, monkeypatch):
    ecrire(tmp_path / "README.md", README_A_PLAT)
    ecrire(tmp_path / "tests" / "test_api.py", "def test_x():\n    assert True\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    service = ("import bentoml\nclass Entree(BaseModel):\n    gre_score: int\n\n"
               "@bentoml.service\nclass S:\n    @bentoml.api\n"
               "    def predict(self, input_data: Entree, ctx: bentoml.Context) -> dict:\n        ...\n")
    runner._read_service_from_image = lambda: [("/home/bentoml/bento/src/service.py (dans l'image)", service)]
    brancher_service(monkeypatch, enveloppe_input_data)
    resultats = runner._run_api_tests("http://localhost:32770")
    assert resultats["predict_passed"], resultats
    assert resultats["corps_enveloppe"] == "input_data", resultats
    assert resultats["corps_source"] == "image", resultats


def test_corps_du_readme_accepte_tel_quel_reste_readme(tmp_path, monkeypatch):
    ecrire(tmp_path / "README.md", README_A_PLAT)
    ecrire(tmp_path / "tests" / "test_api.py", "def test_x():\n    assert True\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    runner._read_service_from_image = lambda: []
    brancher_service(monkeypatch, lambda corps: corps == CORPS_A_PLAT)
    resultats = runner._run_api_tests("http://localhost:32770")
    assert resultats["predict_passed"], resultats
    assert resultats["corps_enveloppe"] is None, resultats
    assert resultats["corps_source"] == "readme", resultats


def test_468775_corps_deja_enveloppe_au_readme_reste_readme_apres_un_400(tmp_path, monkeypatch):
    """Forme réelle de 468775 : le README porte déjà `input_data`, la
    signature lue dans l'image aussi. Premier essai doublement enveloppé
    (400), puis le corps du README tel quel (200) : [400, 200]."""
    corps = {"input_data": CORPS_A_PLAT}
    readme = ("curl -X POST http://localhost:3000/login -d '{\"username\": \"user123\", "
              "\"password\": \"password123\"}'\n\n"
              "POST /predict, header Authorization: Bearer <token>, body: "
              + __import__("json").dumps(corps) + " returns {\"prediction\": 0.xx}\n")
    ecrire(tmp_path / "README.md", readme)
    ecrire(tmp_path / "tests" / "test_api.py", "def test_x():\n    assert True\n")
    runner = BentoMLRunner("468775", str(tmp_path), 300, logging.getLogger("t"))
    service = ("class Entree(BaseModel):\n    gre_score: int\n\n"
               "    def predict(self, input_data: Entree) -> dict:\n        ...\n")
    runner._read_service_from_image = lambda: [("/home/bentoml/bento/src/service.py (dans l'image)", service)]
    brancher_service(monkeypatch, enveloppe_input_data)
    resultats = runner._run_api_tests("http://localhost:32770")
    assert resultats["predict_passed"], resultats
    assert resultats["predict_attempt_statuses"][:2] == [400, 200], resultats
    assert resultats["corps_enveloppe"] is None, resultats
    assert resultats["corps_source"] == "readme", resultats


# --- 464154 : code lu dans l'image, puis `pkg_resources` absent ---------------

ERREUR_PKG_RESOURCES = (
    "/tmp/scriptorium-code-image-x/src/src/service.py:6: in <module>\n    import bentoml\n"
    ".../site-packages/fs/__init__.py:4: in <module>\n"
    "    __import__(\"pkg_resources\").declare_namespace(__name__)\n"
    "E   ModuleNotFoundError: No module named 'pkg_resources'\n")


def test_464154_code_de_l_image_puis_pkg_resources_absent(tmp_path, monkeypatch):
    """464154 : tel que livré, `No module named 'src'` (faute de la copie).
    Avec le code lu dans l'image, `src` s'importe, mais bentoml (via `fs`)
    importe `pkg_resources`, que le `setuptools` injecté sans borne (84) ne
    fournit plus. L'étape affichait encore « No module named 'src' ».
    Le correcteur relance avec un setuptools qui fournit `pkg_resources`."""
    ecrire(tmp_path / "requirements.txt", "bentoml\npyjwt\nrequests\npytest\n")
    ecrire(tmp_path / "tests" / "test_service.py",
           "from src.service import app, create_token\n\ndef test_a():\n    assert True\n")
    runner = BentoMLRunner("464154", str(tmp_path), 300, logging.getLogger("t"))
    runner.image_name = "eval_464154/admission_prediction_service:latest"
    runner._env_required_by_image = lambda: {}
    avec_uvx(monkeypatch)
    passages = []

    def faux_run(commande, **kwargs):
        if commande[:2] == ["docker", "cp"]:
            ecrire(os.path.join(commande[3], "src", "service.py"), "app = 1\ncreate_token = 2\n")
            return _Resultat(0, "")
        if commande[0] == "docker":
            return _Resultat(0, "")
        env = kwargs.get("env") or {}
        passages.append(list(commande))
        chemins = env.get("PYTHONPATH", "").split(os.pathsep)
        if not any(c and os.path.isfile(os.path.join(c, "src", "service.py")) for c in chemins):
            return _Resultat(2, "E   ModuleNotFoundError: No module named 'src'\n")
        if "setuptools<82" not in commande:
            return _Resultat(2, ERREUR_PKG_RESOURCES)
        junit(commande, 7)
        return _Resultat(0, "=== 7 passed, 20 warnings in 4.39s ===\n"
                            "The event loop scope for asynchronous fixtures will default to ...\n")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)

    resultat = runner._run_pytest("http://localhost:32770")

    assert resultat["passed"] == 7 and resultat["total"] == 7, resultat
    assert resultat["pytest_tel_que_livre"]["imputable"] == "copie"
    etape = next(s for s in runner.steps if s["title"] == "Lancer les tests avec le code lu dans l'image")
    assert "No module named 'src'" not in etape["output"], etape
    assert etape["output"] == "=== 7 passed, 20 warnings in 4.39s ===", etape
    assert "setuptools<82" in etape["note"], etape
    assert any("setuptools<82" in p for p in passages)


def test_setuptools_borne_seulement_sur_le_signal(tmp_path, monkeypatch):
    """Sans `pkg_resources` dans la sortie, aucune relance."""
    ecrire(tmp_path / "requirements.txt", "requests\n")
    ecrire(tmp_path / "tests" / "test_api.py", "def test_ko():\n    assert False\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    runner._env_required_by_image = lambda: {}
    avec_uvx(monkeypatch)
    passages = []

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        passages.append(list(commande))
        junit(commande, 1, 1)
        return _Resultat(1, "1 failed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    runner._run_pytest("http://localhost:32770")
    assert not any("setuptools<82" in p for p in passages)
