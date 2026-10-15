"""Sondes de sécurité et de contrat bentoml (grille du 15/10).

Les sondes nominales ne prouvaient que « ça répond » : une API sans aucune
vérification de token avait la même note qu'une API sécurisée. Sur la forme
de requête qui a réussi, on relève ce que l'API refuse et ce que /predict rend.
"""
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner, premier_nombre


class _Reponse:
    def __init__(self, code):
        self.status_code = code


def test_premier_nombre():
    assert premier_nombre({"prediction": [0.75]}) == 0.75
    assert premier_nombre({"ok": True, "result": {"chance": 0.4}}) == 0.4
    assert premier_nombre({"message": "texte"}) is None


def test_les_refus_et_le_contrat_sont_releves_sur_la_forme_qui_a_reussi(tmp_path, monkeypatch):
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    vus = []

    def faux_request(methode, url, timeout=None, json=None, headers=None):
        vus.append((url.rsplit("/", 1)[-1], json, headers or {}))
        if url.endswith("/login"):
            return _Reponse(401 if json["credentials"]["password"] != "ok" else 200)
        auth = (headers or {}).get("Authorization", "")
        if auth != "Bearer jeton-valide-123":
            return _Reponse(401)
        return _Reponse(422 if json["input_data"]["cgpa"] == "pas-un-nombre" else 200)
    # Un module entier, pas un attribut : un autre test remplace déjà
    # bentoml_runner.requests par un espace de noms.
    monkeypatch.setattr(bentoml_runner, "requests", SimpleNamespace(request=faux_request))

    resultat = runner._sonder_securite_et_contrat(
        "http://h", {"credentials": {"username": "u", "password": "ok"}},
        {"input_data": {"cgpa": 8.5, "research": 1}}, {"Authorization": "Bearer jeton-valide-123"},
        {"prediction": [0.82]})
    assert resultat["securite"] == {"predict_sans_token": 401, "predict_token_falsifie": 401,
                                    "login_mauvais_identifiants": 401}
    assert resultat["contrat"] == {"prediction": 0.82, "predict_entree_invalide": 422}
    assert resultat["identifiants_dans_la_copie"] is False
    assert any(s["title"] == "Sondes de sécurité et de contrat" for s in runner.steps
               if "title" in s) or any(s.get("titre") == "Sondes de sécurité et de contrat" for s in runner.steps)


def test_identifiants_lus_dans_la_copie(tmp_path):
    """461638 : identifiants de démonstration dans le README, comptés comme
    « non fournis » faute de credentials_source sur ce chemin du moteur."""
    (tmp_path / "README.md").write_text("Login : user123 / password123\n", encoding="utf-8")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    assert runner._identifiants_dans_la_copie({"credentials": {"username": "user123", "password": "password123"}})
    assert not runner._identifiants_dans_la_copie({"username": "admin", "password": "admin-devine"})
