"""Diagnostic des services non sains quand `compose up --wait` échoue (467094).

La stack de 467094 tombait sur « container bike-api-1 is unhealthy ». Le
conteneur était « running » (restart: always), le moteur ne relevait donc ni
ses logs ni l'historique de son healthcheck : impossible de dire si la copie
plantait ou si la machine de correction n'avait pas de réseau. Ces tests
fixent la capture et la classification de la cause.
"""
import logging
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import docker_eval.compose_runner as module
from docker_eval.compose_runner import ComposeRunner
from docker_eval.diagnostic_demarrage import (
    classer_demarrage,
    decrire_service,
    masquer_secrets,
    classer_service,
    definition_healthcheck,
    services_non_sains,
)

NS = 1_000_000_000

# Sortie réelle de bike-api (467094), rejouée en Docker le 16/10 : pyarrow
# 26.0.0, non épinglé, publié le 09/10, après le rendu du 03/10.
LOGS_467094 = """\
Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/uvicorn/importer.py", line 19, in import_from_string
    module = importlib.import_module(module_str)
  File "/app/main.py", line 12, in <module>
    from evidently import Report, Dataset, DataDefinition, Regression
  File "/usr/local/lib/python3.12/site-packages/sklearn/utils/fixes.py", line 421, in <module>
    import pyarrow
  File "/usr/local/lib/python3.12/site-packages/pyarrow/__init__.py", line 59, in <module>
    from pyarrow.lib import (BuildInfo, CppBuildInfo, RuntimeInfo, set_timezone_db_path,
  File "pyarrow/lib.pyx", line 42, in init pyarrow.lib
ImportError: pyarrow requires NumPy 2.0 or newer, found 1.26.4
"""

# Sortie réelle de la même image sur un réseau Docker --internal (sans accès
# sortant), 16/10.
LOGS_RESEAU = """\
INFO:main:Fetching data from UCI archive...
ERROR:main:Error while preparing the model: HTTPSConnectionPool(host='archive.ics.uci.edu', port=443): Max retries exceeded with url: /static/public/275/bike+sharing+dataset.zip (Caused by NameResolutionError("HTTPSConnection(host='archive.ics.uci.edu', port=443): Failed to resolve 'archive.ics.uci.edu' ([Errno -3] Temporary failure in name resolution)"))
Traceback (most recent call last):
urllib3.exceptions.NameResolutionError: HTTPSConnection(host='archive.ics.uci.edu', port=443): Failed to resolve 'archive.ics.uci.edu' ([Errno -3] Temporary failure in name resolution)
    raise RuntimeError("Failed to prepare ML model, application cannot start.") from e
RuntimeError: Failed to prepare ML model, application cannot start.
"""

LOGS_PLANTAGE = """\
Traceback (most recent call last):
  File "/app/main.py", line 3, in <module>
    import joblibb
ModuleNotFoundError: No module named 'joblibb'
"""

HEALTHCHECK = {"Test": ["CMD", "curl", "-f", "http://localhost:8080/health"],
               "Interval": 10 * NS, "Timeout": 5 * NS, "StartPeriod": 30 * NS, "Retries": 12}


def etat(status="running", health="unhealthy", restarts=0, exit_code=0, oom=False,
         health_log=(), healthcheck=HEALTHCHECK, service="bike-api"):
    attrs = {
        "State": {"Status": status, "ExitCode": exit_code, "OOMKilled": oom},
        "RestartCount": restarts,
        "Config": {"Healthcheck": healthcheck, "Env": ["GF_SECURITY_ADMIN_PASSWORD=secret-ne-pas-afficher"],
                   "Labels": {"com.docker.compose.service": service}},
    }
    if health is not None:
        attrs["State"]["Health"] = {
            "Status": health,
            "FailingStreak": len(health_log),
            "Log": [{"ExitCode": code, "Output": sortie} for code, sortie in health_log],
        }
    return attrs


# --- classification ----------------------------------------------------------

def test_reseau_sortant_est_non_mesure_meme_avec_traceback():
    """L'ordre compte : l'erreur réseau produit aussi un traceback et un exit 1.
    Lu comme un plantage, ce serait un zéro pour notre absence de réseau."""
    cause = classer_service(etat(restarts=4, exit_code=1), LOGS_RESEAU)
    assert cause["imputation"] == "non_mesure"
    assert cause["cause"] == "reseau_sortant"
    assert "archive.ics.uci.edu" in cause["preuve"]


def test_conflit_de_versions_467094_n_est_pas_impute_a_la_copie():
    cause = classer_service(etat(restarts=5, exit_code=1, health_log=()), LOGS_467094)
    assert cause["cause"] == "conflit_versions_dependances"
    assert cause["imputation"] == "indetermine"
    assert "NumPy 2.0" in cause["preuve"]


def test_plantage_de_l_application_est_la_faute_de_la_copie():
    cause = classer_service(etat(status="exited", health=None, exit_code=1), LOGS_PLANTAGE)
    assert cause["imputation"] == "copie"
    assert cause["cause"] == "plantage_application"
    assert "joblibb" in cause["preuve"]


def test_sortie_non_nulle_sans_traceback_est_la_faute_de_la_copie():
    cause = classer_service(etat(status="exited", health=None, exit_code=3), "bye\n")
    assert cause["imputation"] == "copie"
    assert cause["cause"] == "plantage_application"


def test_commande_de_healthcheck_absente_est_la_faute_de_la_copie():
    cause = classer_service(
        etat(health_log=[(127, "/bin/sh: 1: curl: not found\n")] * 3),
        "INFO:     Application startup complete.\n")
    assert cause["imputation"] == "copie"
    assert cause["cause"] == "healthcheck_commande_absente"
    assert "curl: not found" in cause["preuve"]


def test_executable_introuvable_en_forme_exec():
    cause = classer_service(
        etat(health_log=[(-1, 'OCI runtime exec failed: exec: "wget": executable file not found in $PATH')]),
        "")
    assert cause["cause"] == "healthcheck_commande_absente"


def test_connexion_refusee_sur_localhost_n_est_pas_du_reseau_sortant():
    """Le healthcheck qui frappe une app morte voit « connection refused » vers
    localhost : symptôme, pas réseau de la machine de correction."""
    cause = classer_service(
        etat(status="exited", health="unhealthy", exit_code=1,
             health_log=[(1, "urllib.error.URLError: <urlopen error [Errno 111] Connection refused> "
                             "http://localhost:8080/health")]),
        LOGS_PLANTAGE)
    assert cause["imputation"] == "copie"


def test_nom_de_service_compose_non_resolu_n_est_pas_du_reseau_sortant():
    """Un nom sans point est un service de la stack : une faute de frappe de
    la copie, pas l'absence d'internet."""
    logs = ("socket.gaierror: [Errno -2] Name or service not known\n"
            "requests.exceptions.ConnectionError: HTTPConnectionPool(host='promethus', port=9090): "
            "Max retries exceeded\nTraceback (most recent call last):\n")
    cause = classer_service(etat(status="exited", health=None, exit_code=1), logs)
    assert cause["imputation"] == "copie"


def test_healthcheck_encore_en_demarrage_est_un_delai():
    cause = classer_service(etat(health="starting"), "INFO:main:Fetching data from UCI archive...\n")
    assert cause["cause"] == "delai_depasse"
    assert cause["reessayable"] is True


def test_unhealthy_sans_plantage_ni_redemarrage_est_reessayable():
    """Sur 2 vCPU, un entraînement au démarrage peut dépasser start_period +
    retries × interval : on attend plus longtemps avant de conclure."""
    cause = classer_service(
        etat(health_log=[(1, "<urlopen error [Errno 111] Connection refused>")] * 12),
        "INFO:main:Fetching data from UCI archive...\nINFO:main:Data fetched successfully\n")
    assert cause["reessayable"] is True


def test_boucle_de_redemarrage_n_est_pas_reessayable():
    cause = classer_service(etat(restarts=3, exit_code=1), LOGS_PLANTAGE)
    assert cause["reessayable"] is False


def test_oom_est_non_mesure_comme_dans_le_runner_bentoml():
    """Le moteur ne pose pas de limite mémoire : un OOM est la mémoire de la
    machine de correction (même règle que bentoml_runner._attribute_fault)."""
    cause = classer_service(etat(status="exited", health=None, exit_code=137, oom=True), "Killed\n")
    assert cause["cause"] == "memoire_epuisee"
    assert cause["imputation"] == "non_mesure"


def test_definition_du_healthcheck_en_secondes():
    assert definition_healthcheck(HEALTHCHECK) == {
        "test": ["CMD", "curl", "-f", "http://localhost:8080/health"],
        "interval_s": 10.0, "timeout_s": 5.0, "start_period_s": 30.0, "retries": 12}
    assert definition_healthcheck(None) is None


def test_selection_des_services_non_sains():
    etats = {
        "api": etat(health="unhealthy"),
        "ok": etat(health="healthy"),
        "remis-sain": etat(health="healthy", restarts=1),
        "sans-healthcheck": etat(health=None, healthcheck=None),
        "boucle": etat(health=None, healthcheck=None, restarts=2),
        "stabilise": etat(health=None, healthcheck=None, restarts=1),
        "sorti-0": etat(status="exited", health=None, exit_code=0),
        "mort": etat(status="exited", health=None, exit_code=1),
        "jamais-lance": etat(status="created", health=None),
    }
    # « stabilise » a redémarré une fois en attendant sa dépendance puis tient
    # debout : seule une boucle observée en fait un service non sain.
    assert services_non_sains(etats, boucles={"boucle"}) == ["api", "boucle", "mort"]


def test_un_redemarrage_rattrape_ne_bloque_pas_le_reessai(tmp_path, monkeypatch):
    """Prometheus (restart: always) a redémarré une fois puis est sain ; l'API
    est seulement lente. Lire le redémarrage comme un plantage imputerait la
    copie et empêcherait le réessai."""
    prom = Conteneur("eval-prometheus-1",
                     etat(health="healthy", restarts=1, service="prometheus"),
                     b"Traceback (most recent call last):\nOSError: old\n")
    attrs_api = etat(health="unhealthy", health_log=[(1, "Connection refused")] * 12)
    api = Conteneur("eval-bike-api-1", attrs_api, b"INFO:main:Training...\n")
    relectures = []

    def reload():
        relectures.append(1)
        if len(relectures) > 1:
            attrs_api["State"]["Health"]["Status"] = "healthy"
    api.reload = reload
    lances = []

    class Lent(FakeCompose):
        def start(self):
            lances.append(1)
            if len(lances) == 1:
                super().start()

    runner = runner_sans_docker(tmp_path, monkeypatch, [prom, api])
    monkeypatch.setattr(module, "DockerCompose", Lent)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    for nom in ("_wait_for_services", "_completer_environnement_compose", "_retenir_images_de_la_copie"):
        monkeypatch.setattr(runner, nom, lambda: None)
    monkeypatch.setattr(runner, "_evaluer_services_persistants",
                        lambda: {"success": True, "exit_code": 0, "steps": runner.steps})

    result = runner.run_evaluation()

    assert len(lances) == 2
    assert "prometheus" not in result["cause_demarrage"]["services"]
    assert result["cause_demarrage"]["imputation"] != "copie"


def test_secrets_masques_dans_healthcheck_sorties_et_logs():
    attrs = etat(health_log=[(22, "curl: (22) 401 for admin:s3cr3t-grafana")],
                 healthcheck=dict(HEALTHCHECK, Test=["CMD-SHELL", "curl -u admin:s3cr3t-grafana http://localhost:3000"]))
    attrs["Config"]["Env"] = ["GF_SECURITY_ADMIN_PASSWORD=s3cr3t-grafana", "PATH=/usr/bin"]
    logs = masquer_secrets("login admin / s3cr3t-grafana\n", attrs)
    bloc = decrire_service("grafana", attrs, logs, classer_service(attrs, logs))
    assert "s3cr3t-grafana" not in bloc
    assert "s3cr3t-grafana" not in logs


def test_agregat_le_non_mesure_l_emporte():
    """Une seule cause non mesurée suffit : la note ne peut pas s'appuyer sur
    une stack que nous n'avons pas pu faire tourner."""
    causes = {"api": {"imputation": "copie", "cause": "plantage_application"},
              "worker": {"imputation": "non_mesure", "cause": "reseau_sortant"}}
    assert classer_demarrage(causes)["imputation"] == "non_mesure"
    assert classer_demarrage({"api": causes["api"]})["imputation"] == "copie"
    assert classer_demarrage({})["imputation"] == "indetermine"


def test_agregat_un_service_seulement_en_attente_ne_couvre_pas_un_plantage():
    """Compose abandonne dès qu'un service plante : les autres restent en
    « starting ». Ce délai-là n'est pas vérifié et ne masque pas la faute."""
    causes = {"api": {"imputation": "copie", "cause": "plantage_application", "reessayable": False},
              "grafana": {"imputation": "non_mesure", "cause": "delai_depasse", "reessayable": True}}
    assert classer_demarrage(causes)["cause"] == "plantage_application"


# --- intégration au moteur ---------------------------------------------------

class FakeCompose:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        raise subprocess.CalledProcessError(
            1, ["docker", "compose", "up", "--build", "--wait"],
            output=b"", stderr=b"dependency failed to start: container bike-api-1 is unhealthy\n")

    def __exit__(self, *_args):
        return False

    def stop(self):
        pass


class Conteneur:
    def __init__(self, name, attrs, logs=b""):
        self.name = name
        self.attrs = attrs
        self.status = attrs["State"]["Status"]
        self.labels = attrs["Config"]["Labels"]
        self._logs = logs

    def reload(self):
        pass

    def logs(self, tail=None):
        return self._logs


def runner_sans_docker(tmp_path, monkeypatch, conteneurs):
    (tmp_path / "docker-compose.yml").write_text("services:\n  bike-api:\n    image: x\n")
    runner = ComposeRunner("copie-test", str(tmp_path), 600, logging.getLogger("diag"))
    monkeypatch.setattr(module, "DockerCompose", FakeCompose)
    monkeypatch.setattr(runner, "cleanup", lambda force=False: None)
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: conteneurs)
    return runner


def test_service_unhealthy_mais_running_est_diagnostique(tmp_path, monkeypatch):
    """467094 : bike-api « running » mais unhealthy ; ses logs manquaient."""
    conteneurs = [Conteneur("eval-bike-api-1", etat(restarts=5, exit_code=1), LOGS_467094.encode())]
    runner = runner_sans_docker(tmp_path, monkeypatch, conteneurs)

    result = runner.run_evaluation()

    diag = next(s for s in result["steps"] if s["title"] == "Diagnostic des services non sains")
    assert diag["exit_code"] != 0
    assert "pyarrow requires NumPy 2.0" in diag["output"]
    assert "RestartCount 5" in diag["output"]
    assert "start_period 30" in diag["output"]
    assert "secret-ne-pas-afficher" not in diag["output"]
    assert result["cause_demarrage"]["imputation"] == "indetermine"
    assert result["cause_demarrage"]["cause"] == "conflit_versions_dependances"
    assert result["cause_demarrage"]["service"] == "bike-api"
    # contrat déjà lu par pi-corrector (result.fault) : apprenant|systeme|indetermine
    assert result["fault"] == "indetermine"


@pytest.mark.parametrize("imputation, fault", [
    ("copie", "apprenant"), ("non_mesure", "systeme"), ("indetermine", "indetermine")])
def test_imputation_reportee_dans_fault(imputation, fault):
    from docker_eval.diagnostic_demarrage import FAUTE_DU_CONTRAT
    assert FAUTE_DU_CONTRAT[imputation] == fault


def test_images_construites_retenues_meme_si_le_up_echoue(tmp_path, monkeypatch):
    """Les images n'étaient recensées qu'après un `up` réussi : une stack
    unhealthy laissait ~1,3 Go d'images sur la machine (467094)."""
    conteneurs = [Conteneur("eval-bike-api-1", etat(restarts=5, exit_code=1), LOGS_467094.encode())]
    runner = runner_sans_docker(tmp_path, monkeypatch, conteneurs)
    appels = []
    monkeypatch.setattr(runner, "_retenir_images_de_la_copie", lambda: appels.append(1))

    runner.run_evaluation()

    assert appels


def test_sortie_du_healthcheck_dans_le_diagnostic(tmp_path, monkeypatch):
    conteneurs = [Conteneur("eval-bike-api-1",
                            etat(restarts=1, exit_code=0,
                                 health_log=[(127, "/bin/sh: 1: curl: not found")] * 12),
                            b"INFO:     Application startup complete.\n")]
    runner = runner_sans_docker(tmp_path, monkeypatch, conteneurs)

    result = runner.run_evaluation()

    diag = next(s for s in result["steps"] if s["title"] == "Diagnostic des services non sains")
    assert "curl: not found" in diag["output"]
    assert "FailingStreak 12" in diag["output"]
    assert result["cause_demarrage"]["imputation"] == "copie"


def test_delai_depasse_reessaie_une_fois_et_poursuit(tmp_path, monkeypatch):
    """Healthcheck encore en starting : on attend plus longtemps, puis on
    relance le `up --wait` une fois ; s'il passe, l'évaluation continue."""
    attrs = etat(health="starting")
    lances = []

    class Lent(FakeCompose):
        def start(self):
            lances.append(1)
            if len(lances) == 1:
                super().start()

    conteneur = Conteneur("eval-bike-api-1", attrs, b"INFO:main:Fetching data...\n")

    relectures = []

    def reload():
        # le premier relevé (diagnostic) voit « starting », la suite « healthy »
        relectures.append(1)
        if len(relectures) > 1:
            attrs["State"]["Health"]["Status"] = "healthy"
    conteneur.reload = reload

    runner = runner_sans_docker(tmp_path, monkeypatch, [conteneur])
    monkeypatch.setattr(module, "DockerCompose", Lent)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner, "_wait_for_services", lambda: None)
    monkeypatch.setattr(runner, "_completer_environnement_compose", lambda: None)
    monkeypatch.setattr(runner, "_retenir_images_de_la_copie", lambda: None)
    monkeypatch.setattr(runner, "_evaluer_services_persistants",
                        lambda: {"success": True, "exit_code": 0, "steps": runner.steps})

    result = runner.run_evaluation()

    assert len(lances) == 2
    assert result["success"] is True
    assert result["cause_demarrage"]["cause"] == "delai_depasse"
    assert result["cause_demarrage"]["reessai"]["resultat"] == "sain"
    diag = next(s for s in result["steps"] if s["title"] == "Diagnostic des services non sains")
    assert "réessai" in diag["output"]
    # stack finalement saine : pas d'étape en échec qui ferait chercher une cause
    assert diag["exit_code"] == 0


def test_une_seule_capture_des_logs_entre_les_deux_etapes(tmp_path, monkeypatch):
    """L'étape « Build et démarrage Compose » (#31) garde la liste des
    services et leur santé, mais renvoie au diagnostic pour les services
    déjà relevés : pas de seconde capture de logs."""
    appels = []

    class Compte(Conteneur):
        def logs(self, tail=None):
            appels.append(tail)
            return LOGS_467094.encode()

    conteneurs = [Compte("eval-bike-api-1", etat(restarts=5, exit_code=1))]
    runner = runner_sans_docker(tmp_path, monkeypatch, conteneurs)

    result = runner.run_evaluation()

    assert appels == [200]
    build = next(s for s in result["steps"] if s["title"] == "Build et démarrage Compose")
    assert "bike-api (eval-bike-api-1) : running, unhealthy" in build["output"]
    assert "Diagnostic des services non sains" in build["output"]
    assert "pyarrow requires NumPy" not in build["output"]


def test_reseau_sortant_cite_le_dns_retire_de_la_copie(tmp_path, monkeypatch):
    """469634 : si l'erreur réseau survient malgré le retrait de `dns:`, la
    preuve cite la clé d'origine, pour la repérer sans ouvrir le compose."""
    conteneurs = [Conteneur("eval-bike-api-1", etat(restarts=1, exit_code=1), LOGS_RESEAU.encode())]
    runner = runner_sans_docker(tmp_path, monkeypatch, conteneurs)
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  bike-api:\n    image: x\n    dns: [8.8.8.8, 1.1.1.1]\n")

    result = runner.run_evaluation()

    cause = result["cause_demarrage"]
    assert cause["cause"] == "reseau_sortant"
    assert "dns: [8.8.8.8, 1.1.1.1]" in cause["preuve"]
    assert "dns: [8.8.8.8, 1.1.1.1]" in cause["services"]["bike-api"]["preuve"]
