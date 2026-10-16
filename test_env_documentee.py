"""Ce que la copie documente se fournit dès le premier passage : ce n'est pas
une réparation.

    uv run --with pytest pytest -q test_env_documentee.py

462853 (rejeu Docker du 16/10) : les tests lisent
`BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:3001")` et
`os.environ["API_USERNAME"]`. Le README exporte API_USERNAME, API_PASSWORD,
JWT_SECRET et API_BASE_URL avant `pytest -v`. Le skill bentoml déclare
`pi_test_env: BASE_URL,API_URL,SERVICE_URL` : API_BASE_URL n'était plus
exportée, le défaut 127.0.0.1:3001 ne répond pas (port éphémère), la fixture
boucle 60 s puis « API indisponible », 20/20 en erreur dans les deux passages,
imputés à la copie. Avec l'adresse et les variables du README : 20/20.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner  # noqa: E402
from docker_eval.bentoml_runner import BentoMLRunner  # noqa: E402

BASE = "http://localhost:32768"

TESTS_462853 = '''import os
import httpx
import jwt
import pytest

BASE_URL = os.getenv(
    "API_BASE_URL",
    "http://127.0.0.1:3001",
)


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=10) as session:
        yield session


@pytest.fixture(scope="session")
def token(client):
    return client.post("/login", json={
        "username": os.environ["API_USERNAME"],
        "password": os.environ["API_PASSWORD"],
    }).json()["access_token"]


def test_expired_token(client):
    jwt.encode({"sub": "x"}, os.environ["JWT_SECRET"], algorithm="HS256")
'''

README_462853 = '''# Examen BentoML

## Tests

```bash
export API_USERNAME="exam_user"
export API_PASSWORD="password123"
export JWT_SECRET="une-cle-secrete-de-plus-de-32-caracteres"
export API_BASE_URL="http://127.0.0.1:3001"

pytest -v tests/test_api.py
```
'''

SERVICE_462853 = '''import os
USERNAME = os.environ["API_USERNAME"]
PASSWORD = os.environ["API_PASSWORD"]
SECRET = os.environ["JWT_SECRET"]
'''

DOCUMENTEES = {"API_USERNAME": "exam_user", "API_PASSWORD": "password123",
               "JWT_SECRET": "une-cle-secrete-de-plus-de-32-caracteres"}


def ecrire(chemin, contenu=""):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


class _Resultat:
    def __init__(self, code, sortie=""):
        self.returncode, self.stdout, self.stderr = code, sortie, ""


def junit(commande, total, echecs=0, erreurs=0):
    chemin = next(a.split("=", 1)[1] for a in commande if a.startswith("--junit-xml="))
    ecrire(chemin, f'<testsuite tests="{total}" failures="{echecs}" errors="{erreurs}" skipped="0"></testsuite>')


def tel_que_livre(commande) -> bool:
    avant = commande[:bentoml_runner._position_pytest(commande)]
    return "--with" not in avant


def copie_462853(tmp_path, monkeypatch, readme=README_462853):
    ecrire(tmp_path / "requirements.txt", "bentoml==1.4.35\nPyJWT==2.10.1\npytest>=8,<9\n")
    ecrire(tmp_path / "tests" / "test_api.py", TESTS_462853)
    if readme is not None:
        ecrire(tmp_path / "README.md", readme)
    runner = BentoMLRunner("462853", str(tmp_path), 300, logging.getLogger("t"))
    runner.image_name = "eval_462853/admission_prediction_service:exam"
    runner._read_service_from_image = lambda: [("/home/bentoml/bento/src/service.py (dans l'image)",
                                                SERVICE_462853)]
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    # La convention déclarée par le skill bentoml en production.
    monkeypatch.setenv("EXAM_TEST_ENV", "BASE_URL,API_URL,SERVICE_URL")
    passages = []

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        env = kwargs.get("env") or {}
        passages.append((list(commande), dict(env)))
        # Le comportement réel des tests de 462853 face à l'environnement.
        if env.get("API_BASE_URL") != BASE:
            junit(commande, 20, erreurs=20)
            return _Resultat(1, "tests/test_api.py:84: in client\n"
                                "E   Failed: API indisponible après le délai d'attente.\n"
                                "20 errors in 61.00s")
        conteneur = runner._env_required_by_image()
        if any(env.get(nom) != valeur for nom, valeur in conteneur.items()):
            junit(commande, 20, echecs=4, erreurs=13)
            return _Resultat(1, "E   KeyError: 'API_USERNAME'\n4 failed, 3 passed, 13 errors")
        junit(commande, 20)
        return _Resultat(0, "20 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    return runner, passages


def test_462853_adresse_lue_par_les_tests_exportee_malgre_la_convention_du_skill(tmp_path, monkeypatch):
    runner, passages = copie_462853(tmp_path, monkeypatch)
    resultat = runner._run_pytest(BASE)
    for commande, env in passages:
        assert env.get("API_BASE_URL") == BASE, (commande, env.get("API_BASE_URL"))


def test_462853_variables_du_readme_fournies_tel_que_livre_sans_reparation(tmp_path, monkeypatch):
    runner, passages = copie_462853(tmp_path, monkeypatch)
    resultat = runner._run_pytest(BASE)
    premier, env_premier = passages[0]
    assert tel_que_livre(premier), premier
    for nom, valeur in DOCUMENTEES.items():
        assert env_premier.get(nom) == valeur, nom
    tql = resultat["pytest_tel_que_livre"]
    assert tql["passe"] is True and tql["passed"] == 20 and tql["total"] == 20, tql
    assert resultat["reparations"] == [], resultat["reparations"]
    # Un seul passage : rien à réparer.
    assert len(passages) == 1
    # Les valeurs restent hors du résultat exposé.
    assert "password123" not in str(resultat)


def test_adresse_perimee_de_l_hote_ne_gagne_pas(tmp_path, monkeypatch):
    """Une API_BASE_URL laissée dans l'environnement de la machine pointerait
    ailleurs que le conteneur de cette copie."""
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:3001")
    runner, passages = copie_462853(tmp_path, monkeypatch)
    runner._run_pytest(BASE)
    assert passages and all(env.get("API_BASE_URL") == BASE for _, env in passages)


def test_variables_non_documentees_restent_une_reparation(tmp_path, monkeypatch):
    """Rien dans la copie ne dit d'exporter API_USERNAME : les fournir est la
    réparation `secret_du_conteneur`, comme avant."""
    runner, passages = copie_462853(tmp_path, monkeypatch, readme="# Examen BentoML\n\npytest -v\n")
    resultat = runner._run_pytest(BASE)
    _, env_premier = passages[0]
    assert "API_USERNAME" not in env_premier
    tql = resultat["pytest_tel_que_livre"]
    assert tql["passe"] is False and tql["imputable"] == "copie", tql
    reparations = {r["id"]: r for r in resultat["reparations"]}
    assert set(reparations) == {"secret_du_conteneur"}, resultat["reparations"]
    assert "API_USERNAME" in reparations["secret_du_conteneur"]["applique"]
    assert resultat["passed"] == 20


def test_documentation_partielle_seuls_les_non_documentes_sont_reproches(tmp_path, monkeypatch):
    readme = "# Examen\n\n```bash\nexport API_USERNAME=exam_user\nexport API_PASSWORD=password123\n```\n"
    runner, passages = copie_462853(tmp_path, monkeypatch, readme=readme)
    resultat = runner._run_pytest(BASE)
    _, env_premier = passages[0]
    assert env_premier.get("API_USERNAME") == "exam_user"
    assert "JWT_SECRET" not in env_premier
    reparations = {r["id"]: r for r in resultat["reparations"]}
    applique = reparations["secret_du_conteneur"]["applique"]
    assert "JWT_SECRET" in applique and "API_USERNAME" not in applique, applique


def test_convention_du_skill_s_ajoute_aux_noms_par_defaut(tmp_path, monkeypatch):
    """Cause racine de 462853 : `pi_test_env: BASE_URL,API_URL,SERVICE_URL`
    remplaçait la liste par défaut et en retirait API_BASE_URL. La
    convention s'ajoute : chaque nom par défaut reste exporté, même quand
    les tests ne le lisent pas sous une forme reconnaissable."""
    ecrire(tmp_path / "requirements.txt", "requests==2.32.5\n")
    # Lecture sans défaut : seule la liste exportée peut la satisfaire.
    ecrire(tmp_path / "tests" / "test_api.py",
           "import os\nBASE_URL = os.environ['API_BASE_URL']\n\ndef test_a():\n    assert True\n")
    runner = BentoMLRunner("x", str(tmp_path), 300, logging.getLogger("t"))
    runner._env_required_by_image = lambda: {}
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    monkeypatch.setenv("EXAM_TEST_ENV", "BASE_URL,API_URL,SERVICE_URL,EXAM_URL")
    for nom in ("API_BASE_URL", "SERVICE_BASE_URL", "BENTO_URL"):
        monkeypatch.delenv(nom, raising=False)
    envs = []

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        envs.append(dict(kwargs.get("env") or {}))
        junit(commande, 1)
        return _Resultat(0, "1 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)

    runner._run_pytest(BASE)
    for nom in ("BASE_URL", "API_URL", "SERVICE_URL", "SERVICE_BASE_URL", "API_BASE_URL", "BENTO_URL",
                "EXAM_URL"):
        assert envs[0].get(nom) == BASE, nom
