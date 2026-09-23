"""Les faits d'environnement du harnais sont une étape de la trace (scriptorium #322).

461638 : les dépendances de la copie ont été installées en Python 3.11
(`uvx --python 3.11`), alors que son README prévoit `uv venv --python 3.12`.
« numpy==2.5.3 exige Python >= 3.12 » a été imputé à l'apprenant : aucune
étape ne disait quel Python tournait, ni lequel la copie demandait.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import environnement
from docker_eval.bento_compiled_runner import BentoCompiledRunner
from docker_eval.bentoml_runner import BentoMLRunner
from docker_eval.compose_runner import ComposeRunner
from docker_eval.config import PYTHON_TESTS

TITRE = "Environnement du harnais"

# Extrait du README de 461638, tel que rendu.
README_461638 = """# Admission prediction service

## Installation

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt
```
"""


@pytest.fixture(autouse=True)
def python_resolu(monkeypatch):
    # Pas d'appel à uv dans les tests : la résolution est simulée.
    monkeypatch.setattr(environnement, "version_python_resolue", lambda version: "3.11.15")


def etape(steps):
    trouvees = [s for s in steps if s["title"] == TITRE]
    assert len(trouvees) == 1, [s["title"] for s in steps]
    return trouvees[0]


# --- version déclarée par la copie ------------------------------------------

def test_version_declaree_dans_le_readme_imbrique(tmp_path):
    (tmp_path / "exam_bentoml_rendu").mkdir()
    (tmp_path / "exam_bentoml_rendu" / "README.md").write_text(README_461638)

    assert environnement.versions_python_declarees(str(tmp_path)) == [
        ("exam_bentoml_rendu/README.md", "3.12", "uv venv --python 3.12"),
    ]


def test_version_declaree_python_version_et_pyproject(tmp_path):
    (tmp_path / ".python-version").write_text("3.12.4\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.12"\n')
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyproject.toml").write_text('requires-python = ">=3.8"\n')

    assert environnement.versions_python_declarees(str(tmp_path)) == [
        (".python-version", "3.12.4", "3.12.4"),
        ("pyproject.toml", ">=3.12", 'requires-python = ">=3.12"'),
    ]


def test_aucune_version_declaree(tmp_path):
    (tmp_path / "README.md").write_text("Lancer `make run`.\n")

    assert environnement.versions_python_declarees(str(tmp_path)) == []


def test_images_de_base_des_dockerfiles(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "Dockerfile").write_text(
        "FROM --platform=linux/amd64 python:3.12-slim AS build\nRUN pip install x\n"
        "FROM build\nCMD [\"python\"]\n")
    (tmp_path / "Dockerfile").write_text("FROM nginx:1.27\n")

    assert environnement.images_de_base(str(tmp_path)) == [
        ("Dockerfile", "nginx:1.27"),
        ("api/Dockerfile", "python:3.12-slim"),
    ]


def test_limites_lues_dans_hostconfig():
    assert environnement.limites({"HostConfig": {"Memory": 0, "NanoCpus": 0}}) == \
        "aucune limite mémoire ni CPU"
    assert environnement.limites({"HostConfig": {"Memory": 2 * 1024 ** 3, "NanoCpus": 1_500_000_000}}) == \
        "mémoire 2.0 Gio, CPU 1.5"
    assert environnement.limites({}) == "limites non lues"


# --- l'étape, runner par runner ----------------------------------------------

def test_bentoml_image_seule_garde_ses_etapes_et_l_environnement(tmp_path, monkeypatch):
    """Le chemin de 461638 (image_only_cli) rendait un résultat sans `steps`."""
    (tmp_path / "exam_bentoml_rendu").mkdir()
    (tmp_path / "exam_bentoml_rendu" / "README.md").write_text(README_461638)
    runner = BentoMLRunner("461638", str(tmp_path), 300, logging.getLogger("env"),
                           image_tar=str(tmp_path / "image.tar"))
    monkeypatch.setattr(runner, "_should_use_image_only_mode", lambda: True)
    monkeypatch.setattr(runner, "_load_docker_image_cli", lambda: None)
    monkeypatch.setattr(runner, "cleanup", lambda force=False: None)
    monkeypatch.setattr(runner, "_host_arch", lambda: "amd64")
    monkeypatch.setattr(runner, "_qemu_available", lambda: False)

    resultat = runner.run_evaluation()

    assert resultat["steps"][0]["title"] == "Ce que l'apprenant a rendu"
    env = etape(resultat["steps"])
    assert env["exit_code"] == 0
    assert f"Python des tests et des dépendances de la copie : {PYTHON_TESTS} " \
           f"(uvx --python {PYTHON_TESTS}, résolu en 3.11.15)" in env["output"]
    assert "version déclarée par la copie : 3.12 (exam_bentoml_rendu/README.md : " \
           "uv venv --python 3.12)" in env["output"]
    assert "machine : amd64, émulation QEMU absente" in env["output"]


def test_bentoml_image_seule_complete_image_et_limites(tmp_path, monkeypatch):
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("env"))
    monkeypatch.setattr(runner, "_host_arch", lambda: "amd64")
    monkeypatch.setattr(runner, "_qemu_available", lambda: True)
    runner.consigner_environnement(python_tests=PYTHON_TESTS)
    runner.image_name = "admission_prediction_service:latest"
    monkeypatch.setattr(runner, "_image_arch", lambda: "arm64")

    runner._completer_environnement_image("linux/arm64", {"HostConfig": {"Memory": 0, "NanoCpus": 0}})

    sortie = etape(runner.steps)["output"]
    assert "image de l'apprenant : admission_prediction_service:latest (arm64), " \
           "exécutée sous émulation linux/arm64" in sortie
    assert "limites du conteneur : aucune limite mémoire ni CPU" in sortie


def test_bento_compile_rend_ses_etapes(tmp_path, monkeypatch):
    runner = BentoCompiledRunner("x", str(tmp_path), 300, logging.getLogger("env"))
    monkeypatch.setattr(runner, "cleanup", lambda force=False: None)

    resultat = runner.run_evaluation()

    assert resultat["success"] is False
    env = etape(resultat["steps"])
    assert "Python des tests et des dépendances de la copie : Python par défaut de uv " \
           "(uvx sans --python, résolu en 3.11.15)" in env["output"]


def test_compose_consigne_l_environnement_avant_le_build(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services:\n  api:\n    build: ./api\n")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / ".python-version").write_text("3.12\n")
    runner = ComposeRunner("copie-test", str(tmp_path), 1, logging.getLogger("env"))
    monkeypatch.setattr(runner, "cleanup", lambda force=False: None)

    class ComposeEnPanne:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            raise RuntimeError("build impossible")

        def __exit__(self, *_a):
            return False

    import docker_eval.compose_runner as module
    monkeypatch.setattr(module, "DockerCompose", ComposeEnPanne)

    resultat = runner.run_evaluation()

    titres = [s["title"] for s in resultat["steps"]]
    assert titres.index(TITRE) < titres.index("Build et démarrage Compose")
    sortie = etape(resultat["steps"])["output"]
    assert "Python des tests et des dépendances de la copie : celui des images de la copie " \
           "(le harnais n'installe rien)" in sortie
    assert "images de base : python:3.12-slim (api/Dockerfile)" in sortie
    assert "version déclarée par la copie : 3.12 (.python-version : 3.12)" in sortie


def test_compose_complete_avec_les_conteneurs(tmp_path, monkeypatch):
    runner = ComposeRunner("copie-test", str(tmp_path), 1, logging.getLogger("env"))
    monkeypatch.setattr(runner, "_host_arch", lambda: "amd64")
    monkeypatch.setattr(runner, "_qemu_available", lambda: True)
    runner.consigner_environnement(python_tests=None)

    class Conteneur:
        name = "copie-api-1"
        attrs = {"Config": {"Image": "copie-api"}, "Image": "sha256:abc",
                 "HostConfig": {"Memory": 512 * 1024 ** 2, "NanoCpus": 0}}

    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [Conteneur()])
    monkeypatch.setattr(environnement, "arch_image", lambda ref: "amd64")

    runner._completer_environnement_compose()

    sortie = etape(runner.steps)["output"]
    assert "conteneur copie-api-1 : image copie-api (amd64), limites : mémoire 512 Mio" in sortie
