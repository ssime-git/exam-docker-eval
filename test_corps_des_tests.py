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


def test_test_qui_exclut_200_ne_documente_rien():
    """`!= 200` ou `not in (200, 201)` attend un refus : corps non lu."""
    tests = '''import requests
CORPS = {"gre_score": 320, "cgpa": 9.1}

def test_refus():
    r = requests.post("http://x/predict", json=CORPS)
    assert r.status_code != 200

def test_refus_bis():
    r = requests.post("http://x/predict", json=CORPS)
    assert r.status_code not in (200, 201)
'''
    assert bentoml_runner.corps_documentes_par_les_tests(tests) == []


def test_469475_script_d_exemple_sous_tests():
    corps_documentes_par_les_tests = bentoml_runner.corps_documentes_par_les_tests
    """469475 : tests/example_request.py, script `if __name__ == '__main__'`
    qui se connecte puis envoie le corps à /predict et lève sur une erreur
    (`raise_for_status`). C'est la copie qui documente le corps."""
    texte = '''
import requests
if __name__ == '__main__':
    base = "http://127.0.0.1:3000"
    login = requests.post(base + '/login', json={'username': 'u', 'password': 'p'}, timeout=30)
    login.raise_for_status()
    response = requests.post(base + '/predict', headers={},
        json={'gre_score': 320, 'toefl_score': 110, 'university_rating': 4,
              'sop': 4.0, 'lor': 4.5, 'cgpa': 9.0, 'research': 1}, timeout=30)
    response.raise_for_status()
'''
    assert corps_documentes_par_les_tests(texte) == [{'gre_score': 320, 'toefl_score': 110, 'university_rating': 4,
                                                      'sop': 4.0, 'lor': 4.5, 'cgpa': 9.0, 'research': 1}]


def test_script_sans_verification_de_succes_ne_documente_rien():
    corps_documentes_par_les_tests = bentoml_runner.corps_documentes_par_les_tests
    texte = '''
import requests
if __name__ == '__main__':
    requests.post("http://x/predict", json={'gre_score': 0})
'''
    assert corps_documentes_par_les_tests(texte) == []


def test_raise_for_status_sous_pytest_raises_attend_un_refus():
    texte = '''
import pytest, requests
def test_predict_invalide():
    r = requests.post("http://x/predict", json={'gre_score': 320, 'toefl_score': 110, 'university_rating': 4,
                      'sop': 4.0, 'lor': 4.5, 'cgpa': 9.0, 'research': 1})
    with pytest.raises(requests.HTTPError):
        r.raise_for_status()
'''
    assert bentoml_runner.corps_documentes_par_les_tests(texte) == []


# --- Corps documentés que le moteur ne lisait pas (provenance « image » ou
# « devine », −2 en reproductibilité au barème) -------------------------------

def test_463355_corps_entre_accents_graves_apres_le_libelle():
    """463355 : « - Body : `{"input_data": {...}}` » : un accent grave entre
    le libellé et l'objet."""
    readme = ('- `POST /predict` : prédiction (nécessite un token JWT valide dans le header `Authorization: Bearer <token>`)\n'
              '  - Body : `{"input_data": {"gre_score": 320, "toefl_score": 110, "university_rating": 4, '
              '"sop": 4.0, "lor": 4.5, "cgpa": 8.9, "research": 1}}`\n')
    corps = next(bentoml_runner.corps_en_ligne_apres_predict(readme), None)
    assert corps == {"input_data": {"gre_score": 320, "toefl_score": 110, "university_rating": 4,
                                    "sop": 4.0, "lor": 4.5, "cgpa": 8.9, "research": 1}}


def test_467058_corps_dans_un_bloc_json_apres_le_libelle():
    """467058 : « Example body: » puis un bloc ```json."""
    readme = ("### POST /predict\n\nHeaders:\n\n```text\nAuthorization: Bearer <token>\n```\n\nExample body:\n\n"
              '```json\n{\n  "gre_score": 320,\n  "toefl_score": 110,\n  "university_rating": 4,\n  "sop": 4.5,\n'
              '  "lor": 4.5,\n  "cgpa": 9.0,\n  "research": 1\n}\n```\n')
    corps = next(bentoml_runner.corps_en_ligne_apres_predict(readme), None)
    assert corps == {"gre_score": 320, "toefl_score": 110, "university_rating": 4, "sop": 4.5,
                     "lor": 4.5, "cgpa": 9.0, "research": 1}


def test_467635_corps_passe_par_une_fonction_d_aide():
    """467635 : `post_predict(base_url, VALID_INPUT, headers)` enveloppe le
    corps dans `{"input_data": input_data}` ; le test attend 200."""
    texte = '''
import requests
VALID_INPUT = {"gre_score": 309, "toefl_score": 108, "university_rating": 4, "sop": 3.0, "lor": 4.0, "cgpa": 7.94, "research": 0}

def post_predict(base_url, input_data, headers=None):
    return requests.post(f"{base_url}/predict", headers=headers or {}, json={"input_data": input_data})

def test_jwt_missing(base_url):
    response = post_predict(base_url, VALID_INPUT)
    assert response.status_code == 401

def test_jwt_valid_token(base_url):
    response = post_predict(base_url, VALID_INPUT, {"Authorization": "Bearer x"})
    assert response.status_code == 200
'''
    assert bentoml_runner.corps_documentes_par_les_tests(texte) == [{"input_data": {
        "gre_score": 309, "toefl_score": 108, "university_rating": 4, "sop": 3.0, "lor": 4.0, "cgpa": 7.94, "research": 0}}]


def test_fonction_d_aide_dans_un_test_d_echec_ne_documente_rien():
    texte = '''
import requests
BAD = {"gre_score": "x"}
def post_predict(base_url, data):
    return requests.post(f"{base_url}/predict", json=data)
def test_invalide(base_url):
    assert post_predict(base_url, BAD).status_code == 422
'''
    assert bentoml_runner.corps_documentes_par_les_tests(texte) == []


FEATURES_471639 = {
    "GRE Score": 337, "TOEFL Score": 118, "University Rating": 4,
    "SOP": 4.5, "LOR": 4.5, "CGPA": 9.65, "Research": 1,
}
TESTS_471639 = '''
import requests
FEATURES = {"GRE Score": 337, "TOEFL Score": 118, "University Rating": 4,
            "SOP": 4.5, "LOR": 4.5, "CGPA": 9.65, "Research": 1}

def test_predict_rejects_missing_token():
    response = requests.post("http://localhost:3000/predict", json={"input_data": FEATURES})
    assert response.status_code == 401

def test_predict_returns_chance_between_zero_and_one():
    response = requests.post("http://localhost:3000/predict", json={"input_data": FEATURES},
                             headers={"Authorization": "Bearer token"})
    assert response.status_code == 200

def test_predict_rejects_invalid_feature_values():
    response = requests.post("http://localhost:3000/predict", json={"input_data": {"GRE Score": 400}})
    assert response.status_code == 400
'''


def test_471639_constante_imbriquee_dans_le_corps_positif():
    """Le corps valide est écrit avec FEATURES dans l'enveloppe input_data."""
    assert bentoml_runner.corps_documentes_par_les_tests(TESTS_471639) == [
        {"input_data": FEATURES_471639}]


def test_471639_corps_positif_mesure_par_la_sonde(tmp_path, monkeypatch):
    runner = copie_462113(tmp_path, TESTS_471639)
    brancher_service(monkeypatch, lambda corps: corps == {"input_data": FEATURES_471639})
    resultats = runner._run_api_tests("http://localhost:32768")
    assert resultats["predict_passed"], resultats
    assert resultats["corps_source"] == "tests", resultats


def test_constante_imbriquee_dans_une_constante_du_module():
    texte = '''
FEATURES = {"gre_score": 320, "cgpa": 9}
BODY = {"input_data": FEATURES}
def test_predict():
    response = client.post("/predict", json=BODY)
    assert response.status_code == 200
'''
    assert bentoml_runner.corps_documentes_par_les_tests(texte) == [
        {"input_data": {"gre_score": 320, "cgpa": 9}}]


def test_constante_imbriquee_d_un_test_de_refus_ne_documente_rien():
    texte = '''
FEATURES = {"gre_score": 400, "cgpa": 9}
def test_predict_invalide():
    response = client.post("/predict", json={"input_data": FEATURES})
    assert response.status_code == 400
'''
    assert bentoml_runner.corps_documentes_par_les_tests(texte) == []


def test_expressions_non_litterales_ne_sont_jamais_executees(tmp_path):
    marqueur = tmp_path / "ne_doit_pas_exister"
    expressions = ["INCONNU", "FEATURES.copy()", "FEATURES.inconnu",
                   f"__import__('pathlib').Path({str(marqueur)!r}).touch()"]
    for expression in expressions:
        texte = f'''
FEATURES = {{"gre_score": 320}}
BODY = {{"input_data": {expression}}}
def test_predict():
    response = client.post("/predict", json={{"input_data": {expression}}})
    assert response.status_code == 200
'''
        assert bentoml_runner.corps_documentes_par_les_tests(texte) == []
    assert not marqueur.exists()
