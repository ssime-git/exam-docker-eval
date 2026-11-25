"""471621 : le `.env` livré n'était pas lu.

auth.py fait `os.environ["JWT_SECRET_KEY"]` et `os.environ["JWT_ALGORITHM"]`
à l'import ; le README lance le conteneur avec `--env-file .env`, et la copie
livre ce `.env`. Le moteur ne lisait que .py/.md/.txt/.yaml : faute de valeur
déclarée, il tirait les deux au hasard. `JWT_ALGORITHM=<hex>` fait lever
`jwt.encode` (/login en 500) et le secret aléatoire diffère de celui que les
tests lisent dans le même `.env` (/predict en 401). Rejoué selon le README,
le service passe ses 10 tests.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval.bentoml_runner import BentoMLRunner

AUTH_471621 = '''
import os
from dotenv import load_dotenv
load_dotenv()
JWT_SECRET_KEY = os.environ["JWT_SECRET_KEY"]
JWT_ALGORITHM = os.environ["JWT_ALGORITHM"]
'''

README_471621 = """# Admission Prediction Service

Create a `.env` file containing the same `JWT_SECRET_KEY` and `JWT_ALGORITHM` values used by the API and tests.

```bash
docker run -d --name admission-api -p 3000:3000 --env-file .env admission_prediction_service:latest
```
"""


def copie(tmp_path, env_livre=True):
    partie = tmp_path / "mandatory_part"
    (partie / "tests").mkdir(parents=True)
    (partie / "README.md").write_text(README_471621, encoding="utf-8")
    if env_livre:
        (partie / ".env").write_text("JWT_SECRET_KEY=cle-de-la-copie-471621\nJWT_ALGORITHM=HS256\n", encoding="utf-8")
    (partie / "tests" / "test_endpoints.py").write_text("def test_a():\n    assert True\n")
    runner = BentoMLRunner("471621", str(tmp_path), 300, logging.getLogger("t"))
    runner.image_name = "eval_471621/admission_prediction_service:latest"
    runner._read_service_from_image = lambda: [
        ("/home/bentoml/bento/src/src/auth.py (dans l'image)", AUTH_471621)]
    return runner


def test_le_env_livre_fournit_les_valeurs_du_conteneur(tmp_path):
    env = copie(tmp_path)._env_required_by_image()
    assert env == {"JWT_SECRET_KEY": "cle-de-la-copie-471621", "JWT_ALGORITHM": "HS256"}


def test_sans_env_livre_les_valeurs_restent_generees(tmp_path):
    runner = copie(tmp_path, env_livre=False)
    env = runner._env_required_by_image()
    assert set(env) == {"JWT_SECRET_KEY", "JWT_ALGORITHM"}
    assert runner._env_genere == {"JWT_SECRET_KEY", "JWT_ALGORITHM"}
