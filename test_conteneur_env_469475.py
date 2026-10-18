"""Des tests qui vont dans l'image par `docker exec` visent le conteneur du harnais.

    uv run --with pytest pytest -q test_conteneur_env_469475.py

469475 : tests/test_model.py lit os.environ.get('API_CONTAINER_NAME', 'admission-api')
(le nom du `docker run --name` du README). Le harnais nomme son conteneur
`bentoml_eval_<id>` : 2 tests sur 33 echouaient (faute du harnais, pas de la copie)
alors que la categorie « tests » etait notee au maximum.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner

from test_env_documentee import BASE, README_462853, _Resultat, copie_462853, junit


def test_469475_nom_du_conteneur_du_harnais_exporte_aux_tests(tmp_path, monkeypatch):
    runner, passages = copie_462853(tmp_path, monkeypatch)
    runner._run_pytest(BASE)
    assert passages
    for commande, env in passages:
        for nom in ("API_CONTAINER_NAME", "CONTAINER_NAME", "SERVICE_CONTAINER_NAME"):
            assert env.get(nom) == runner.container_name, (commande, nom)
