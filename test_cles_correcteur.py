"""Les clés du correcteur n'atteignent jamais le code de l'apprenant.

Le moteur est lancé par pi-corrector, dont l'environnement porte les clés des
fournisseurs LLM (GROQ_API_KEY, LANGSMITH_API_KEY, LIORA_API_KEY…).
testcontainers lance `docker compose` sans `env=` : compose héritait de tout
`os.environ`, et un compose d'apprenant qui écrit `${GROQ_API_KEY}` recevait
la vraie clé. Les examens LLM (LangChain, RAGOps) en contiennent justement.

Règle : à l'ouverture d'un runner, `os.environ` ne garde que ce dont Docker,
compose et uv ont besoin, plus les conventions EXAM_* de l'examen. Le reste
est mis de côté : seuls le crochet d'agent et l'investigateur le relisent.

    uvx --with testcontainers --with docker --with requests --with pyyaml --with pytest \
        python -m pytest test_cles_correcteur.py
"""
import logging
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval.compose_runner import ComposeRunner  # noqa: E402

SENTINELLE = "cle-sentinelle-du-correcteur-3f9a"
CLES = ("GROQ_API_KEY", "LANGSMITH_API_KEY", "LIORA_API_KEY", "CEREBRAS_API_KEY",
        "GOOGLE_AI_STUDIO_API_KEY", "PI_CORRECTOR_VERIFY_API_KEY")

COMPOSE = """services:
  fuite:
    image: busybox:1.36
    environment:
      GROQ: "${GROQ_API_KEY:-absente}"
      LANGSMITH: "${LANGSMITH_API_KEY:-absente}"
      LIORA: "${LIORA_API_KEY:-absente}"
      PROJET: "${COMPOSE_PROJECT_NAME:-absent}"
    volumes:
      - ./sortie:/sortie
    command: ["sh", "-c", "env > /sortie/env.txt; sleep 300"]
"""


@pytest.fixture
def cles(monkeypatch):
    for nom in CLES:
        monkeypatch.setenv(nom, SENTINELLE)
    # Le runner réécrit os.environ : monkeypatch restaure l'état d'avant.
    for nom in list(os.environ):
        monkeypatch.setenv(nom, os.environ[nom])
    yield


docker_absent = shutil.which("docker") is None or subprocess.run(
    ["docker", "info"], capture_output=True).returncode != 0


@pytest.mark.skipif(docker_absent, reason="démon Docker requis")
def test_compose_de_la_copie_ne_recoit_pas_les_cles(tmp_path, cles):
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    (tmp_path / "sortie").mkdir()
    runner = ComposeRunner("cles-test", str(tmp_path), 120, logging.getLogger("cles"))
    try:
        runner.run_evaluation()
    finally:
        runner.cleanup(force=True)
    vu = (tmp_path / "sortie" / "env.txt").read_text()
    assert SENTINELLE not in vu, vu
    assert "GROQ=absente" in vu and "LANGSMITH=absente" in vu
    # Ce dont compose a besoin passe toujours : le nom de projet isolé.
    assert "PROJET=eval_cles-test" in vu


def test_ouverture_retire_les_cles_et_garde_le_necessaire(tmp_path, cles, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("EXAM_STACK_MODE", "auto")
    monkeypatch.setenv("PI_CORRECTOR_DOCKER_HOST", "host.docker.internal")
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    ComposeRunner("cles-test", str(tmp_path), 120, logging.getLogger("cles"))
    for nom in CLES:
        assert nom not in os.environ, nom
    assert os.environ["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert os.environ["EXAM_STACK_MODE"] == "auto"
    assert os.environ["PI_CORRECTOR_DOCKER_HOST"] == "host.docker.internal"
    assert os.environ.get("PATH")
    # Un sous-processus du moteur (bentoml build, pytest de la copie) non plus.
    sortie = subprocess.run(["env"], capture_output=True, text=True).stdout
    assert SENTINELLE not in sortie


def test_le_crochet_et_l_investigateur_relisent_les_cles_mises_de_cote(tmp_path, cles):
    from docker_eval import environnement
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    ComposeRunner("cles-test", str(tmp_path), 120, logging.getLogger("cles"))
    assert environnement.variable_correcteur("LIORA_API_KEY") == SENTINELLE
    assert environnement.environnement_correcteur()["GROQ_API_KEY"] == SENTINELLE
