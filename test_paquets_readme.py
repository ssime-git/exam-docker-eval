"""Les paquets que le README fait installer avant `pytest -v` sont installés
dès le premier passage : suivre la copie n'est pas la réparer.

    uv run --with pytest pytest -q test_paquets_readme.py

466697 (rejeu Docker du 16/10) : tests/test_requests.py importe requests,
absent du requirements.txt (un pip freeze avec pyjwt). Le README, dont les
commandes sont « strictes et ordonnées » (énoncé 1.6), fait jouer :

    pip install -r requirements.txt
    pip install pytest requests
    pytest tests/ -v

Le moteur ne passait que `--with-requirements` : collecte en erreur (code 2,
« No module named 'requests' »), imputée à la copie, taux pytest à 0. Avec
les commandes du README : 8/8.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner  # noqa: E402
from docker_eval.bentoml_runner import BentoMLRunner  # noqa: E402

BASE = "http://localhost:32771"

README_466697 = """# Admission Prediction Service

## 3. Install test dependencies

```sh
pip install -r requirements.txt
pip install pytest requests
```

## 4. Run the tests

```sh
pytest tests/ -v
```
"""

TESTS_466697 = """import jwt
import requests

BASE_URL = "http://localhost:3000"


def test_login():
    assert requests.post(f"{BASE_URL}/login").status_code == 200
"""

ERREUR_COLLECTE = ("ERROR collecting tests/test_requests.py\n"
                   "E   ModuleNotFoundError: No module named 'requests'\n"
                   "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
                   "1 error in 0.23s")


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


def copie(tmp_path, monkeypatch, readme):
    ecrire(tmp_path / "requirements.txt", "pyjwt==2.15.1\nnumpy==2.3.3\n")
    ecrire(tmp_path / "tests" / "test_requests.py", TESTS_466697)
    if readme is not None:
        ecrire(tmp_path / "README.md", readme)
    runner = BentoMLRunner("466697", str(tmp_path), 300, logging.getLogger("t"))
    runner._env_required_by_image = lambda: {}
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    passages = []

    def faux_run(commande, **kwargs):
        if commande[0] == "docker":
            return _Resultat(0, "")
        passages.append(list(commande))
        if "requests" not in bentoml_runner._paquets_avec(commande):
            junit(commande, 1, erreurs=1)
            return _Resultat(2, ERREUR_COLLECTE)
        junit(commande, 8)
        return _Resultat(0, "8 passed")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    return runner, passages


def test_466697_paquets_du_readme_installes_tel_que_livre(tmp_path, monkeypatch):
    runner, passages = copie(tmp_path, monkeypatch, README_466697)
    resultat = runner._run_pytest(BASE)
    premier = passages[0]
    avant = premier[:bentoml_runner._position_pytest(premier)]
    assert "--with-requirements" in avant
    assert set(bentoml_runner._paquets_avec(premier)) == {"pytest", "requests"}, premier
    # Rien d'autre de notre outillage : ni httpx, ni pyjwt injectés.
    tql = resultat["pytest_tel_que_livre"]
    assert tql["passe"] is True and tql["passed"] == 8 and tql["total"] == 8, tql
    assert "--with requests" in tql["commande"], tql
    assert tql["paquets_readme"] == ["pytest", "requests"], tql
    assert resultat["reparations"] == [] and len(passages) == 1, resultat["reparations"]


def test_sans_commande_d_installation_le_paquet_reste_une_reparation(tmp_path, monkeypatch):
    readme = "# Service\n\n```sh\npip install -r requirements.txt\npytest tests/ -v\n```\n"
    runner, passages = copie(tmp_path, monkeypatch, readme)
    resultat = runner._run_pytest(BASE)
    assert bentoml_runner._paquets_avec(passages[0]) == []
    tql = resultat["pytest_tel_que_livre"]
    assert tql["passe"] is False and tql["imputable"] == "copie", tql
    reparations = {r["id"]: r for r in resultat["reparations"]}
    assert set(reparations) == {"paquets_non_declares"}, resultat["reparations"]
    assert "requests" in reparations["paquets_non_declares"]["applique"]


def test_lecture_des_commandes_pip_du_readme(tmp_path):
    ecrire(tmp_path / "README.md", """# X

```bash
python -m pip install --upgrade pip
pip3 install -U "httpx>=0.27" pytest-asyncio \\
    requests==2.32.3
uv pip install -r requirements-dev.txt
pip install -e .
pip install -r requirements.txt
```

Pour installer : pip install ne se lit pas dans la prose.
""")
    ecrire(tmp_path / "requirements-dev.txt", "pytest\n")
    paquets, fichiers = bentoml_runner._installations_du_readme(str(tmp_path))
    assert paquets == ["httpx>=0.27", "pytest-asyncio", "requests==2.32.3"], paquets
    # requirements.txt n'existe pas ici : seuls les fichiers livrés comptent.
    assert fichiers == [str(tmp_path / "requirements-dev.txt")], fichiers
