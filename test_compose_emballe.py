"""Un rendu emballé dans un dossier n'est pas un rendu sans Compose
(scriptorium, copie 466056).

466056 livrait un projet Prometheus complet et conforme — docker-compose.yml,
deployment/, src/, Makefile — mais dans `projet/`, sous le dossier que
pi-corrector déroule déjà. Le moteur ne cherchait le Compose qu'à la racine :
« not found », évaluation interrompue, ticket Bloqué pour une affaire de zip.
Règle : la racine d'abord ; sinon un seul dossier candidat (deux niveaux au
plus) est le projet, et l'évaluation en repart ; plusieurs candidats, on ne
choisit pas.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import compose_runner
from docker_eval.compose_runner import ComposeRunner, dossier_du_compose

COMPOSE = "services:\n  api:\n    image: alpine\n"


def ecrire(chemin, contenu=""):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def test_le_compose_a_la_racine_gagne(tmp_path):
    ecrire(tmp_path / "docker-compose.yml", COMPOSE)
    ecrire(tmp_path / "deployment" / "docker-compose.yml", COMPOSE)
    assert dossier_du_compose(str(tmp_path)) == (str(tmp_path), [])


def test_un_dossier_d_emballage_est_le_projet(tmp_path):
    """Le cas 466056 : tout le projet sous projet/."""
    projet = tmp_path / "projet"
    ecrire(projet / "docker-compose.yml", COMPOSE)
    ecrire(projet / "deployment" / "prometheus" / "prometheus.yml")
    ecrire(projet / "src" / "api" / "main.py")
    ecrire(tmp_path / "__MACOSX" / "projet" / "docker-compose.yml", COMPOSE)
    assert dossier_du_compose(str(tmp_path)) == (str(projet), ["projet"])


def test_deux_niveaux_au_plus(tmp_path):
    ecrire(tmp_path / "a" / "b" / "compose.yaml", COMPOSE)
    assert dossier_du_compose(str(tmp_path))[0] == str(tmp_path / "a" / "b")
    profond = tmp_path / "x"
    ecrire(profond / "a" / "b" / "c" / "compose.yaml", COMPOSE)
    assert dossier_du_compose(str(profond)) == (None, [])


def test_un_compose_imbrique_dans_le_projet_n_est_pas_un_autre_projet(tmp_path):
    projet = tmp_path / "projet"
    ecrire(projet / "docker-compose.yml", COMPOSE)
    ecrire(projet / "deployment" / "docker-compose.yml", COMPOSE)
    assert dossier_du_compose(str(tmp_path)) == (str(projet), ["projet"])


def test_plusieurs_projets_on_ne_choisit_pas(tmp_path):
    ecrire(tmp_path / "v1" / "docker-compose.yml", COMPOSE)
    ecrire(tmp_path / "v2" / "docker-compose.yml", COMPOSE)
    dossier, candidats = dossier_du_compose(str(tmp_path))
    assert dossier is None and candidats == ["v1", "v2"]


class _Arret(Exception):
    pass


def _runner(tmp_path, monkeypatch):
    def compose_factice(**kwargs):
        # Arrête l'exécution juste après la résolution : on observe le contexte.
        _runner.contexte = kwargs.get("context")
        raise _Arret("arrêt du contrôle après la résolution")
    monkeypatch.setattr(compose_runner, "DockerCompose", compose_factice)
    logger = logging.getLogger("test_compose_emballe")
    return ComposeRunner("466056", str(tmp_path), timeout=60, logger=logger)


def test_l_evaluation_repart_du_dossier_du_projet(tmp_path, monkeypatch):
    """Le contexte de build et la suite de l'évaluation partent de projet/, et
    le détour est consigné en étape pour le relecteur."""
    projet = tmp_path / "projet"
    ecrire(projet / "docker-compose.yml", COMPOSE)
    runner = _runner(tmp_path, monkeypatch)
    runner.run_evaluation()
    assert _runner.contexte == str(projet)
    assert runner.eval_dir == str(projet)
    titres = [e.get("title") or e.get("titre") for e in runner.steps]
    assert "Dossier du projet" in titres
    etape = runner.steps[titres.index("Dossier du projet")]
    sortie = etape.get("output") or etape.get("sortie")
    assert "projet" in sortie and (etape.get("exit_code") == 0)
    assert not any("not found" in str(e.get("output") or e.get("sortie")) for e in runner.steps)


def test_projet_ambigu_l_evaluation_le_dit(tmp_path, monkeypatch):
    ecrire(tmp_path / "v1" / "docker-compose.yml", COMPOSE)
    ecrire(tmp_path / "v2" / "docker-compose.yml", COMPOSE)
    runner = _runner(tmp_path, monkeypatch)
    resultat = runner.run_evaluation()
    assert "projet ambigu" in str(resultat.get("error")), resultat
