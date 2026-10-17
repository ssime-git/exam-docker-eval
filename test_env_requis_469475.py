"""469475 : variables exigées au démarrage, lues par `os.environ.get`.

Le service (security.py) lit `os.environ.get('API_USERNAME', '')`,
`API_PASSWORD`, `JWT_SECRET`, puis lève une RuntimeError si l'une manque. Le
README fait générer ces valeurs (`.env.runtime`, `--env-file`). Le moteur ne
reconnaissait que `os.environ["X"]` : conteneur lancé sans rien, mort en
code 1, « API not ready after 60s », et en mode image seule ni logs ni
imputation (« cause non établie »).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner

SECURITY_469475 = '''
import os

class Security:
    @classmethod
    def from_environment(cls):
        username = os.environ.get('API_USERNAME', '')
        password = os.environ.get('API_PASSWORD', '')
        secret = os.environ.get('JWT_SECRET', '')
        ttl = int(os.environ.get('JWT_TTL_SECONDS', '900'))
        debug = os.getenv('APP_DEBUG') or 'no'
        region = os.getenv('CLOUD_REGION')
        if not username or len(password) < 12 or len(secret) < 32:
            raise RuntimeError('Set API_USERNAME, API_PASSWORD (12+ characters), '
                               'and JWT_SECRET (32+ characters) before starting.')
'''

README_469475 = """# Admissions prediction service

Startup fails unless `API_USERNAME`, `API_PASSWORD` (at least 12 characters),
and `JWT_SECRET` (at least 32 characters) are configured. `JWT_TTL_SECONDS`
defaults to 900. `APP_DEBUG` is optional.

```bash
.venv/bin/python - <<'PY'
path.write_text('API_USERNAME=reviewer\\nAPI_PASSWORD=' + secrets.token_urlsafe(24))
PY
docker run -d -p 127.0.0.1:3000:3000 --env-file .env.runtime admission_prediction_service:latest
```

`POST /login` accepts:

```json
{"username": "reviewer", "password": "YOUR_GENERATED_PASSWORD"}
```
"""


def copie(tmp_path):
    (tmp_path / "README.md").write_text(README_469475, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text("def test_a():\n    assert True\n")
    runner = BentoMLRunner("469475", str(tmp_path), 300, logging.getLogger("t"))
    runner.image_name = "eval_469475/admission_prediction_service:latest"
    runner._read_service_from_image = lambda: [
        ("/home/bentoml/bento/src/src/security.py (dans l'image)", SECURITY_469475)]
    return runner


def test_variables_lues_par_environ_get_sans_defaut_et_documentees_sont_fournies(tmp_path):
    env = copie(tmp_path)._env_required_by_image()
    assert set(env) == {"API_USERNAME", "API_PASSWORD", "JWT_SECRET"}
    # Défaut non vide (900) : une valeur aléatoire ferait planter int().
    assert "JWT_TTL_SECONDS" not in env
    # Repli `or 'no'` : le service a sa valeur, on ne la remplace pas.
    assert "APP_DEBUG" not in env
    # Ni défaut ni mention dans la documentation : rien ne dit qu'elle est requise.
    assert "CLOUD_REGION" not in env
    assert len(env["API_PASSWORD"]) >= 12 and len(env["JWT_SECRET"]) >= 32


def test_login_essaie_d_abord_la_paire_fournie_au_conteneur(tmp_path):
    runner = copie(tmp_path)
    env = runner._env_required_by_image()
    contrat = runner._discover_api_contract()
    assert contrat["usernames"][0] == env["API_USERNAME"]
    assert contrat["passwords"][0] == env["API_PASSWORD"]
    assert "environnement" in contrat["credentials_source"]


def test_identifiants_generes_par_nous_ne_valent_pas_absence(tmp_path):
    runner = copie(tmp_path)
    env = runner._env_required_by_image()
    login = {"username": env["API_USERNAME"], "password": env["API_PASSWORD"]}
    assert runner._identifiants_dans_la_copie(login) is None


class _Resultat:
    def __init__(self, code=0, sortie=""):
        self.returncode, self.stdout, self.stderr = code, sortie, ""


def mode_image_seule(tmp_path, monkeypatch, logs, env_fourni):
    runner = copie(tmp_path)
    runner.image_tar = str(tmp_path / "image.tar")
    commandes = []

    def faux_run(commande, **kwargs):
        commandes.append(list(commande))
        return _Resultat(0, "cid123\n")

    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    runner._load_docker_image_cli = lambda: "eval_469475/admission_prediction_service:latest"
    runner.record_exam_image = lambda image: None
    runner._platform_for_image = lambda: None
    runner._env_required_by_image = lambda: env_fourni
    runner._service_port_from_image = lambda: 3000
    runner._published_host_port_cli = lambda port: 32772
    runner._attrs_conteneur_cli = lambda: {}
    runner._completer_environnement_image = lambda *a: None
    runner._resolve_published_host = lambda: "localhost"

    def jamais_pret(base_url):
        raise TimeoutError("API not ready after 60s")

    runner._wait_for_api_ready = jamais_pret
    runner._collect_diagnostics = lambda: {"container_status": "exited", "container_exit_code": 1,
                                           "qemu_available": True}
    runner._container_exit_state = lambda: (True, 1)
    runner._capture_container_logs = lambda: logs
    runner.cleanup = lambda force=False: None
    return runner, commandes


def test_mode_image_seule_garde_le_conteneur_mort_et_impute_l_echec(tmp_path, monkeypatch):
    runner, commandes = mode_image_seule(
        tmp_path, monkeypatch, "Traceback (most recent call last):\nZeroDivisionError: division by zero",
        env_fourni={"API_USERNAME": "u", "API_PASSWORD": "p" * 20, "JWT_SECRET": "s" * 40})
    resultat = runner._run_image_only_evaluation()
    run = next(c for c in commandes if c[:2] == ["docker", "run"])
    # `--rm` effaçait le conteneur mort : plus de logs ni d'état à lire.
    assert "--rm" not in run
    assert resultat["container_exited"] is True
    assert resultat["container_exit_code"] == 1
    assert resultat["fault"] == "apprenant"
    assert "ZeroDivisionError" in resultat["container_logs"]
    assert resultat["execution_mode"] == "image_only_cli"


def test_variable_documentee_non_fournie_n_est_pas_la_faute_de_la_copie(tmp_path, monkeypatch):
    runner, _ = mode_image_seule(
        tmp_path, monkeypatch,
        "RuntimeError: Set API_USERNAME, API_PASSWORD (12+ characters), and JWT_SECRET "
        "(32+ characters) before starting.", env_fourni={})
    resultat = runner._run_image_only_evaluation()
    assert resultat["fault"] == "systeme"
    assert "API_USERNAME" in resultat["cause"]
