"""Les tests d'une copie bentoml doivent tourner — sinon la catégorie sort
du barème et la copie ne se juge plus (scriptorium, 14/10).

459845 : `BASE_URL = os.environ.get("SERVICE_BASE_URL", "http://localhost:3000")`.
Ni la variable exportée ni la forme réécrite : les 8 tests visaient le port
documenté, le service tournait sur un port éphémère — connexion refusée.
461638 : numpy==2.5.3 (Python >= 3.12), README `uv venv --python 3.12`, uvx
en 3.11 : aucun test collecté.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner, environnement
from docker_eval.bentoml_runner import BentoMLRunner

TEST_459845 = '''import os

import requests

BASE_URL = os.environ.get("SERVICE_BASE_URL", "http://localhost:3000")
AUTRE = "http://localhost:30001"
SERVEUR_LOCAL = "http://127.0.0.1:8000"


def test_login():
    assert requests.post(f"{BASE_URL}/login").status_code == 200
'''


def ecrire(chemin, contenu=""):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def test_l_adresse_documentee_du_service_vise_le_port_reel(tmp_path):
    ecrire(tmp_path / "tests" / "test_endpoints.py", TEST_459845)
    runner = BentoMLRunner("459845", str(tmp_path), 300, logging.getLogger("t"))
    runner._prepare_tests_for_dynamic_base_url(str(tmp_path / "tests"), "http://localhost:32772")
    contenu = (tmp_path / "tests" / "test_endpoints.py").read_text()
    assert 'os.environ.get("SERVICE_BASE_URL", "http://localhost:32772")' in contenu
    # Ni un autre port qui commence pareil, ni un serveur local du conftest.
    assert '"http://localhost:30001"' in contenu and '"http://127.0.0.1:8000"' in contenu


def test_le_python_declare_passe_en_premier(tmp_path):
    ecrire(tmp_path / "exam_bentoml_rendu" / "README.md", "Installer :\n\n    uv venv --python 3.12\n")
    assert environnement.pythons_pour_les_tests(str(tmp_path), "3.11") == ["3.12", "3.11", "3.13"]
    vide = tmp_path / "vide"
    vide.mkdir()
    assert environnement.pythons_pour_les_tests(str(vide), "3.11") == ["3.11", "3.12", "3.13"]


class _Resultat:
    def __init__(self, code, sortie=""):
        self.returncode, self.stdout, self.stderr = code, sortie, ""


def test_dependances_non_installables_on_essaie_le_python_suivant(tmp_path, monkeypatch):
    """461638 sans déclaration lisible : 3.11 ne peut pas installer numpy
    2.5.3, 3.12 si. Les tests tournent, et le Python retenu est consigné."""
    ecrire(tmp_path / "tests" / "test_api.py", "def test_ok():\n    assert True\n")
    ecrire(tmp_path / "requirements.txt", "numpy==2.5.3\n")
    runner = BentoMLRunner("461638", str(tmp_path), 300, logging.getLogger("t"))
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    pythons = []

    def faux_run(commande, **kwargs):
        python = commande[commande.index("--python") + 1]
        pythons.append(python)
        if python == "3.11":
            return _Resultat(1, "  × No solution found when resolving `--with` dependencies:\n"
                                "  ╰─▶ Because numpy==2.5.3 requires Python>=3.12 ...\n"
                                "error: Failed to run tool")
        junit = next(a.split("=", 1)[1] for a in commande if a.startswith("--junit-xml="))
        ecrire(junit, '<testsuite tests="1" failures="0" errors="0" skipped="0"></testsuite>')
        return _Resultat(0, "1 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)

    resultat = runner._run_pytest("http://localhost:32773")
    assert pythons == ["3.11", "3.12"]
    assert resultat["evaluable"] is True and resultat["passed"] == 1
    assert resultat["python"] == "3.12" and resultat["pythons_essayes"] == ["3.11", "3.12"]


def test_des_tests_qui_echouent_ne_relancent_rien(tmp_path, monkeypatch):
    """Un vrai échec de la copie n'est pas une panne d'installation."""
    ecrire(tmp_path / "tests" / "test_api.py", "def test_ko():\n    assert False\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    appels = []

    def faux_run(commande, **kwargs):
        appels.append(commande)
        junit = next(a.split("=", 1)[1] for a in commande if a.startswith("--junit-xml="))
        ecrire(junit, '<testsuite tests="1" failures="1" errors="0" skipped="0"></testsuite>')
        return _Resultat(1, "1 failed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    resultat = runner._run_pytest("http://localhost:32773")
    assert len(appels) == 1 and resultat["failed"] == 1 and resultat["evaluable"] is True
