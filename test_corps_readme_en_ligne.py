"""Le corps /predict que la copie documente en ligne dans son README.

    uv run --with pytest pytest -q test_corps_readme_en_ligne.py

468775 : le README écrit, hors de tout bloc de code et de tout `curl -d` :
`POST /predict, header Authorization: Bearer <token>, body: {"input_data":
{...}} returns {"prediction": 0.xx}`. Le moteur lisait les identifiants dans
ce README mais pas le corps : ses quatre corps devinés (à plat, ou enveloppés
dans `request`) ont reçu 400, et /predict a été noté en échec.
"""
import json
import logging
import os
import sys
import types

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner  # noqa: E402
from docker_eval.bentoml_runner import BentoMLRunner  # noqa: E402

# Absent avant le correctif : la reproduction échoue sur les assertions,
# pas à l'import.
source_du_corps = getattr(bentoml_runner, "source_du_corps", lambda contrat, corps: None)

README_468775 = """# Examen BentoML
Admission Prediction Service

Endpoints: POST /login and POST /predict (JWT protected).

3. Run the tests (terminal 2)
        bash
        pip install -r requirements.txt
        pytest -v

API

POST /login,
    body: {"credentials": {"username": "user123", "password": "password123"}} returns {"token": "..."}
POST /predict,
    header Authorization: Bearer <token>, body: {"input_data": {"gre_score": 320, "toefl_score": 110, "university_rating": 4, "sop": 4.0, "lor": 4.0, "cgpa": 9.0, "research": 1}} returns {"prediction": 0.xx}
"""

CORPS_468775 = {"input_data": {"gre_score": 320, "toefl_score": 110, "university_rating": 4,
                               "sop": 4.0, "lor": 4.0, "cgpa": 9.0, "research": 1}}


def ecrire(chemin, contenu=""):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def runner_sur(tmp_path, readme):
    racine = tmp_path / "examen_bentoml_ready_for_submition"
    ecrire(racine / "README.md", readme)
    ecrire(racine / "tests" / "test_api.py", "import requests\n\ndef test_x():\n    assert True\n")
    runner = BentoMLRunner("468775", str(tmp_path), 300, logging.getLogger("t"))
    # Rien dans l'image (le journal de 468775 ne montre aucune lecture).
    runner._read_service_from_image = lambda: []
    return runner


class _Reponse:
    def __init__(self, code, donnee=None):
        self.status_code, self._donnee = code, donnee
        self.cookies, self.headers = {}, {}

    def json(self):
        return self._donnee


def service_double(accepte):
    """Un service BentoML qui ne rend 200 sur /predict que pour un corps
    accepté par `accepte(corps)`, 400 sinon (validation BentoML), 401 sans
    jeton valide."""
    def post(url, json=None, headers=None, **_):
        if url.endswith("/login"):
            cible = (json or {}).get("credentials", {})
            if cible.get("username") == "user123" and cible.get("password") == "password123":
                return _Reponse(200, {"token": "eyJhbGciOiJIUzI1NiJ9.e30.signature-valide"})
            return _Reponse(401 if cible else 400, {})
        if (headers or {}).get("Authorization") != "Bearer eyJhbGciOiJIUzI1NiJ9.e30.signature-valide":
            return _Reponse(401, {})
        return _Reponse(200, {"prediction": 0.72}) if accepte(json) else _Reponse(400, {"error": "validation"})

    def request(methode, url, json=None, headers=None, **_):
        return post(url, json=json, headers=headers)
    return post, request


def brancher(monkeypatch, post, request):
    # Module entier remplacé : d'autres fichiers de test remplacent déjà
    # `bentoml_runner.requests` par un espace de noms sans `post`.
    monkeypatch.setattr(bentoml_runner, "requests", types.SimpleNamespace(
        post=post, request=request, exceptions=requests.exceptions))


def test_le_corps_en_ligne_du_readme_passe_en_premier(tmp_path):
    contrat = runner_sur(tmp_path, README_468775)._discover_api_contract()
    assert contrat["predict_bodies"][0] == CORPS_468775, contrat["predict_bodies"]
    assert source_du_corps(contrat, CORPS_468775) == "readme"
    # Le corps du login, juste au-dessus, n'est jamais pris pour celui de /predict.
    assert all("credentials" not in corps for corps in contrat["predict_bodies"])


def test_468775_predict_valide_avec_le_corps_documente(tmp_path, monkeypatch):
    runner = runner_sur(tmp_path, README_468775)
    post, request = service_double(lambda corps: corps == CORPS_468775
                                   or (isinstance(corps, dict) and "gre_score" in corps.get("input_data", {})))
    brancher(monkeypatch, post, request)
    resultats = runner._run_api_tests("http://localhost:32770")
    assert resultats["login_passed"] and resultats["predict_passed"], resultats
    assert resultats["corps_source"] == "readme", resultats
    # Les sondes de sécurité et de contrat sont jouées.
    assert resultats["securite"]["predict_sans_token"] == 401
    assert resultats["contrat"]["prediction"] == 0.72


def test_sans_corps_documente_l_enveloppe_input_data_est_essayee(tmp_path, monkeypatch):
    """Ni corps documenté ni service lisible : les corps à plat échouent,
    l'enveloppe au nom courant du paramètre BentoML (`input_data`) passe.
    Le corps reste deviné."""
    readme = README_468775.split("POST /predict,")[0]
    assert "input_data" not in readme
    runner = runner_sur(tmp_path, readme)
    post, request = service_double(lambda corps: isinstance(corps, dict) and set(corps) == {"input_data"}
                                   and "gre_score" in corps["input_data"])
    brancher(monkeypatch, post, request)
    resultats = runner._run_api_tests("http://localhost:32770")
    assert resultats["predict_passed"], resultats
    assert resultats["corps_source"] == "devine", resultats


def test_echec_avec_le_corps_documente_est_attribue_a_la_copie(tmp_path, monkeypatch):
    """Tout échoue : la provenance du premier corps essayé est exposée, pour
    que le barème distingue une faute de la copie d'un corps deviné."""
    runner = runner_sur(tmp_path, README_468775)
    post, request = service_double(lambda corps: False)
    brancher(monkeypatch, post, request)
    resultats = runner._run_api_tests("http://localhost:32770")
    assert not resultats["predict_passed"]
    assert resultats["corps_source"] == "readme", resultats


def test_corps_curl_toujours_lu_et_source_readme(tmp_path):
    readme = ("curl -X POST http://localhost:3000/predict -H 'Authorization: Bearer $T' "
              "-d '{\"gre_score\": 337, \"cgpa\": 9.65}'\n")
    contrat = runner_sur(tmp_path, readme)._discover_api_contract()
    assert contrat["predict_bodies"][0] == {"gre_score": 337, "cgpa": 9.65}
    assert source_du_corps(contrat, contrat["predict_bodies"][0]) == "readme"
    assert source_du_corps(contrat, contrat["predict_bodies"][-1]) == "devine"


def test_corps_reconstruit_depuis_le_service_n_est_pas_documente(tmp_path):
    service = (
        "import bentoml\nfrom pydantic import BaseModel\n\n"
        "class Features(BaseModel):\n    gre_score: float\n    cgpa: float\n\n"
        "@bentoml.service\nclass S:\n    @bentoml.api\n"
        "    def predict(self, input_data: Features) -> dict:\n        return {}\n"
    )
    ecrire(tmp_path / "src" / "service.py", service)
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    contrat = runner._discover_api_contract()
    assert source_du_corps(contrat, contrat["predict_bodies"][0]) == "source"
    assert json.dumps(contrat["predict_wrappers"][0]) == '"input_data"'


def test_un_helper_predict_dans_les_tests_n_empeche_pas_de_lire_l_image(tmp_path):
    """Le service se lit dans l'image quand le rendu n'en livre pas. Un
    `def predict(` dans un fichier de test (helper qui appelle l'API) n'est
    pas le service : il bloquait la lecture de l'image."""
    racine = tmp_path / "rendu"
    ecrire(racine / "README.md", "docker load -i admission_prediction_service.tar\n")
    ecrire(racine / "tests" / "test_api.py",
           "import requests\n\ndef predict(token, payload):\n"
           "    return requests.post(URL + '/predict', json=payload, headers={'Authorization': token})\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    service = (
        "import bentoml\nfrom pydantic import BaseModel\n\n"
        "class AdmissionInput(BaseModel):\n    gre_score: float\n    cgpa: float\n\n"
        "@bentoml.service\nclass S:\n    @bentoml.api\n"
        "    def predict(self, input_data: AdmissionInput) -> dict:\n        return {}\n"
    )
    lus = []

    def image():
        lus.append(True)
        return [("/home/bentoml/bento/src/service.py (dans l'image)", service)]
    runner._read_service_from_image = image
    contrat = runner._discover_api_contract()
    assert lus, "le service n'a pas été lu dans l'image"
    assert contrat["predict_wrappers"][0] == "input_data", contrat["predict_wrappers"]
    assert source_du_corps(contrat, contrat["predict_bodies"][0]) == "image"
