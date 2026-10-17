"""451711 : jeton lu dans le corps, erreurs rendues en HTTP 200.

Le service lit `input_data.token` et répond `200 {"error": "Invalid or
expired token", "status": 401}` sans jeton valide. Le README documente
`"token": "<TOKEN_FROM_LOGIN>"`. Le moteur envoyait le placeholder tel quel,
comptait le 200 comme une prédiction réussie, et prenait 401 pour la
prédiction (premier nombre du JSON).
"""
import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner

JETON = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.signature-synthetique"

README_451711 = """# Admission Prediction Service - BentoML

```bash
curl -X POST "http://localhost:3000/login" \\
  -H "Content-Type: application/json" \\
  -d '{"username": "admin", "password": "password123"}'
```

```bash
curl -X POST "http://localhost:3000/predict" \\
  -H "Content-Type: application/json" \\
  -d '{
    "token": "<TOKEN_FROM_LOGIN>",
    "GRE_Score": 320,
    "TOEFL_Score": 110,
    "University_Rating": 3,
    "SOP": 3.5,
    "LOR": 3.5,
    "CGPA": 8.5,
    "Research": 1
  }'
```
"""


class Reponse:
    def __init__(self, code, corps):
        self.status_code, self.corps = code, corps
        self.cookies, self.headers, self.text = {}, {}, json.dumps(corps)

    def json(self):
        return self.corps


def service_451711(methode, url, json=None, headers=None, **kw):
    """Comportement observé : jeton dans `input_data.token`, refus en 200."""
    corps = json or {}
    if url.endswith("/login"):
        if corps == {"username": "admin", "password": "password123"}:
            return Reponse(200, {"token": JETON})
        return Reponse(200, {"error": "Invalid credentials", "status": 401})
    interne = corps.get("input_data")
    if not isinstance(interne, dict):
        return Reponse(400, {"error": "input_data manquant"})
    if interne.get("token") != JETON:
        return Reponse(200, {"error": "Invalid or expired token", "status": 401})
    if not isinstance(interne.get("CGPA"), (int, float)):
        return Reponse(400, {"error": "invalid input"})
    return Reponse(200, {"prediction": 0.02 * interne["CGPA"] + 0.6, "status": "success"})


def test_451711_jeton_substitue_dans_le_corps(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text(README_451711, encoding="utf-8")
    runner = BentoMLRunner("451711", str(tmp_path), 60, logging.getLogger("t"))
    monkeypatch.setattr(runner, "_read_service_from_image", lambda: [(
        "/home/bentoml/bento/src/service.py (dans l'image)",
        "class PredictInput(BaseModel):\n    token: str\n    GRE_Score: float\n\n"
        "class Svc:\n    def predict(self, input_data: PredictInput) -> dict:\n        pass\n")])

    def post(url, **kw):
        return service_451711("POST", url, **kw)

    monkeypatch.setattr(bentoml_runner, "requests",
                        SimpleNamespace(post=post, request=service_451711))
    resultat = runner._run_api_tests("http://service.invalid")
    assert resultat["login_passed"] is True
    assert resultat["predict_passed"] is True
    assert resultat["mecanisme_acces"] == "jeton_corps"
    assert 0 <= resultat["contrat"]["prediction"] <= 1
    securite = resultat["securite"]
    # Le jeton retiré ou altéré dans le corps : le service refuse (dans un
    # HTTP 200, ce que le relevé dit tel quel).
    assert securite["predict_sans_token"] == 200
    assert securite["predict_token_falsifie"] == 200
    assert set(securite["refus_dans_un_200"]) >= {"predict_sans_token", "predict_token_falsifie"}
    assert securite["login_mauvais_identifiants"] == 200
    assert "login_mauvais_identifiants" in securite["refus_dans_un_200"]


def test_jeton_dans_le_corps_introuvable_ne_se_mesure_pas(tmp_path, monkeypatch):
    runner = BentoMLRunner("451711", str(tmp_path), 60, logging.getLogger("t"))
    monkeypatch.setattr(bentoml_runner, "requests", SimpleNamespace(
        request=lambda *a, **kw: Reponse(200, {"prediction": 0.7})))
    resultat = runner._sonder_securite_et_contrat(
        "http://service.invalid", {"username": "a", "password": "b"}, {"cgpa": 8.6}, {},
        {"prediction": 0.7}, {"mecanisme": "jeton_corps", "entete": None, "valeur": JETON,
                              "entetes": {}})
    assert resultat["securite"]["predict_sans_token"] is None
    assert resultat["securite"]["predict_token_falsifie"] is None


@pytest.mark.parametrize("reponse, attendu", [
    ({"status": 401}, None),
    ({"error": "Invalid or expired token", "status": 401}, None),
    ({"detail": "Not authenticated"}, None),
    ({"status_code": 403, "message": "Forbidden"}, None),
    ({"chance_of_admit": 0.73, "model_version": "admission_lr:5v5osnghv6f6esx3"}, 0.73),
    ({"gre_score": 320, "chance": 0.7}, 0.7),
    ({"prediction": [0.81]}, 0.81),
    ({"status": 200, "result": 0.5}, 0.5),
    ({"status": "success", "prediction": 0.62}, 0.62),
    ([0.4], 0.4),
])
def test_prediction_lue_dans_un_champ_dedie(reponse, attendu):
    assert bentoml_runner.lire_prediction(reponse) == attendu
