"""Une réponse HTTP 200 JSON ne prouve pas qu'une prédiction a été produite."""
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner, premier_nombre


@pytest.mark.parametrize("response", [
    {"error": "Access denied", "status": 401},
    {"detail": "Access denied", "status_code": 403},
    {"result": {"errors": ["Invalid input"], "code": 422}},
    {"status": 200},
    {"code": 200, "message": "Ready"},
    {"error": "Prediction failed", "prediction": 0.7},
])
def test_error_or_status_is_not_a_prediction(response):
    assert premier_nombre(response) is None


@pytest.mark.parametrize("response, expected", [
    ({"status": 200, "prediction": [0.7]}, 0.7),
    ({"code": 200, "result": {"chance": 0.0}}, 0.0),
    ({"error": None, "estimate": -0.1}, -0.1),
    ([0.6], 0.6),
    (0.4, 0.4),
    ({"error": False, "status": 200, "result": {"chance": 0.3}}, 0.3),
    ({"errors": [], "prediction": [0.5], "other": 0.9}, 0.5),
])
def test_prediction_remains_independent_of_field_name(response, expected):
    assert premier_nombre(response) == expected


class Response:
    status_code = 200
    cookies = {}
    headers = {}
    text = ""

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_http_200_error_does_not_validate_predict(tmp_path, monkeypatch):
    runner = BentoMLRunner("synthetic", str(tmp_path), 60, logging.getLogger("test"))
    monkeypatch.setattr(runner, "_discover_api_contract", lambda: {
        "usernames": ["demo"], "passwords": ["example-password"],
        "login_wrappers": [None], "predict_wrappers": [None],
        "predict_bodies": [{"feature": 1}],
    })

    def post(url, **kwargs):
        return Response({"token": "synthetic-access-token"} if url.endswith("/login")
                        else {"error": "Access denied", "status": 401})

    monkeypatch.setattr(bentoml_runner, "requests", SimpleNamespace(post=post))
    result = runner._run_api_tests("http://service.invalid")
    assert result["login_passed"] is True
    assert result["predict_attempt_statuses"] == [200]
    assert result["predict_passed"] is False
    assert result["all_passed"] is False
    assert "contrat" not in result


def test_median_error_does_not_replace_nominal_prediction(tmp_path, monkeypatch):
    runner = BentoMLRunner("synthetic", str(tmp_path), 60, logging.getLogger("test"))
    monkeypatch.setattr(bentoml_runner, "requests", SimpleNamespace(
        request=lambda *a, **kw: Response({"error": "Prediction failed", "status": 500})))
    result = runner._sonder_securite_et_contrat(
        "http://service.invalid", {"username": "demo", "password": "example-password"},
        {"cgpa": 9.0}, {"Authorization": "Bearer synthetic-token"}, {"prediction": 0.8})
    assert result["contrat"]["prediction"] == 0.8
    # Le contrat HTTP des sondes reste explicite : aucun refus JSON inventé.
    assert result["securite"]["predict_sans_token"] == 200
