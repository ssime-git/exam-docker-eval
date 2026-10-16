"""Le corps /predict documenté par les tests de la copie, et les champs `int`.

    uv run --with pytest pytest -q test_corps_des_tests.py

462113 : README sans corps (`docker load`, `docker run`, `pytest -v`), service
lu dans l'image (`def predict(self, input_data: InputModel)`, champs `int`
dont `CGPA: int`). Les tests documentent le corps accepté :
`VALID_INPUT_DATA = {"input_data": {..., "CGPA": 9, ...}}`, envoyé par
`test_jwt_authentication_success` qui attend 200. Le correcteur ne lisait
pas ce corps et reconstruisait le sien depuis les types avec le profil
médian (SOP 3.5, CGPA 8.6) : 400 `int_from_float` trois fois, /predict
« non mesuré », Sécurité et Contrat non jugés. Vérifié sur l'image réelle :
le corps des tests rend 200, le corps reconstruit 400.
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner  # noqa: E402
from docker_eval.bentoml_runner import BentoMLRunner  # noqa: E402
from test_pytest_tel_que_livre import brancher_service, ecrire  # noqa: E402

SERVICE_462113 = '''import bentoml
from pydantic import BaseModel, Field, ConfigDict

USERS = {
    "user123": "password123",
}

class Credentials(BaseModel):
    username: str
    password: str

class InputModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    GRE_Score: int
    TOEFL_Score: int
    University_Rating: int
    SOP: int
    LOR_: int
    CGPA: int
    Research: int

@bentoml.service
class LRClassifierService:
    @bentoml.api(route="/login")
    def login(self, credentials: Credentials):
        ...

    @bentoml.api(route="/predict")
    def predict(self, input_data: InputModel):
        ...
'''
CHEMIN_IMAGE = "/home/bentoml/bento/src/src/service.py (dans l'image)"

TESTS_462113 = '''from datetime import datetime, timedelta, timezone
import jwt
import pytest
from starlette.testclient import TestClient

from src.service import LRClassifierService, JWT_SECRET_KEY, JWT_ALGORITHM

@pytest.fixture(scope="module")
def client():
    app = LRClassifierService.to_asgi()
    with TestClient(app) as test_client:
        yield test_client


VALID_INPUT_DATA = {
    "input_data":
        {
            "GRE_Score": 320,
            "TOEFL_Score": 110,
            "University_Rating": 4,
            "SOP": 4,
            "LOR_": 4,
            "CGPA": 9,
            "Research": 1,
        }
    }


def test_jwt_authentication_missing_token(client):
    response = client.post("/predict", json=VALID_INPUT_DATA)
    assert response.status_code == 401

def test_jwt_authentication_success(client):
    valid_token = "x"
    response = client.post(
        "/predict",
        json=VALID_INPUT_DATA,
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert response.status_code == 200

def test_login_success(client):
    response = client.post(
        "/login", json={"credentials":{"username": "user123", "password": "password123"}}
    )
    assert response.status_code == 200
'''

CHAMPS = ("GRE_Score", "TOEFL_Score", "University_Rating", "SOP", "LOR_", "CGPA", "Research")


def accepte_entiers_enveloppes(corps):
    """Le service réel : `input_data` obligatoire, sept champs entiers."""
    if not (isinstance(corps, dict) and set(corps) == {"input_data"} and isinstance(corps["input_data"], dict)):
        return False
    entree = corps["input_data"]
    return set(entree) == set(CHAMPS) and all(isinstance(v, int) and not isinstance(v, bool)
                                              for v in entree.values())


def copie_462113(tmp_path, tests=TESTS_462113):
    ecrire(tmp_path / "README.md", "docker load -i admission_prediction_service.tar\n"
                                   "docker run --rm -p 3000:3000 lr_classifier_service:latest\npytest -v\n")
    ecrire(tmp_path / "tests" / "test_service.py", tests)
    runner = BentoMLRunner("462113", str(tmp_path), 300, logging.getLogger("t"))
    runner._read_service_from_image = lambda: [(CHEMIN_IMAGE, SERVICE_462113)]
    return runner


def test_462113_corps_des_tests_lu_et_accepte(tmp_path, monkeypatch):
    runner = copie_462113(tmp_path)
    brancher_service(monkeypatch, accepte_entiers_enveloppes)
    resultats = runner._run_api_tests("http://localhost:32768")
    assert resultats["predict_passed"], resultats
    assert resultats["corps_source"] == "tests", resultats
    assert resultats["corps_enveloppe"] is None, resultats


def test_corps_d_un_test_d_echec_non_lu(tmp_path, monkeypatch):
    """Un corps envoyé seulement par un test qui attend un refus est une
    entrée invalide : il ne documente pas le contrat."""
    tests = '''import requests
BAD = {"input_data": {"GRE_Score": "abc", "TOEFL_Score": 110, "University_Rating": 4,
                      "SOP": 4, "LOR_": 4, "CGPA": 9, "Research": 1}}

def test_predict_invalide():
    r = requests.post("http://localhost:3000/predict", json=BAD, headers={"Authorization": "Bearer x"})
    assert r.status_code == 400
'''
    assert bentoml_runner.corps_documentes_par_les_tests(tests) == []
    runner = copie_462113(tmp_path, tests)
    contrat = runner._discover_api_contract()
    assert "predict_documented_source" not in contrat, contrat


def test_formes_lues_dans_les_tests():
    """URL en f-string, corps en ligne, `in (200, 201)` : lus. Un test qui
    ne vérifie aucun code ne documente rien."""
    tests = '''import requests
BASE_URL = "http://localhost:3000"

def test_ok():
    r = requests.post(f"{BASE_URL}/predict", json={"gre_score": 320, "toefl_score": 110, "cgpa": 9.1},
                      headers=H)
    assert r.status_code in (200, 201)

def test_sans_verification():
    requests.post(f"{BASE_URL}/predict", json={"gre_score": 1, "cgpa": 2})
'''
    assert bentoml_runner.corps_documentes_par_les_tests(tests) == [{"gre_score": 320, "toefl_score": 110, "cgpa": 9.1}]
    assert bentoml_runner.corps_documentes_par_les_tests("def f(:\n") == []


def test_corps_reconstruit_respecte_les_champs_int(tmp_path, monkeypatch):
    """Sans corps documenté, le corps reconstruit depuis la signature suit
    les types : `CGPA: int` reçoit un entier, `LOR_` le profil de LOR."""
    tests = "def test_x():\n    assert True\n"
    runner = copie_462113(tmp_path, tests)
    brancher_service(monkeypatch, accepte_entiers_enveloppes)
    resultats = runner._run_api_tests("http://localhost:32768")
    assert resultats["predict_passed"], resultats
    assert resultats["corps_source"] == "image", resultats
    contrat = runner._discover_api_contract()
    entree = contrat["predict_bodies"][0]
    assert all(isinstance(v, int) for v in entree.values()), json.dumps(entree)
    assert entree["LOR_"] == 4 and entree["CGPA"] == 9, entree


def test_fichiers_de_l_image_sans_saut_de_ligne_final(tmp_path, monkeypatch):
    """462113 : `prepare_data.py` sans saut de ligne final, le marqueur du
    fichier suivant (`service.py`) collait à sa dernière ligne : un seul
    fichier lu, le service rangé sous le nom de `prepare_data.py`."""
    from test_pytest_tel_que_livre import _Resultat
    sortie = ("===FILE:/home/bentoml/bento/src/src/prepare_data.py\nimport pandas"
              "===FILE:/home/bentoml/bento/src/src/service.py\nUSERS = {}\n"
              "def predict(self, input_data: InputModel): ...")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", lambda *a, **k: _Resultat(0, sortie))
    runner = BentoMLRunner("462113", str(tmp_path), 300, logging.getLogger("t"))
    runner.image_name = "lr_classifier_service:x"
    runner._platform_for_image = lambda: None
    lus = dict(runner._read_service_from_image())
    assert lus == {
        "/home/bentoml/bento/src/src/prepare_data.py (dans l'image)": "import pandas",
        "/home/bentoml/bento/src/src/service.py (dans l'image)":
            "USERS = {}\ndef predict(self, input_data: InputModel): ...",
    }, lus
