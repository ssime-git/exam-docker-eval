"""La cause d'un `bentoml build` raté doit atteindre le résultat (log et JSON).

Le conteneur de construction envoie tout son stdout (les phases, dont
`bentoml build`) ; `docker run` écrit le pull de l'image sur stderr. Concaténés
puis coupés à 400 caractères, il ne restait que le pull : l'erreur de
`bentoml build` disparaissait.

Lancer depuis ce repertoire :

    uv run --frozen --with pytest pytest -q test_build_bento_erreur_visible.py
"""
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import docker_eval.bentoml_runner as module
from docker_eval.bentoml_runner import BentoMLRunner

ERREUR_BENTOML = "ERROR: Failed to import module \"src.service\": No module named 'pandas'"


def test_erreur_du_bentoml_build_dans_le_resultat(tmp_path, monkeypatch):
    source = tmp_path / "examen"
    source.mkdir()
    (source / "bentofile.yaml").write_text("service: src.service:Svc\n", encoding="utf-8")
    runner = BentoMLRunner("copie-test", str(tmp_path), 1800, logging.getLogger("bento"))

    stdout = (
        "--- install bentoml\n--- install bentoml: exit_code=0\n"
        "--- bentoml build\n" + ERREUR_BENTOML + "\n"
        "--- bentoml build: exit_code=1\n"
    )
    # Pull de l'image de construction : plus long que la limite d'affichage.
    stderr = "Unable to find image 'python:3.11-slim' locally\n" + "".join(
        f"{n:x}a4f: Pulling fs layer\n" for n in range(60))

    def faux_docker(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(module.subprocess, "run", faux_docker)

    resultat = runner._build_bento_from_source(str(source))

    assert resultat["success"] is False
    assert ERREUR_BENTOML in resultat["error"], resultat["error"]
    assert ERREUR_BENTOML in runner.steps[-1]["output"]
    assert runner.steps[-1]["exit_code"] == 1
