"""Méthode de sécurisation libre (énoncé bentoml, 1.3).

« vous devrez obligatoirement créer un endpoint nommé login pour sécuriser
l'accès à l'API (vous êtes libres de choisir la méthode de sécurisation que
vous voulez) ». Le moteur n'acceptait qu'un JSON {token|access_token} rejoué
en Bearer : une copie à cookie de session ou à clé d'API ratait /login, puis
/predict et toutes les sondes en cascade, alors qu'elle respecte la consigne.
"""
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner

PROFIL = {"gre_score": 330, "toefl_score": 115, "university_rating": 5, "sop": 4.5,
          "lor": 4.5, "cgpa": 9.0, "research": 1}


class _Reponse:
    def __init__(self, code, corps=None, cookies=None, texte=None):
        self.status_code = code
        self._corps = corps
        self.cookies = cookies or {}
        self.headers = {}
        self.text = texte if texte is not None else ""

    def json(self):
        if self._corps is None:
            raise ValueError("pas du JSON")
        return self._corps


def _runner(tmp_path, monkeypatch, login, autorise):
    """Un service factice : `login(json)` rend la réponse de /login,
    `autorise(headers)` dit si /predict accepte l'accès présenté."""
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    monkeypatch.setattr(runner, "_discover_api_contract", lambda: {
        "source": "test", "usernames": ["u"], "passwords": ["ok"],
        "login_wrappers": [None], "predict_wrappers": [None], "predict_bodies": [dict(PROFIL)],
    })
    vus = []

    def requete(methode, url, timeout=None, json=None, headers=None, **_):
        headers = headers or {}
        vus.append((url.rsplit("/", 1)[-1], json, headers))
        if url.endswith("/login"):
            return login(json)
        if not autorise(headers):
            return _Reponse(401, {"detail": "refusé"})
        if isinstance(json, dict) and json.get("cgpa") == "pas-un-nombre":
            return _Reponse(422, {"detail": "invalide"})
        # Prédiction proportionnelle au CGPA : le profil sondé se lit dans la réponse.
        return _Reponse(200, {"prediction": round(json["cgpa"] / 10, 2)})

    faux = SimpleNamespace(request=requete,
                           post=lambda url, **kw: requete("POST", url, **kw))
    monkeypatch.setattr(bentoml_runner, "requests", faux)
    return runner, vus


def _login_ok(corps=None, cookies=None):
    def login(json):
        if json.get("password") != "ok":
            return _Reponse(401, {"detail": "identifiants"})
        return _Reponse(200, corps, cookies=cookies, texte="Connecté")
    return login


def test_login_par_cookie_de_session(tmp_path, monkeypatch):
    runner, _ = _runner(tmp_path, monkeypatch,
                        _login_ok(cookies={"session_id": "s3ss10n-0123456789abcdef"}),
                        lambda h: h.get("Cookie") == "session_id=s3ss10n-0123456789abcdef")
    r = runner._run_api_tests("http://h")
    assert r["login_passed"] and r["predict_passed"], r
    assert r["mecanisme_acces"] == "cookie"
    assert r["securite"]["mecanisme"] == "cookie"
    # Sans cookie, cookie altéré, mauvais mot de passe : trois refus.
    assert r["securite"]["predict_sans_token"] == 401
    assert r["securite"]["predict_token_falsifie"] == 401
    assert r["securite"]["login_mauvais_identifiants"] == 401


def test_login_par_cle_api(tmp_path, monkeypatch):
    cle = "cle-api-0123456789abcdef"
    runner, _ = _runner(tmp_path, monkeypatch, _login_ok(corps={"api_key": cle}),
                        lambda h: h.get("X-API-Key") == cle or h.get("api_key") == cle)
    r = runner._run_api_tests("http://h")
    assert r["login_passed"] and r["predict_passed"], r
    assert r["mecanisme_acces"] == "cle"
    assert r["securite"]["predict_sans_token"] == 401
    assert r["securite"]["predict_token_falsifie"] == 401


def test_jwt_inchange_et_token_type_ignore(tmp_path, monkeypatch):
    """Réponse OAuth2 : `token_type: bearer` ne doit pas être pris pour le
    jeton — on enverrait `Bearer bearer`."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.c2lnbmF0dXJlLWR1LWpldG9u"
    runner, _ = _runner(tmp_path, monkeypatch,
                        _login_ok(corps={"token_type": "bearer", "access_token": jwt}),
                        lambda h: h.get("Authorization") == f"Bearer {jwt}")
    r = runner._run_api_tests("http://h")
    assert r["login_passed"] and r["predict_passed"], r
    assert r["token"] == jwt
    assert r["mecanisme_acces"] == "jeton"
    assert r["securite"] == {"predict_sans_token": 401, "predict_token_falsifie": 401,
                             "login_mauvais_identifiants": 401, "mecanisme": "jeton"}


def test_cle_imbriquee_contenant_token(tmp_path, monkeypatch):
    """« toute clé contenant token » : {"data": {"jwt_token": ...}}."""
    jeton = "jeton-opaque-0123456789abcdef"
    runner, _ = _runner(tmp_path, monkeypatch, _login_ok(corps={"data": {"jwt_token": jeton}}),
                        lambda h: h.get("Authorization") == f"Bearer {jeton}")
    r = runner._run_api_tests("http://h")
    assert r["login_passed"] and r["predict_passed"], r


def test_prediction_sur_profil_median(tmp_path, monkeypatch):
    """Régression linéaire non bornée : la sonde de contrat part d'un profil
    médian (CGPA 8.6), pas du profil extrême qui a servi au premier appel."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.c2lnbmF0dXJlLWR1LWpldG9u"
    runner, _ = _runner(tmp_path, monkeypatch, _login_ok(corps={"token": jwt}),
                        lambda h: h.get("Authorization") == f"Bearer {jwt}")
    r = runner._run_api_tests("http://h")
    assert r["contrat"]["prediction"] == 0.86, r["contrat"]
    assert r["contrat"]["predict_entree_invalide"] == 422


def test_identifiants_du_service_ne_comptent_pas(tmp_path):
    """Les identifiants « documentés pour les tests » se lisent dans le README
    ou les tests, pas dans le code du service que le correcteur ne relance pas."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text('USERS = {"alice": "secret42"}\n', encoding="utf-8")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    assert not runner._identifiants_dans_la_copie({"username": "alice", "password": "secret42"})
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text('CREDS = ("alice", "secret42")\n', encoding="utf-8")
    assert runner._identifiants_dans_la_copie({"username": "alice", "password": "secret42"})
