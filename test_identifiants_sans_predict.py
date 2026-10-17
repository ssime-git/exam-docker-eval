"""460660, 464154 : identifiants non remontés quand /login ou /predict échoue.

`identifiants_dans_la_copie` n'était calculé qu'après un /predict réussi, et
le mode image seule ne remontait ni `credentials_source` ni
`credentials_in_tests`.
"""
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner


class Reponse:
    def __init__(self, code):
        self.status_code, self.cookies, self.headers, self.text = code, {}, {}, ""

    def json(self):
        return {}


README = """# Service
```bash
curl -X POST http://localhost:3000/login -H "Content-Type: application/json" \\
  -d '{"username": "admin", "password": "admin123"}'
```
"""


def test_464154_login_en_404_identifiants_mesures_quand_meme(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    runner = BentoMLRunner("464154", str(tmp_path), 60, logging.getLogger("t"))
    runner._read_service_from_image = lambda: []
    monkeypatch.setattr(bentoml_runner, "requests",
                        SimpleNamespace(post=lambda url, **kw: Reponse(404)))
    resultat = runner._run_api_tests("http://service.invalid")
    assert resultat["login_passed"] is False
    assert resultat["identifiants_dans_la_copie"] is True


def test_aucun_identifiant_connu_n_est_pas_mesure(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Service\n", encoding="utf-8")
    runner = BentoMLRunner("464154", str(tmp_path), 60, logging.getLogger("t"))
    runner._read_service_from_image = lambda: []
    monkeypatch.setattr(bentoml_runner, "requests",
                        SimpleNamespace(post=lambda url, **kw: Reponse(404)))
    resultat = runner._run_api_tests("http://service.invalid")
    assert resultat["identifiants_dans_la_copie"] is None


def test_mode_image_seule_remonte_la_source_des_identifiants(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    runner = BentoMLRunner("460660", str(tmp_path), 60, logging.getLogger("t"))
    runner.image_tar = str(tmp_path / "image.tar")

    class R:
        returncode, stdout, stderr = 0, "cid\n", ""

    monkeypatch.setattr(bentoml_runner.subprocess, "run", lambda *a, **kw: R())
    runner._load_docker_image_cli = lambda: "img"
    runner.record_exam_image = lambda image: None
    runner._platform_for_image = lambda: None
    runner._env_required_by_image = lambda: {}
    runner._service_port_from_image = lambda: 3000
    runner._published_host_port_cli = lambda port: 32772
    runner._attrs_conteneur_cli = lambda: {}
    runner._completer_environnement_image = lambda *a: None
    runner._resolve_published_host = lambda: "localhost"
    runner._wait_for_api_ready = lambda url: None

    def api(url):
        runner.credentials_source, runner.credentials_in_tests = "README.md", False
        return {"all_passed": False, "login_passed": False, "predict_passed": False}

    runner._run_api_tests = api
    runner._run_pytest = lambda url: {"executed": False}
    runner._investiguer_echecs = lambda: None
    runner.cleanup = lambda force=False: None
    resultat = runner._run_image_only_evaluation()
    assert resultat["credentials_source"] == "README.md"
    assert resultat["credentials_in_tests"] is False


def test_460660_image_non_chargee_identifiants_lus_dans_le_rendu(tmp_path):
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    runner = BentoMLRunner("460660", str(tmp_path), 60, logging.getLogger("t"))
    runner.image_tar = str(tmp_path / "image.tar")
    runner._load_docker_image_cli = lambda: None
    runner.record_exam_image = lambda image: None
    runner.cleanup = lambda force=False: None
    resultat = runner._run_image_only_evaluation()
    assert resultat["image_loaded"] is False
    assert "README.md" in resultat["credentials_source"]
    assert resultat["identifiants_dans_la_copie"] is True
