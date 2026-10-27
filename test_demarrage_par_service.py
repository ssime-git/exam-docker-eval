"""Un service en échec ne fait pas tomber la stack entière (stack servante).

`docker compose up --build --wait` est tout-ou-rien : bike-api qui plante à
l'import (ImportError pandas, 472135) faisait sortir la copie en « Execution
error » ; les services sains restaient debout et n'étaient jamais sondés.

Lancer depuis ce repertoire :

    uv run --frozen --with pytest pytest -q test_demarrage_par_service.py
"""
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import docker_eval.compose_runner as module
from docker_eval.compose_runner import ComposeRunner

COMPOSE = (
    "services:\n"
    "  api:\n    image: nginx:alpine\n    ports:\n      - \"8081:80\"\n"
    "  bike-api:\n    build: ./src/api\n"
)
LOGS_IMPORT = (
    "Traceback (most recent call last):\n"
    '  File "/app/main.py", line 2, in <module>\n'
    "ModuleNotFoundError: No module named 'pandas'\n"
)


class Conteneur:
    def __init__(self, service, attrs, logs=b""):
        self.name = f"eval-{service}-1"
        self.attrs = attrs
        self.status = attrs["State"]["Status"]
        self.labels = {"com.docker.compose.service": service, "com.docker.compose.project": "eval_copie-test"}
        self._logs = logs

    def reload(self):
        pass

    def logs(self, tail=None):
        return self._logs


def etat(status, health=None, restarts=0, ports=None, exit_code=0):
    return {
        "State": {"Status": status, "ExitCode": exit_code, "OOMKilled": False,
                  "Health": {"Status": health} if health else None},
        "RestartCount": restarts,
        "Config": {"Labels": {}, "Env": [], "Healthcheck": {"Test": ["CMD", "true"]}},
        "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(ports)}]} if ports else {}},
    }


def runner_partiel(tmp_path, monkeypatch, conteneurs):
    (tmp_path / "src/api").mkdir(parents=True)
    (tmp_path / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    runner = ComposeRunner("copie-test", str(tmp_path), 1800, logging.getLogger("partiel"))

    class Compose:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            # `up --wait` : bike-api n'atteint jamais « healthy ».
            raise subprocess.CalledProcessError(
                1, ["docker", "compose", "up", "--build", "--wait"], output=b"",
                stderr=b"dependency failed to start: container eval-bike-api-1 is unhealthy\n")

        def __exit__(self, *_a):
            return False

        def stop(self):
            pass

    sondes = []

    def sonder(url):
        sondes.append(url)
        return {"code": 200, "extrait": "ok"}

    monkeypatch.setattr(module, "DockerCompose", Compose)
    monkeypatch.setattr(module, "sonder", sonder)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner, "cleanup", lambda force=False: None)
    monkeypatch.setattr(runner, "_jouer_lancement", lambda: None)
    monkeypatch.setattr(runner, "_retenir_images_de_la_copie", lambda: None)
    monkeypatch.setattr(runner, "_images_des_services_construits", lambda: {})
    monkeypatch.setattr(runner, "_wait_for_services", lambda: None)
    monkeypatch.setattr(runner, "_completer_environnement_compose", lambda: None)
    monkeypatch.setattr(runner, "_capture_logs", lambda: "")
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: conteneurs())
    monkeypatch.delenv("LIORA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PI_CORRECTOR_INVESTIGATE_URL", raising=False)
    return runner, sondes


def conteneurs_api_sain_bike_api_casse():
    return [
        Conteneur("api", etat("running", health="healthy", ports=8081)),
        Conteneur("bike-api", etat("running", health="unhealthy", restarts=5, exit_code=1), LOGS_IMPORT.encode()),
    ]


def test_un_service_en_echec_laisse_les_sains_evalues(tmp_path, monkeypatch):
    """Avant correction : « Execution error » (score 0), api jamais sondé."""
    runner, sondes = runner_partiel(tmp_path, monkeypatch, conteneurs_api_sain_bike_api_casse)

    result = runner.run_evaluation()

    assert any(url == "http://127.0.0.1:8081/" for url in sondes), sondes
    assert result["success"] is False
    assert not str(result.get("error", "")).startswith("Execution error"), result.get("error")
    par_service = result["demarrage_par_service"]
    assert par_service["api"]["etat"] == "sain", par_service
    assert par_service["bike-api"]["etat"] == "en_echec", par_service
    assert par_service["bike-api"]["cause"] == result["cause_demarrage"]["services"]["bike-api"]["cause"]
    assert any(p["compose_service"] == "api" and p.get("code") == 200 for p in result["probes"]), result["probes"]
    etape = next(s for s in result["steps"] if s["title"] == "Build et démarrage Compose")
    assert etape["exit_code"] != 0 and "bike-api" in etape["output"], etape


def test_aucun_service_sain_garde_l_echec_du_up(tmp_path, monkeypatch):
    """Rien ne tient debout : pas d'évaluation partielle, l'échec du up reste."""
    def tout_casse():
        return [Conteneur("bike-api", etat("running", health="unhealthy", restarts=5, exit_code=1),
                          LOGS_IMPORT.encode())]
    runner, _ = runner_partiel(tmp_path, monkeypatch, tout_casse)

    result = runner.run_evaluation()

    assert result["success"] is False
    assert "demarrage_par_service" not in result
    assert str(result.get("error", "")).startswith("Execution error"), result.get("error")
