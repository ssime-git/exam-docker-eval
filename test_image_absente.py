"""462649 : rendu sans archive d'image Docker.

Le README demande `docker load -i admission_prediction_service.tar`, mais ce
fichier n'est pas dans le rendu. Avant de construire l'image depuis les
sources, le moteur cherche dans le README une instruction de récupération
(`docker pull` de l'image que le README lance, ou URL d'une archive d'image)
et la suit : ce n'est pas une réparation. Sans instruction, il le dit
(`image_absente`, `instruction_recuperation: null`, ligne du README citée).
Le `curl` qui télécharge admission.csv n'est pas une instruction de
récupération d'image.
"""
import logging
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner, instruction_de_recuperation, ligne_d_image_citee

README_462649 = """# Examen BentoML

```bash
curl -o data/raw/admission.csv \\
  https://assets-datascientest.s3.eu-west-1.amazonaws.com/MLOPS/bentoml/admission.csv
```

## 4. Lancer

```bash
docker load -i admission_prediction_service.tar
docker run --rm -d -p 3000:3000 --name admission_prediction_service admission_prediction_service:latest
pytest -v
```
"""


def test_462649_pas_d_instruction_le_csv_ne_compte_pas():
    assert instruction_de_recuperation(README_462649) is None


def test_462649_ligne_docker_load_citee():
    assert ligne_d_image_citee(README_462649) == "docker load -i admission_prediction_service.tar"


def test_docker_pull_de_l_image_lancee():
    readme = "docker pull alice/admission:1.0\ndocker run -p 3000:3000 alice/admission:1.0\n"
    assert instruction_de_recuperation(readme) == {
        "type": "registre", "ref": "alice/admission:1.0", "ligne": "docker pull alice/admission:1.0"}


def test_docker_pull_d_une_image_de_base_ne_compte_pas():
    readme = "docker pull python:3.11\ndocker run -p 3000:3000 admission:latest\n"
    assert instruction_de_recuperation(readme) is None


def test_docker_pull_sans_docker_run_exige_un_espace_de_noms():
    assert instruction_de_recuperation("docker pull python:3.11\n") is None
    assert instruction_de_recuperation("docker pull ghcr.io/alice/admission:2\n")["ref"] == "ghcr.io/alice/admission:2"


def test_url_d_archive_d_image():
    readme = "wget https://example.org/rendu/admission_service.tar.gz\ndocker load -i admission_service.tar.gz\n"
    assert instruction_de_recuperation(readme)["type"] == "url"
    assert instruction_de_recuperation(readme)["url"] == "https://example.org/rendu/admission_service.tar.gz"


def test_url_sans_extension_mais_sortie_en_tar():
    readme = "curl -L -o image.tar 'https://drive.example.org/download?id=abc'\n"
    assert instruction_de_recuperation(readme)["url"] == "https://drive.example.org/download?id=abc"


def runner_sans_image(tmp_path, readme):
    (tmp_path / "README.md").write_text(readme)
    return BentoMLRunner("462649", str(tmp_path), 60, logging.getLogger("t"))


def test_462649_champs_exposes_sans_instruction(tmp_path):
    runner = runner_sans_image(tmp_path, README_462649)
    assert runner._recuperer_image_absente() is None
    champs = runner._champs_image_absente()
    assert champs["image_absente"] is True
    assert champs["instruction_recuperation"] is None
    assert champs["image_source"] is None
    assert champs["readme_image_citee"] == "docker load -i admission_prediction_service.tar"


def test_archive_livree_rien_d_absent(tmp_path):
    runner = runner_sans_image(tmp_path, README_462649)
    runner.image_tar = str(tmp_path / "admission_prediction_service.tar")
    champs = runner._champs_image_absente()
    assert champs["image_absente"] is False
    assert champs["instruction_recuperation"] is None


def test_docker_pull_suivi(tmp_path, monkeypatch):
    runner = runner_sans_image(tmp_path, "docker pull alice/admission:1.0\ndocker run alice/admission:1.0\n")
    lances = []

    def faux_run(commande, **kw):
        lances.append(commande)
        if commande[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="No such image")
        return types.SimpleNamespace(returncode=0, stdout="1.0: Pulling from alice/admission\n", stderr="")

    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    monkeypatch.setattr(runner, "adopter_image", lambda ref: f"eval_462649/{ref}")
    assert runner._recuperer_image_absente() == "eval_462649/alice/admission:1.0"
    assert ["docker", "pull", "alice/admission:1.0"] in lances
    champs = runner._champs_image_absente()
    assert champs["image_absente"] is False
    assert champs["image_source"] == "registre"
    assert champs["instruction_recuperation"]["ref"] == "alice/admission:1.0"
    assert "alice/admission:1.0" in runner._infos_image()["image_tags"]


def test_docker_pull_en_echec(tmp_path, monkeypatch):
    runner = runner_sans_image(tmp_path, "docker pull alice/admission:1.0\ndocker run alice/admission:1.0\n")
    monkeypatch.setattr(bentoml_runner.subprocess, "run",
                        lambda commande, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="manifest unknown"))
    assert runner._recuperer_image_absente() is None
    champs = runner._champs_image_absente()
    assert champs["image_absente"] is True
    assert champs["image_source"] is None
    assert champs["instruction_recuperation"]["ref"] == "alice/admission:1.0"
    assert "manifest unknown" in champs["recuperation_erreur"]


# --- 464154 : /login répond 404 ---------------------------------------------
# L'image sert /login_service et /predict_service (apis/openapi.yaml de
# l'image) ; l'énoncé impose un endpoint nommé login. Le moteur dit la route
# introuvable et liste les routes déclarées, au lieu d'un refus d'identifiants.

def test_464154_routes_de_l_openapi():
    doc = {"paths": {"/login_service": {}, "/predict_service": {}, "/readyz": {}, "/livez": {}, "/docs.json": {}}}
    assert bentoml_runner.routes_de_l_openapi(doc) == ["/login_service", "/predict_service"]
    assert bentoml_runner.routes_de_l_openapi("pas un dict") == []


def test_464154_login_introuvable(tmp_path, monkeypatch):
    runner = BentoMLRunner("464154", str(tmp_path), 60, logging.getLogger("t"))
    monkeypatch.setattr(runner, "_discover_api_contract", lambda: {"usernames": ["u"], "passwords": ["p"], "login_wrappers": [None]})

    def faux_post(url, **kw):
        return types.SimpleNamespace(status_code=404, text="Not Found", headers={}, json=lambda: {})

    def faux_get(url, **kw):
        return types.SimpleNamespace(status_code=200, json=lambda: {"paths": {"/login_service": {}, "/predict_service": {}}})

    import requests as vrai_requests
    # Un autre test remplace bentoml_runner.requests par un double : on pose
    # le nôtre en entier plutôt que d'en patcher les attributs.
    monkeypatch.setattr(bentoml_runner, "requests", types.SimpleNamespace(
        post=faux_post, get=faux_get, exceptions=vrai_requests.exceptions))
    resultats = runner._run_api_tests("http://127.0.0.1:1")
    assert resultats["login_passed"] is False
    assert resultats["login_introuvable"] is True
    assert resultats["routes_declarees"] == ["/login_service", "/predict_service"]
