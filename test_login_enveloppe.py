"""461573, 451711 : login enveloppé ailleurs que sous `credentials`.

La sonde « mauvais mot de passe » ne cherchait le mot de passe que sous
`credentials`. Avec `{"data": {...}}` ou `{"input_data": {...}}`, elle
renvoyait le bon mot de passe, obtenait 200 et comptait un accès accepté ;
`identifiants_dans_la_copie` passait à faux pour la même raison.
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

BON = {"username": "admin", "password": "secret123"}


class Reponse:
    def __init__(self, code, corps):
        self.status_code, self.corps = code, corps
        self.cookies, self.headers, self.text = {}, {}, json.dumps(corps)

    def json(self):
        return self.corps


def faux_service(methode, url, json=None, headers=None, **kw):
    """Refuse un mauvais mot de passe, où que le login l'enveloppe."""
    if url.endswith("/login"):
        interne = next((v for v in (json or {}).values() if isinstance(v, dict)), json or {})
        return Reponse(200 if interne == BON else 401, {"token": "x" * 40})
    jeton = (headers or {}).get("Authorization", "")
    return Reponse(200 if jeton == "Bearer " + "x" * 40 else 401, {"prediction": 0.7})


@pytest.mark.parametrize("enveloppe", ["data", "input_data", "credentials", None])
def test_sonde_mauvais_mot_de_passe_quel_que_soit_le_wrapper(tmp_path, monkeypatch, enveloppe):
    (tmp_path / "README.md").write_text(
        'curl -X POST /login -d \'{"username": "admin", "password": "secret123"}\'\n')
    runner = BentoMLRunner("461573", str(tmp_path), 60, logging.getLogger("t"))
    monkeypatch.setattr(bentoml_runner, "requests", SimpleNamespace(request=faux_service))
    login_ok = {enveloppe: dict(BON)} if enveloppe else dict(BON)
    resultat = runner._sonder_securite_et_contrat(
        "http://service.invalid", login_ok, {"cgpa": 8.6},
        {"Authorization": "Bearer " + "x" * 40}, {"prediction": 0.7})
    assert resultat["securite"]["login_mauvais_identifiants"] == 401
    assert resultat["identifiants_dans_la_copie"] is True


def test_login_sans_champ_mot_de_passe_reconnu_n_est_pas_mesure(tmp_path, monkeypatch):
    runner = BentoMLRunner("461573", str(tmp_path), 60, logging.getLogger("t"))
    appels = []

    def service(methode, url, **kw):
        appels.append(url)
        return faux_service(methode, url, **kw)

    monkeypatch.setattr(bentoml_runner, "requests", SimpleNamespace(request=service))
    resultat = runner._sonder_securite_et_contrat(
        "http://service.invalid", {"data": {"user": "admin", "pin": "1234"}}, {"cgpa": 8.6},
        {"Authorization": "Bearer " + "x" * 40}, {"prediction": 0.7})
    # Le « mauvais » corps serait identique au bon : non mesuré, pas 200.
    assert resultat["securite"]["login_mauvais_identifiants"] is None
    assert not any(url.endswith("/login") for url in appels)
