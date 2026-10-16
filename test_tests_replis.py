"""Les tests d'une copie bentoml se mesurent même quand leur environnement
livré ne s'installe pas ou qu'ils importent du code non livré.

    uv run --with pytest pytest -q test_tests_replis.py

468775 : requirements.txt illisible pour uv (« Unexpected '·' ...
requirements.txt:1:2 »). Rien ne s'installe, pytest ne collecte rien, la
catégorie Tests part en revue. Le requirements cassé est une faute de la
copie (critère Reproductibilité) ; les tests, eux, se mesurent dans un
environnement minimal.
466786 : tests/conftest.py fait `from src.auth import create_jwt_token`.
L'énoncé interdit de livrer src/ (archive = image, requirements, README,
tests) ; le code est dans l'image du bento.
"""
import logging
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner  # noqa: E402
from docker_eval.bentoml_runner import BentoMLRunner  # noqa: E402

ERREUR_UV_468775 = ("error: Unexpected '·', expected '-c', '-e', '-r' or the start of a requirement: "
                    "requirements.txt:1:2\n")


def ecrire(chemin, contenu=""):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


class _Resultat:
    def __init__(self, code, sortie=""):
        self.returncode, self.stdout, self.stderr = code, sortie, ""


def junit(commande, total):
    chemin = next(a.split("=", 1)[1] for a in commande if a.startswith("--junit-xml="))
    ecrire(chemin, f'<testsuite tests="{total}" failures="0" errors="0" skipped="0"></testsuite>')


def listing(racine):
    return sorted(os.path.relpath(os.path.join(r, f), racine)
                  for r, _, fichiers in os.walk(racine) for f in fichiers)


def test_468775_requirements_illisible_tests_mesures_en_repli(tmp_path, monkeypatch):
    racine = tmp_path / "examen_bentoml_ready_for_submition"
    ecrire(racine / "requirements.txt", "1·bentoml==1.4.35\n")
    ecrire(racine / "tests" / "test_api.py",
           "import os\nimport json\nimport requests\nimport jwt\nimport numpy as np\nfrom helpers import x\n\n"
           "def test_a():\n    assert True\n")
    ecrire(racine / "tests" / "helpers.py", "x = 1\n")
    runner = BentoMLRunner("468775", str(tmp_path), 300, logging.getLogger("t"))
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    commandes = []

    def faux_run(commande, **kwargs):
        commandes.append(list(commande))
        if "--with-requirements" in commande:
            return _Resultat(2, ERREUR_UV_468775)
        junit(commande, 3)
        return _Resultat(0, "3 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)

    resultat = runner._run_pytest("http://localhost:32770")
    assert resultat["evaluable"] is True and resultat["passed"] == 3 and resultat["total"] == 3, resultat
    assert resultat["requirements_inutilisable"] is True
    assert "requirements.txt:1:2" in resultat["requirements_erreur"]
    assert "environnement minimal" in resultat["environnement_de_repli"]
    repli = commandes[-1]
    paquets = [repli[i + 1] for i, a in enumerate(repli) if a == "--with"]
    assert "--with-requirements" not in repli
    assert {"pytest", "requests", "pyjwt", "numpy"} <= {p.lower() for p in paquets}, paquets
    # Ni la bibliothèque standard ni les modules locaux des tests.
    assert not {"os", "json", "helpers"} & set(paquets), paquets


def test_requirements_valide_aucun_repli(tmp_path, monkeypatch):
    ecrire(tmp_path / "requirements.txt", "requests==2.32.5\n")
    ecrire(tmp_path / "tests" / "test_api.py", "def test_ko():\n    assert False\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    appels = []

    def faux_run(commande, **kwargs):
        appels.append(commande)
        ecrire(next(a.split("=", 1)[1] for a in commande if a.startswith("--junit-xml=")),
               '<testsuite tests="1" failures="1" errors="0" skipped="0"></testsuite>')
        return _Resultat(1, "1 failed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    resultat = runner._run_pytest("http://localhost:32773")
    # Tel que livré puis réparé (politique du 16/10), aucun repli de plus.
    assert len(appels) == 2 and resultat["failed"] == 1
    assert "--with" not in appels[0][:bentoml_runner._position_pytest(appels[0])]
    assert "requirements_inutilisable" not in resultat and "code_lu_dans_l_image" not in resultat


CONFTEST_466786 = """import os
import pytest
import requests
import jwt

from src.auth import create_jwt_token

BASE_URL = "http://localhost:3000"
"""


def docker_cp_reel(source_dans_l_image, fichiers_image):
    """`docker cp CONTENEUR:SRC DEST` tel que Docker le fait : DEST absent,
    il est créé avec le contenu de SRC ; DEST présent, SRC y arrive sous son
    nom de base."""
    def cp(commande):
        _, _, src, dest = commande
        chemin = src.split(":", 1)[1]
        if chemin.rstrip("/") != source_dans_l_image:
            return _Resultat(1, f"Error response from daemon: Could not find the file {chemin}")
        cible = os.path.join(dest, os.path.basename(chemin.rstrip("/"))) if os.path.isdir(dest) else dest
        for relatif, contenu in fichiers_image.items():
            ecrire(os.path.join(cible, relatif), contenu)
        return _Resultat(0, "")
    return cp


@pytest.mark.parametrize("fichiers_image", [
    # include: ["src/"] dans bentofile.yaml : /home/bentoml/bento/src/src/auth.py
    {"src/auth.py": "def create_jwt_token():\n    return 't'\n", "src/service.py": "", "src/__init__.py": ""},
    # contenu de src/ à la racine du bento : /home/bentoml/bento/src/auth.py
    {"auth.py": "def create_jwt_token():\n    return 't'\n", "service.py": ""},
], ids=["src-imbrique", "src-a-plat"])
def test_466786_code_des_tests_lu_dans_l_image(tmp_path, monkeypatch, fichiers_image):
    ecrire(tmp_path / "requirements.txt", "pyjwt==2.10.1\nrequests==2.32.5\n")
    ecrire(tmp_path / "tests" / "__init__.py", "")
    ecrire(tmp_path / "tests" / "conftest.py", CONFTEST_466786)
    ecrire(tmp_path / "tests" / "test_auth.py", "def test_a():\n    assert True\n")
    runner = BentoMLRunner("466786", str(tmp_path), 300, logging.getLogger("t"))
    runner.image_name = "eval_466786/admission_prediction_service:latest"
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    avant = listing(tmp_path)
    cp = docker_cp_reel("/home/bentoml/bento/src", fichiers_image)
    racines_vues = []

    def faux_run(commande, **kwargs):
        if commande[:2] == ["docker", "cp"]:
            assert commande[2].startswith(runner.container_name + ":"), commande
            return cp(commande)
        if commande[0] == "docker":  # lecture du service pour l'environnement requis
            return _Resultat(0, "")
        assert commande[0] == "uvx", commande
        chemins = (kwargs.get("env") or {}).get("PYTHONPATH", "").split(os.pathsep)
        racine = next((c for c in chemins if c and os.path.isfile(os.path.join(c, "src", "auth.py"))), None)
        racines_vues.append(racine)
        if racine is None:
            return _Resultat(4, "ImportError while loading conftest 'tests/conftest.py'.\n"
                                "tests/conftest.py:6: in <module>\n    from src.auth import create_jwt_token\n"
                                "E   ModuleNotFoundError: No module named 'src'\n")
        junit(commande, 4)
        return _Resultat(0, "4 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)

    resultat = runner._run_pytest("http://localhost:32768")
    assert resultat["evaluable"] is True and resultat["passed"] == 4, resultat
    assert "/home/bentoml/bento/src" in resultat["code_lu_dans_l_image"]
    assert "code non livré" in resultat["code_lu_dans_l_image"]
    assert racines_vues[0] is None and racines_vues[-1] is not None
    # Le code extrait vit hors de la copie et disparaît après les tests.
    assert not racines_vues[-1].startswith(str(tmp_path))
    assert not os.path.exists(racines_vues[-1])
    apres = [f for f in listing(tmp_path) if f not in ("test_results.log", "pytest_report.xml")]
    assert apres == avant, (avant, apres)


def test_module_absent_qui_n_est_pas_dans_l_image_aucun_repli(tmp_path, monkeypatch):
    """`No module named 'jwt'` est un paquet, pas du code de la copie : rien
    à lire dans l'image, l'échec reste tel quel."""
    ecrire(tmp_path / "tests" / "test_a.py", "import jwt\n\ndef test_a():\n    assert True\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    runner.image_name = "eval_x/img:latest"
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    cp = docker_cp_reel("/home/bentoml/bento/src", {"service.py": ""})
    uvx = []

    def faux_run(commande, **kwargs):
        if commande[:2] == ["docker", "cp"]:
            return cp(commande)
        if commande[0] == "docker":
            return _Resultat(0, "")
        uvx.append(commande)
        return _Resultat(2, "E   ModuleNotFoundError: No module named 'jwt'\n")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    resultat = runner._run_pytest("http://localhost:32768")
    # Tel que livré puis réparé : la lecture de l'image n'ajoute aucun passage.
    assert len(uvx) == 2 and resultat["evaluable"] is False
    assert "code_lu_dans_l_image" not in resultat


def test_relance_sur_fichiers_explicites_garde_le_lanceur_pytest(tmp_path, monkeypatch):
    """Code 5 : la relance coupait la commande au premier « pytest », celui
    de `--with pytest`, et perdait tout l'outillage."""
    ecrire(tmp_path / "tests" / "verif_api.py", "def test_a():\n    assert True\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    commandes = []

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        commandes.append(commande)
        # La découverte par défaut ne trouve rien (tel que livré comme
        # réparé) ; la relance sur fichiers explicites, si.
        if not commande[bentoml_runner._position_pytest(commande) + 1].endswith(".py"):
            return _Resultat(5, "no tests ran")
        junit(commande, 1)
        return _Resultat(0, "1 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    resultat = runner._run_pytest("http://localhost:32773")
    repare, relance = commandes[1], commandes[-1]
    assert relance[:5] == repare[:5] and "--with" in relance[5:], relance
    assert [r["id"] for r in resultat["reparations"]] == ["relance_fichiers_explicites"], resultat["reparations"]
    assert resultat["pytest_tel_que_livre"]["code"] == 5
    assert relance[bentoml_runner._position_pytest(relance) + 1].endswith("verif_api.py"), relance
    assert resultat["passed"] == 1
