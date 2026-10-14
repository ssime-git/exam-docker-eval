"""Trace contractuelle des évaluations Compose."""
import logging
import os
import sys
import tempfile
import types
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval.compose_runner import ComposeRunner
import docker_eval.compose_runner as module


class FakeCompose:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        # Le moteur démarre lui-même (_DemarrageSansDemontage) : les doublures
        # qui simulent un échec le font dans __enter__.
        self.__enter__()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stop(self):
        pass


class FakeContainer:
    def __init__(self, name, status="running", exit_code=0, service=None, ports=None):
        self.name = name
        self.status = status
        self.labels = {"com.docker.compose.service": service} if service else {}
        self.attrs = {
            "State": {"ExitCode": exit_code},
            "NetworkSettings": {"Ports": ports or {}},
        }

    def reload(self):
        pass


@pytest.fixture
def logger():
    return logging.getLogger("compose-steps")


def make_runner(tmp_path, logger, compose_text="services:\n  nginx:\n    image: nginx\n"):
    (tmp_path / "docker-compose.yml").write_text(compose_text)
    return ComposeRunner("copie-test", str(tmp_path), 1, logger)


def no_docker_cleanup(runner, monkeypatch):
    monkeypatch.setattr(
        runner,
        "cleanup",
        lambda force=False: runner.record_step(
            "Nettoyage Compose", command="docker compose down", output="ok", exit_code=0, duration=0,
        ),
    )


def test_absent_compose_keeps_base_steps(tmp_path, logger, monkeypatch):
    runner = ComposeRunner("copie-test", str(tmp_path), 1, logger)
    no_docker_cleanup(runner, monkeypatch)

    result = runner.run_evaluation()

    assert result["success"] is False
    assert [step["title"] for step in result["steps"]][:2] == [
        "Ce que l'apprenant a rendu", "Fichier Compose résolu",
    ]


@pytest.mark.parametrize(
    ("compose_text", "expected"),
    [
        ("services:\n  nginx:\n    image: nginx\n", "Absence explicite"),
        ("services: [\n", "Échec explicite"),
    ],
)
def test_port_relaxation_reports_absence_or_yaml_error(tmp_path, logger, compose_text, expected):
    runner = make_runner(tmp_path, logger, compose_text)

    runner._relax_host_ports(str(tmp_path / "docker-compose.yml"))

    assert expected in runner._port_relaxation_outcome
    assert runner._port_relaxation_exit_code in {0, 2}


def test_port_relaxation_removes_fixed_host_port(tmp_path, logger):
    runner = make_runner(
        tmp_path,
        logger,
        "services:\n  nginx:\n    image: nginx\n    ports:\n      - '8080:80'\n",
    )

    compose_name = runner._relax_host_ports(str(tmp_path / "docker-compose.yml"))

    assert compose_name == "docker-compose.eval.yml"
    assert "80" in (tmp_path / compose_name).read_text()
    assert "8080:80" not in (tmp_path / compose_name).read_text()


def test_pipeline_lookup_uses_real_container_labels(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger, "services:\n  pipeline:\n    image: busybox\n")
    pipeline = FakeContainer("actual-project-pipeline-1", status="exited", service="pipeline")
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [FakeContainer("db", service="db"), pipeline])

    assert runner._pipeline_container() is pipeline


def test_pipeline_waits_for_labeled_real_container(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger, "services:\n  pipeline:\n    image: busybox\n")
    pipeline = FakeContainer("actual-project-pipeline-1", status="exited", exit_code=7, service="pipeline")
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [pipeline])

    assert runner._wait_for_pipeline_completion() == 7


def test_pipeline_success_records_service_snapshot(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger, "services:\n  pipeline:\n    image: busybox\n")
    monkeypatch.setattr(module, "DockerCompose", FakeCompose)
    no_docker_cleanup(runner, monkeypatch)
    monkeypatch.setattr(runner, "_images_du_projet", lambda: [])
    monkeypatch.setattr(runner, "_wait_for_services", lambda: None)
    monkeypatch.setattr(runner, "_wait_for_pipeline_completion", lambda: 0)
    monkeypatch.setattr(runner, "_etats_du_projet", lambda: {"actual-project-pipeline-1": ("exited", 0)})
    monkeypatch.setattr(runner, "_capture_logs", lambda: "logs")

    result = runner.run_evaluation()

    assert result["success"] is True
    assert any(step["title"] == "État des services" for step in result["steps"])


def test_pipeline_timeout_records_service_snapshot(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger, "services:\n  pipeline:\n    image: busybox\n")
    monkeypatch.setattr(module, "DockerCompose", FakeCompose)
    no_docker_cleanup(runner, monkeypatch)
    monkeypatch.setattr(runner, "_images_du_projet", lambda: [])
    monkeypatch.setattr(runner, "_wait_for_services", lambda: None)
    monkeypatch.setattr(runner, "_wait_for_pipeline_completion", lambda: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(runner, "_etats_du_projet", lambda: {"actual-project-pipeline-1": ("running", 0)})

    result = runner.run_evaluation()

    assert result["exit_code"] == 3
    assert any(step["title"] == "État des services" for step in result["steps"])


def test_pipeline_timeout_snapshots_while_compose_context_is_active(tmp_path, logger, monkeypatch):
    active = {"value": False}

    class ActiveCompose(FakeCompose):
        def __enter__(self):
            active["value"] = True
            return self

        def __exit__(self, *_args):
            active["value"] = False
            return False

    runner = make_runner(tmp_path, logger, "services:\n  pipeline:\n    image: busybox\n")
    monkeypatch.setattr(module, "DockerCompose", ActiveCompose)
    no_docker_cleanup(runner, monkeypatch)
    monkeypatch.setattr(runner, "_images_du_projet", lambda: [])
    monkeypatch.setattr(runner, "_wait_for_services", lambda: None)
    monkeypatch.setattr(runner, "_wait_for_pipeline_completion", lambda: (_ for _ in ()).throw(TimeoutError()))

    def states():
        assert active["value"] is True
        return {"actual-project-pipeline-1": ("running", 0)}

    monkeypatch.setattr(runner, "_etats_du_projet", states)

    result = runner.run_evaluation()

    assert result["exit_code"] == 3
    assert sum(step["title"] == "État des services" for step in result["steps"]) == 1


def test_probe_error_has_nonzero_exit_and_exact_message(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger)
    container = FakeContainer("nginx", service="nginx", ports={"80/tcp": [{"HostPort": "49152"}]})
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [container])
    monkeypatch.setattr(runner, "_capture_logs", lambda: "logs")
    import docker_eval.sonde_http as sonde_http
    monkeypatch.setattr(sonde_http, "_ouvrir", lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("refused")))

    result = runner._evaluer_services_persistants()
    probes = [step for step in result["steps"] if step["title"].startswith("Sonde ")]

    assert probes
    assert all(step["exit_code"] != 0 for step in probes)
    assert all("code non reçu" in step["output"] and "<urlopen error refused>" in step["output"] for step in probes)


def test_cleanup_exception_is_recorded_not_raised(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger)
    runner.compose = FakeCompose()
    monkeypatch.setattr(runner.compose, "stop", lambda: (_ for _ in ()).throw(RuntimeError("stop failed")))
    monkeypatch.setattr(runner, "_force_cleanup_docker_resources", lambda: (_ for _ in ()).throw(RuntimeError("resources failed")))
    monkeypatch.setattr(runner, "remove_exam_images", lambda: (_ for _ in ()).throw(RuntimeError("images failed")))

    runner.cleanup(force=True)

    step = runner.steps[-1]
    assert step["title"] == "Nettoyage Compose"
    assert step["exit_code"] != 0
    assert "stop failed" in step["output"] and "images failed" in step["output"]


def test_cleanup_records_image_removal_errors_returned_by_base_runner(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger)
    monkeypatch.setattr(runner, "_force_cleanup_docker_resources", lambda: None)
    monkeypatch.setattr(runner, "remove_exam_images", lambda: ["image example: remove failed"])

    runner.cleanup(force=True)

    step = runner.steps[-1]
    assert step["exit_code"] == 1
    assert "image example: remove failed" in step["output"]


def test_force_cleanup_uses_real_project_label_and_reports_remove_error(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger)
    runner._compose_project_names.add("actual-folder-name")
    removed_filters = []

    class BrokenResource:
        name = "leftover"

        def remove(self, **_kwargs):
            raise RuntimeError("remove failed")

    class Resources:
        def list(self, **kwargs):
            removed_filters.append(kwargs["filters"]["label"])
            return [BrokenResource()]

    fake_client = types.SimpleNamespace(containers=Resources(), networks=Resources())
    monkeypatch.setitem(sys.modules, "docker", types.SimpleNamespace(from_env=lambda: fake_client))

    with pytest.raises(RuntimeError, match="remove failed"):
        runner._force_cleanup_docker_resources()

    assert removed_filters == [
        "com.docker.compose.project=actual-folder-name",
        "com.docker.compose.project=actual-folder-name",
    ]
    assert all(runner.project_name not in label for label in removed_filters)


def test_build_failure_records_failed_step(tmp_path, logger, monkeypatch):
    class BrokenCompose(FakeCompose):
        def __enter__(self):
            raise RuntimeError("build failed")

    runner = make_runner(tmp_path, logger)
    monkeypatch.setattr(module, "DockerCompose", BrokenCompose)
    no_docker_cleanup(runner, monkeypatch)

    result = runner.run_evaluation()

    build = next(step for step in result["steps"] if step["title"] == "Build et démarrage Compose")
    assert build["exit_code"] != 0
    assert "build failed" in build["output"]


def test_capture_logs_uses_containers_returned_by_testcontainers(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger)
    container = FakeContainer("actual-project-nginx-1", service="nginx")
    container.logs = lambda **_kwargs: b"service output"
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [container])

    logs = runner._capture_logs()

    assert "Logs from nginx" in logs
    assert "service output" in logs


def test_build_step_honestly_describes_testcontainers_without_stdout(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger)
    monkeypatch.setattr(module, "DockerCompose", FakeCompose)
    no_docker_cleanup(runner, monkeypatch)
    monkeypatch.setattr(runner, "_images_du_projet", lambda: [])
    monkeypatch.setattr(runner, "_wait_for_services", lambda: None)
    monkeypatch.setattr(runner, "_services_du_compose", lambda _path: ["nginx"])
    monkeypatch.setattr(runner, "_etats_du_projet", lambda: {"nginx": ("running", 0)})
    monkeypatch.setattr(runner, "_sonder_ports_publies", lambda: [])
    monkeypatch.setattr(runner, "_capture_logs", lambda: "logs")

    result = runner.run_evaluation()

    build = next(step for step in result["steps"] if step["title"] == "Build et démarrage Compose")
    assert "stdout non disponible" in build["output"]


def test_annotation_du_bruit_de_sondes():
    from docker_eval.compose_runner import annoter_bruit_de_sondes

    sondes = [
        # port en clair : http répond, https échoue mécaniquement
        {"service": "grafana", "port": "3000/tcp", "url": "http://127.0.0.1:1/", "code": 200, "extrait": "<html>"},
        {"service": "grafana", "port": "3000/tcp", "url": "https://127.0.0.1:1/",
         "code": "non reçu", "erreur": "WRONG_VERSION_NUMBER", "extrait": "WRONG_VERSION_NUMBER"},
        # port TLS : le refus du HTTP en clair prouve la terminaison TLS
        {"service": "nginx", "port": "443/tcp", "url": "http://127.0.0.1:2/", "code": 400,
         "extrait": "The plain HTTP request was sent to HTTPS port"},
        {"service": "nginx", "port": "443/tcp", "url": "https://127.0.0.1:2/", "code": 404, "extrait": "Not Found"},
        # port muet : aucun schéma ne répond, vrai échec, pas de note
        {"service": "api", "port": "8000/tcp", "url": "http://127.0.0.1:3/", "code": "non reçu", "extrait": "refused"},
        {"service": "api", "port": "8000/tcp", "url": "https://127.0.0.1:3/", "code": "non reçu", "extrait": "refused"},
    ]
    annoter_bruit_de_sondes(sondes)

    assert sondes[1]["note"].startswith("échec attendu")
    assert sondes[2]["note"].startswith("preuve TLS")
    assert "note" not in sondes[0]
    assert "note" not in sondes[3]
    assert "note" not in sondes[4] and "note" not in sondes[5]


def test_investigator_garde_fous(tmp_path):
    from docker_eval.investigator import Investigator

    class RunnerFactice:
        def __init__(self): self.steps = []
        def record_step(self, title, command="", output="", exit_code=0, duration=0):
            self.steps.append({"title": title, "output": output, "exit_code": exit_code})

    (tmp_path / "note.txt").write_text("contenu de la copie")
    inv = Investigator(RunnerFactice(), str(tmp_path), services=["copie-nginx-1"])

    assert inv._sonde("http://example.com/") .startswith("refusé")
    assert inv._logs("autre-stack-postgres", 50).startswith("refusé")
    assert inv._exec("autre-stack-postgres", "cat /etc/passwd").startswith("refusé")
    assert inv._exec("copie-nginx-1", "rm -rf /").startswith("refusé")
    assert inv._fichier("../../etc/passwd").startswith("refusé")
    assert inv._fichier("note.txt") == "contenu de la copie"


def test_investigator_verdict_enregistre(monkeypatch, tmp_path):
    from docker_eval import investigator as mod

    monkeypatch.delenv("PI_CORRECTOR_VERIFY_BASE_URL", raising=False)

    class RunnerFactice:
        def __init__(self): self.steps = []
        def record_step(self, title, command="", output="", exit_code=0, duration=0):
            self.steps.append({"title": title, "output": output, "exit_code": exit_code})

    runner = RunnerFactice()
    inv = mod.Investigator(runner, str(tmp_path), services=["s1"])
    inv.base_url, inv.api_key = "https://gw.example", "clef"
    reponses = iter([
        '{"action":"fichier","chemin":"absent.txt"}',
        '{"action":"verdict","cause":"port 80 jamais lié","faute":"apprenant","revelable":"le port 80 refuse la connexion","non_revelable":"la directive à ajouter"}',
    ])
    monkeypatch.setattr(inv, "_appeler_llm", lambda messages: next(reponses))
    inv.investiguer([{"title": "Sonde http://127.0.0.1:80/", "output": "non reçu"}])

    assert runner.steps[0]["title"].startswith("Investigation 1")
    assert runner.steps[-1]["title"] == "Investigation — verdict"
    assert "port 80 jamais lié" in runner.steps[-1]["output"]
    assert runner.steps[-1]["exit_code"] == 0


def test_chemins_nginx_declares(tmp_path):
    from docker_eval.compose_runner import chemins_nginx_declares

    conf = tmp_path / "deployments" / "nginx"
    conf.mkdir(parents=True)
    (conf / "nginx.conf").write_text("""
    server {
        listen 443 ssl;
        location / { return 200; }
        location /api/v1/ { proxy_pass http://api_v1/; }
        location = /api/v2/predict { proxy_pass http://api_v2; }
        location /metrics { auth_basic "restricted"; }
        location ~ \\.php$ { deny all; }
    }
    """)
    chemins = chemins_nginx_declares(str(tmp_path))
    assert chemins == ["/api/v1/", "/api/v2/predict", "/metrics"]

    assert chemins_nginx_declares(str(tmp_path / "deployments")) == chemins
    vide = tmp_path / "vide"
    vide.mkdir()
    assert chemins_nginx_declares(str(vide)) == []


def test_identifiants_declares(tmp_path):
    from docker_eval.compose_runner import identifiants_declares

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "run_tests.sh").write_text(
        "curl -k https://localhost/predict \\\n     --user admin:admin \\\n     -d @request.json\n"
        "curl -k https://localhost/predict \\\n     --user admin:wrongpassword \\\n"
    )
    (tmp_path / "README.md").write_text("Utiliser `-u alice:s3cret` pour la route protégée.")
    couples = identifiants_declares(str(tmp_path))
    assert ("admin:admin", "tests/run_tests.sh") in couples
    assert any(c == "alice:s3cret" for c, _ in couples)
    assert all("wrong" not in c for c, _ in couples)


def test_requetes_declarees(tmp_path):
    from docker_eval.compose_runner import requetes_declarees

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "run_tests.sh").write_text(
        'response_v1=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://localhost/predict" \\\n'
        '     -H "Content-Type: application/json" \\\n'
        "     -d '{\"sentence\": \"cool\"}' \\\n"
        '     --user admin:admin \\\n'
        '     --cacert ./certs/nginx.crt)\n'
        'curl -s "https://localhost:443/health"\n'
    )
    reqs = requetes_declarees(str(tmp_path))
    assert {"methode", "chemin", "corps", "entetes", "identifiants", "source"} <= set(reqs[0])
    predict = [r for r in reqs if r["chemin"] == "/predict"][0]
    assert predict["methode"] == "POST"
    assert predict["identifiants"] == "admin:admin"
    assert "sentence" in predict["corps"]
    assert predict["entetes"].get("Content-Type") == "application/json"
    assert any(r["chemin"] == "/health" and r["methode"] == "GET" for r in reqs)


def test_fixed_container_names_become_network_aliases(tmp_path, logger):
    """461416 : deux corrections simultanées se disputaient
    `container_name: prometheus_server` (« Conflict. The container name is
    already in use »), et la copie échouait sans y être pour rien. Le nom fixe
    devient un alias réseau : la copie qui y fait référence (proxy_pass
    http://api_v2) le résout toujours."""
    import yaml

    runner = make_runner(
        tmp_path,
        logger,
        "services:\n"
        "  api-v2:\n    image: api\n    container_name: api_v2\n"
        "  prometheus:\n    image: prom\n    container_name: prometheus_server\n"
        "    networks: [monitoring, default]\n"
        "  grafana:\n    image: graf\n    container_name: grafana_dashboard\n"
        "    networks:\n      monitoring:\n        ipv4_address: 172.30.0.9\n"
        "  hote:\n    image: x\n    container_name: sur_l_hote\n    network_mode: host\n"
        "networks:\n  monitoring: {}\n",
    )

    nom = runner._relax_host_ports(str(tmp_path / "docker-compose.yml"))

    doc = yaml.safe_load((tmp_path / nom).read_text())
    services = doc["services"]
    assert "container_name" not in services["api-v2"]
    assert services["api-v2"]["networks"] == {"default": {"aliases": ["api_v2"]}}
    assert services["prometheus"]["networks"] == {
        "monitoring": {"aliases": ["prometheus_server"]},
        "default": {"aliases": ["prometheus_server"]},
    }
    assert services["grafana"]["networks"]["monitoring"] == {
        "ipv4_address": "172.30.0.9", "aliases": ["grafana_dashboard"],
    }
    # sans réseau Compose, pas d'alias possible : le nom reste
    assert services["hote"]["container_name"] == "sur_l_hote"
    assert "alias" in runner._port_relaxation_outcome


def test_compose_project_is_named_after_the_copy(tmp_path, logger, monkeypatch):
    """468469 et 459773, deux copies nginx extraites dans un dossier du même
    nom, corrigées en même temps : un seul projet compose, « No such
    container ». Le projet porte désormais l'identifiant de la copie."""
    vus = []

    class Temoin(FakeCompose):
        def __enter__(self):
            vus.append(os.environ.get("COMPOSE_PROJECT_NAME"))
            raise RuntimeError("arrêt du test")

    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    (tmp_path / "mlops-nginx-exam-2").mkdir()
    (tmp_path / "mlops-nginx-exam-2" / "docker-compose.yml").write_text("services:\n  nginx:\n    image: nginx\n")
    runner = ComposeRunner("468469", str(tmp_path / "mlops-nginx-exam-2"), 1, logger)
    monkeypatch.setattr(module, "DockerCompose", Temoin)
    no_docker_cleanup(runner, monkeypatch)

    runner.run_evaluation()

    assert vus == ["eval_468469"]


def test_built_images_are_those_of_the_copy_project(tmp_path, logger, monkeypatch):
    """Depuis que le projet s'appelle eval_<id>, les images construites portent
    ce préfixe : les retenir par le nom du dossier les laissait sur la machine."""
    (tmp_path / "mlops-nginx-exam-2").mkdir()
    runner = ComposeRunner("468469", str(tmp_path / "mlops-nginx-exam-2"), 1, logger)
    monkeypatch.setattr(runner, "_images_du_projet", lambda: [
        "eval_468469-api-v1:latest", "eval_468469-nginx:latest", "nginx:1.27", "prom/prometheus:v2.55.1",
    ])
    retenues = []
    monkeypatch.setattr(runner, "record_exam_image", retenues.append)

    runner._retenir_images_de_la_copie()

    assert retenues == ["eval_468469-api-v1:latest", "eval_468469-nginx:latest"]


class Redemarreur(FakeContainer):
    """Un conteneur dont chaque reload() avance d'un état dans `suite`."""

    def __init__(self, name, suite, service=None):
        super().__init__(name, service=service)
        self.suite = list(suite)
        self.reload()

    def reload(self):
        status, compte = self.suite.pop(0) if len(self.suite) > 1 else self.suite[0]
        self.status = status
        self.attrs["RestartCount"] = compte


def test_crash_loop_is_detected_even_when_caught_running(tmp_path, logger, monkeypatch):
    """461338 : à l'instant du relevé, le conteneur qui plante en boucle est
    souvent « running » (code remis à 0). Son compteur de redémarrages, lui,
    continue d'augmenter. Un service qui a redémarré une fois en attendant
    sa base puis s'est stabilisé n'est pas en boucle."""
    runner = make_runner(tmp_path, logger)
    boucle = Redemarreur("bike-api-1", [("running", 4), ("running", 4), ("restarting", 5)])
    stabilise = Redemarreur("api-1", [("running", 1)] * 50)
    sain = Redemarreur("nginx-1", [("running", 0)])
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [boucle, stabilise, sain])
    horloge = iter(range(0, 1000, 5))
    monkeypatch.setattr(module.time, "time", lambda: next(horloge))
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)

    assert runner._boucles_de_redemarrage() == {"bike-api-1"}


def test_no_restart_no_wait(tmp_path, logger, monkeypatch):
    runner = make_runner(tmp_path, logger)
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [Redemarreur("nginx-1", [("running", 0)])])
    monkeypatch.setattr(module.time, "sleep", lambda _s: (_ for _ in ()).throw(AssertionError("attente inutile")))

    assert runner._boucles_de_redemarrage() == set()


def test_failed_up_keeps_compose_output_and_dead_service_logs(tmp_path, logger, monkeypatch):
    """461416 : la trace ne disait que « returned non-zero exit status 1 ».
    La sortie du `compose up` et l'état des services, pris avant le
    démontage, disent pourquoi la stack n'a pas démarré."""
    import subprocess

    class UpFails(FakeCompose):
        def __enter__(self):
            raise subprocess.CalledProcessError(
                1, ["docker", "compose", "up", "--build", "--wait"],
                output=b"Container api-v1-1  Started\n",
                stderr=b"dependency failed to start: container api-v1-1 is unhealthy\n",
            )

    class Mort(FakeContainer):
        def logs(self, tail=None):
            return b"ModuleNotFoundError: No module named 'joblib'\n"

    runner = make_runner(tmp_path, logger)
    monkeypatch.setattr(module, "DockerCompose", UpFails)
    no_docker_cleanup(runner, monkeypatch)
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [
        FakeContainer("nginx-1", service="nginx"),
        Mort("api-v1-1", status="exited", exit_code=1, service="api-v1"),
    ])

    result = runner.run_evaluation()

    build = next(step for step in result["steps"] if step["title"] == "Build et démarrage Compose")
    assert build["exit_code"] != 0
    assert "is unhealthy" in build["output"]
    assert "api-v1-1  Started" in build["output"]
    assert "api-v1" in build["output"] and "exited (1)" in build["output"]
    assert "No module named 'joblib'" in build["output"]


def test_failed_up_is_diagnosed_before_teardown(tmp_path, logger, monkeypatch):
    """testcontainers démonte la stack dans son propre __enter__ quand le up
    échoue : le moteur démarre lui-même, relève, puis démonte."""
    import subprocess
    ordre = []

    class VraiComportement(FakeCompose):
        def start(self):
            raise subprocess.CalledProcessError(1, ["docker", "compose", "up"], stderr=b"unhealthy")

        def __enter__(self):
            # ce que fait testcontainers : start(), et __exit__ s'il échoue
            try:
                self.start()
            except Exception:
                self.stop()
                raise
            return self

        def stop(self):
            ordre.append("démontage")

    runner = make_runner(tmp_path, logger)
    monkeypatch.setattr(module, "DockerCompose", VraiComportement)
    monkeypatch.setattr(runner, "cleanup", lambda force=False: (runner.compose.stop(), ordre.append("cleanup")))
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: ordre.append("relevé") or [])

    runner.run_evaluation()

    assert ordre[0] == "relevé", ordre
