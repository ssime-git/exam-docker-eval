"""Les tests lancés sont ceux de la partie notée, pas ceux de la partie facultative.

457338 : le rendu contient `mandatory_part/` (l'image notée et ses 10 tests) et
`optional_part/` (une passerelle docker compose et ses 7 tests). Le moteur
prenait le premier dossier de tests rendu par `os.walk`, dans l'ordre du
système de fichiers : il a lancé `optional_part/tests` contre l'image de
`mandatory_part`. Ces tests visent un autre contrat (passerelle, lots) et ont
reçu des 400 ; la copie a perdu 5 points sur la catégorie Tests.

    uvx --with pytest --with testcontainers --with docker --with requests \
        --with pyyaml pytest -q test_tests_partie_obligatoire.py
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval.bentoml_runner import BentoMLRunner

TEST = "def test_ok():\n    assert True\n"


def ecrire(chemin, contenu):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def _rendu(racine, ordre):
    """Les deux parties, créées dans l'ordre donné : selon le système de
    fichiers, `os.walk` rend l'une ou l'autre en premier."""
    for partie in ordre:
        if partie == "optional_part":
            ecrire(racine / "optional_part" / "tests" / "test_endpoints.py", TEST)
            ecrire(racine / "optional_part" / "src" / "service_batch.py", "")
        else:
            ecrire(racine / "mandatory_part" / "tests" / "test_service.py", TEST)
            ecrire(racine / "mandatory_part" / "tests" / "conftest.py", "")
            ecrire(racine / "mandatory_part" / "docker_image" / "service.tar", "")


ORDRES = [("optional_part", "mandatory_part"), ("mandatory_part", "optional_part")]


@pytest.mark.parametrize("ordre", ORDRES)
def test_les_tests_choisis_sont_ceux_de_l_image_evaluee(tmp_path, ordre):
    _rendu(tmp_path, ordre)
    tar = str(tmp_path / "mandatory_part" / "docker_image" / "service.tar")
    runner = BentoMLRunner("457338", str(tmp_path), 300, logging.getLogger("t"), image_tar=tar)

    assert runner._find_tests_path() == str(tmp_path / "mandatory_part" / "tests")


@pytest.mark.parametrize("ordre", ORDRES)
def test_sans_image_la_partie_facultative_passe_apres(tmp_path, ordre):
    _rendu(tmp_path, ordre)
    runner = BentoMLRunner("457338", str(tmp_path), 300, logging.getLogger("t"))

    assert runner._find_tests_path() == str(tmp_path / "mandatory_part" / "tests")


def test_seule_la_partie_facultative_a_des_tests_on_les_lance(tmp_path):
    ecrire(tmp_path / "optional_part" / "tests" / "test_endpoints.py", TEST)
    ecrire(tmp_path / "mandatory_part" / "docker_image" / "service.tar", "")
    tar = str(tmp_path / "mandatory_part" / "docker_image" / "service.tar")
    runner = BentoMLRunner("457338", str(tmp_path), 300, logging.getLogger("t"), image_tar=tar)

    assert runner._find_tests_path() == str(tmp_path / "optional_part" / "tests")


def test_un_dossier_tests_a_la_racine_reste_prioritaire(tmp_path):
    ecrire(tmp_path / "tests" / "test_api.py", TEST)
    ecrire(tmp_path / "mandatory_part" / "tests" / "test_service.py", TEST)
    runner = BentoMLRunner("1", str(tmp_path), 300, logging.getLogger("t"))

    assert runner._find_tests_path() == str(tmp_path / "tests")


# Les README des deux parties documentent des contrats différents. Le moteur
# lisait celui que `os.walk` rendait en premier (457338 : `optional_part`).
README_RACINE = "# Projet\n\nVoir mandatory_part/README.md et optional_part/README.md.\n"
README_OBLIGATOIRE = """# Partie obligatoire

```bash
curl -X POST http://localhost:3000/login \\
  -H "Content-Type: application/json" \\
  -d '{"credentials": {"username": "user123", "password": "password123"}}'
curl -X POST http://localhost:3000/predict \\
  -H "Authorization: Bearer <token>" \\
  -d '{"input_data": {"gre_score": 309, "cgpa": 7.94}}'
```
"""
README_FACULTATIF = """# Partie facultative

```bash
curl -X POST http://localhost:3000/login \\
  -H "Content-Type: application/json" \\
  -d '{"credentials": {"username": "passerelle", "password": "secretbatch"}}'
curl -X POST http://localhost:3000/predict \\
  -H "Authorization: Bearer <token>" \\
  -d '{"lot": [{"gre": 1}]}'
```
"""


def _rendu_readmes(racine, ordre):
    ecrire(racine / "README.md", README_RACINE)
    for partie in ordre:
        if partie == "optional_part":
            ecrire(racine / "optional_part" / "README.md", README_FACULTATIF)
        else:
            ecrire(racine / "mandatory_part" / "README.md", README_OBLIGATOIRE)
            ecrire(racine / "mandatory_part" / "docker_image" / "service.tar", "")


@pytest.mark.parametrize("ordre", ORDRES)
@pytest.mark.parametrize("avec_image", [True, False])
def test_le_contrat_vient_du_readme_de_la_partie_obligatoire(tmp_path, monkeypatch, ordre, avec_image):
    _rendu_readmes(tmp_path, ordre)
    tar = str(tmp_path / "mandatory_part" / "docker_image" / "service.tar") if avec_image else None
    runner = BentoMLRunner("457338", str(tmp_path), 300, logging.getLogger("t"), image_tar=tar)
    monkeypatch.setattr(runner, "_read_service_from_image", lambda: [])

    contrat = runner._discover_api_contract()

    obligatoire = str(tmp_path / "mandatory_part" / "README.md")
    assert contrat["predict_documented_source"] == obligatoire
    assert contrat["predict_bodies"][0] == {"input_data": {"gre_score": 309, "cgpa": 7.94}}
    assert contrat["usernames"] == ["user123"]
    assert contrat["credentials_source"].startswith(obligatoire)
    assert contrat["source"] == str(tmp_path / "README.md")
