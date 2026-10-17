"""B12 : image incohérente imputée à la copie ; paire d'identifiants exigée.

460660 : `docker load` échoue sur « too many non-empty layers in History
section » (config de l'image incohérente) et l'échec restait « cause non
établie ». `_identifiants_dans_la_copie` se contentait de trouver les deux
chaînes n'importe où dans le même fichier (« admin » dans la prose).
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner


class _R:
    def __init__(self, code=0, sortie="", erreur=""):
        self.returncode, self.stdout, self.stderr = code, sortie, erreur


def test_460660_image_incoherente_est_la_faute_de_la_copie(tmp_path, monkeypatch):
    runner = BentoMLRunner("460660", str(tmp_path), 60, logging.getLogger("t"))
    runner.image_tar = str(tmp_path / "admission_prediction_service.tar")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", lambda *a, **kw: _R(
        1, "", "Error: too many non-empty layers in History section"))
    runner.record_exam_image = lambda image: None
    runner.cleanup = lambda force=False: None
    resultat = runner._run_image_only_evaluation()
    assert resultat["image_loaded"] is False
    assert resultat["fault"] == "apprenant"
    assert "History" in resultat["cause"]


def test_echec_de_chargement_ordinaire_reste_non_impute(tmp_path, monkeypatch):
    runner = BentoMLRunner("460660", str(tmp_path), 60, logging.getLogger("t"))
    runner.image_tar = str(tmp_path / "image.tar")
    monkeypatch.setattr(bentoml_runner.subprocess, "run", lambda *a, **kw: _R(
        1, "", "write /var/lib/docker/tmp: no space left on device"))
    runner.record_exam_image = lambda image: None
    runner.cleanup = lambda force=False: None
    resultat = runner._run_image_only_evaluation()
    assert "fault" not in resultat or resultat["fault"] != "apprenant"


LOGIN = {"username": "admin", "password": "s3cret-pass"}


@pytest.mark.parametrize("readme, attendu", [
    ('curl -d \'{"username": "admin", "password": "s3cret-pass"}\'\n', True),
    ("Identifiants :\n- utilisateur : admin\n- mot de passe : s3cret-pass\n", True),
    # « admin » dans la prose, le mot de passe ailleurs, sans lien.
    ("Le compte admin gère le service.\n" + "texte\n" * 20 + "Exemple : s3cret-pass\n", False),
    # Sous-chaînes d'autres mots.
    ('{"username": "administrator", "password": "s3cret-pass-2"}\n', False),
])
def test_paire_utilisateur_mot_de_passe_exigee(tmp_path, readme, attendu):
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    runner = BentoMLRunner("462000", str(tmp_path), 60, logging.getLogger("t"))
    assert runner._identifiants_dans_la_copie(LOGIN) is attendu


def test_paire_identique_exige_deux_occurrences(tmp_path):
    (tmp_path / "README.md").write_text("Connectez-vous en admin.\n", encoding="utf-8")
    runner = BentoMLRunner("462000", str(tmp_path), 60, logging.getLogger("t"))
    assert runner._identifiants_dans_la_copie({"username": "admin", "password": "admin"}) is False
    (tmp_path / "README.md").write_text('{"username": "admin", "password": "admin"}\n', encoding="utf-8")
    assert runner._identifiants_dans_la_copie({"username": "admin", "password": "admin"}) is True
