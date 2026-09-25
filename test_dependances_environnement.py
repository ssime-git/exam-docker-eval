"""Dependances d'environnement detectees avant execution (scriptorium #336).

461451 : `dns: 172.31.0.2` est le resolveur du VPC AWS de la machine de
correction. En production la copie passait (bike-api en 200) ; hors de ce
reseau, le DNS ne repond pas et le dataset est injoignable. Le fait est
vrai, la faute n'est pas celle de l'apprenant.
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import environnement
from docker_eval import investigator as mod

COMPOSE_461451 = """services:
  bike-api:
    build: ./api
    ports:
      - "8000:8000"
    dns:
      - 172.31.0.2
    environment:
      - MODEL_PATH=/models/model.pkl
"""


@pytest.fixture(autouse=True)
def sans_reseau(monkeypatch):
    """Aucun test ne touche le reseau : chaque verification est simulee."""
    monkeypatch.setattr(environnement, "resolveur_repond", lambda ip, **kw: pytest.fail("reseau"))
    monkeypatch.setattr(environnement, "sous_reseaux_locaux", lambda: pytest.fail("reseau"))
    monkeypatch.setattr(environnement, "hote_joignable", lambda hote, port, **kw: pytest.fail("reseau"))
    for nom in ("PI_CORRECTOR_VERIFY_BASE_URL", "PI_CORRECTOR_INVESTIGATE_MAX_ACTIONS",
                "PI_CORRECTOR_INVESTIGATE_TIMEOUT_SECONDS"):
        monkeypatch.delenv(nom, raising=False)


def ecrire(tmp_path, compose, **fichiers):
    (tmp_path / "docker-compose.yml").write_text(compose)
    for nom, contenu in fichiers.items():
        chemin = tmp_path / nom.replace("__", "/")
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(contenu)
    return str(tmp_path / "docker-compose.yml")


def resume(deps):
    return [(d["type"], d["service"], d["valeur"]) for d in deps]


# --- detection ----------------------------------------------------------------

def test_461451_dns_prive_detecte(tmp_path):
    compose = ecrire(tmp_path, COMPOSE_461451)
    assert resume(environnement.dependances_environnement(str(tmp_path), compose)) == \
        [("dns", "bike-api", "172.31.0.2")]


def test_dns_public_n_est_pas_une_dependance(tmp_path):
    compose = ecrire(tmp_path, "services:\n  api:\n    image: x\n    dns: 8.8.8.8\n")
    assert environnement.dependances_environnement(str(tmp_path), compose) == []


def test_ip_privee_en_dur(tmp_path):
    compose = ecrire(tmp_path, """services:
  api:
    image: x
    environment:
      DB_HOST: 10.0.3.4
      BIND: 0.0.0.0
      LOCAL: 127.0.0.1
    extra_hosts:
      - "db.interne:192.168.1.20"
      - "host.docker.internal:host-gateway"
    command: ["python", "app.py", "--redis", "172.20.5.9:6379"]
""", **{"api__Dockerfile": "FROM python:3.12\nENV CACHE=169.254.10.1\n"})
    deps = resume(environnement.dependances_environnement(str(tmp_path), compose))
    assert ("ip_privee", "api", "10.0.3.4") in deps
    assert ("extra_hosts", "api", "192.168.1.20") in deps
    assert ("ip_privee", "api", "172.20.5.9") in deps
    assert ("ip_privee", None, "169.254.10.1") in deps  # Dockerfile, sans service
    assert not any(v in ("0.0.0.0", "127.0.0.1") for _, _, v in deps)
    assert not any("host-gateway" in v for _, _, v in deps)


def test_network_mode_host(tmp_path):
    compose = ecrire(tmp_path, "services:\n  api:\n    image: x\n    network_mode: host\n")
    assert resume(environnement.dependances_environnement(str(tmp_path), compose)) == \
        [("network_mode", "api", "host")]


def test_telechargement_externe_au_demarrage(tmp_path):
    compose = ecrire(tmp_path, """services:
  api:
    image: x
    command: sh -c "curl -sL https://archive.ics.uci.edu/static/bike.zip -o /d.zip && python app.py"
    environment:
      - PEER=http://worker:8000/run
      - HEALTH=http://localhost:8000/
  worker:
    image: y
""")
    assert resume(environnement.dependances_environnement(str(tmp_path), compose)) == \
        [("telechargement", "api", "https://archive.ics.uci.edu")]


def test_sans_dependance(tmp_path):
    compose = ecrire(tmp_path, "services:\n  api:\n    build: ./api\n    ports: ['8000:8000']\n",
                     **{"api__Dockerfile": "FROM python:3.12\nCMD [\"python\", \"app.py\"]\n"})
    assert environnement.dependances_environnement(str(tmp_path), compose) == []


def test_compose_illisible_aucune_dependance(tmp_path):
    compose = ecrire(tmp_path, "services: [\n")
    assert environnement.dependances_environnement(str(tmp_path), compose) == []


# --- satisfaction par la machine courante -------------------------------------

def test_dns_non_satisfait_quand_le_resolveur_ne_repond_pas(tmp_path, monkeypatch):
    monkeypatch.setattr(environnement, "resolveur_repond", lambda ip, **kw: False)
    compose = ecrire(tmp_path, COMPOSE_461451)
    dep = environnement.verifier_dependance(environnement.dependances_environnement(str(tmp_path), compose)[0])
    assert dep["satisfaite"] is False
    assert "172.31.0.2" in dep["verification"] and "53" in dep["verification"]
    lignes = environnement.lignes_dependances([dep])
    assert lignes[0].startswith("dépendances d'environnement : 1")
    assert "non satisfaite" in lignes[1] and "bike-api" in lignes[1]


def test_dns_satisfait_quand_le_resolveur_repond(tmp_path, monkeypatch):
    monkeypatch.setattr(environnement, "resolveur_repond", lambda ip, **kw: True)
    compose = ecrire(tmp_path, COMPOSE_461451)
    dep = environnement.verifier_dependance(environnement.dependances_environnement(str(tmp_path), compose)[0])
    assert dep["satisfaite"] is True
    assert "— satisfaite" in environnement.lignes_dependances([dep])[1]


def test_ip_privee_satisfaite_par_un_sous_reseau_local(monkeypatch):
    import ipaddress
    monkeypatch.setattr(environnement, "sous_reseaux_locaux",
                        lambda: [ipaddress.ip_network("10.0.0.0/16")])
    dedans = environnement.verifier_dependance({"type": "ip_privee", "service": "api", "valeur": "10.0.3.4",
                                                "source": "environment"})
    dehors = environnement.verifier_dependance({"type": "ip_privee", "service": "api", "valeur": "192.168.1.20",
                                                "source": "environment"})
    assert dedans["satisfaite"] is True and dehors["satisfaite"] is False
    monkeypatch.setattr(environnement, "sous_reseaux_locaux", lambda: None)
    inconnu = environnement.verifier_dependance({"type": "ip_privee", "service": "api", "valeur": "10.0.3.4",
                                                 "source": "environment"})
    assert inconnu["satisfaite"] is None


def test_network_mode_host_satisfait_sur_linux(monkeypatch):
    monkeypatch.setattr(environnement.sys, "platform", "linux")
    dep = {"type": "network_mode", "service": "api", "valeur": "host", "source": "network_mode"}
    assert environnement.verifier_dependance(dict(dep))["satisfaite"] is True
    monkeypatch.setattr(environnement.sys, "platform", "darwin")
    assert environnement.verifier_dependance(dict(dep))["satisfaite"] is False


def test_telechargement_verifie_par_connexion_tcp(monkeypatch):
    vus = []
    monkeypatch.setattr(environnement, "hote_joignable",
                        lambda hote, port, **kw: vus.append((hote, port)) or False)
    dep = environnement.verifier_dependance({"type": "telechargement", "service": "api",
                                             "valeur": "https://archive.ics.uci.edu", "source": "command"})
    assert vus == [("archive.ics.uci.edu", 443)] and dep["satisfaite"] is False


def test_resolveur_repond_sur_un_vrai_serveur_udp():
    serveur = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    serveur.bind(("127.0.0.1", 0))
    port = serveur.getsockname()[1]

    def repondre():
        requete, adresse = serveur.recvfrom(512)
        serveur.sendto(requete[:2] + b"\x81\x80" + requete[4:], adresse)

    fil = threading.Thread(target=repondre, daemon=True)
    fil.start()
    try:
        # la fixture a remplace la fonction : on appelle l'originale
        assert ORIGINAL_RESOLVEUR("127.0.0.1", port=port, timeout=2) is True
    finally:
        fil.join(2)
        serveur.close()
    muet = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    muet.bind(("127.0.0.1", 0))
    try:
        assert ORIGINAL_RESOLVEUR("127.0.0.1", port=muet.getsockname()[1], timeout=0.3) is False
    finally:
        muet.close()


ORIGINAL_RESOLVEUR = environnement.resolveur_repond


# --- consignation dans « Environnement du harnais » ---------------------------

def _runner_en_panne(tmp_path, monkeypatch):
    from docker_eval.compose_runner import ComposeRunner
    import docker_eval.compose_runner as module
    monkeypatch.setattr(environnement, "version_python_resolue", lambda version: None)
    runner = ComposeRunner("copie-test", str(tmp_path), 1, logging.getLogger("env"))
    monkeypatch.setattr(runner, "cleanup", lambda force=False: None)

    class ComposeEnPanne:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            raise RuntimeError("build impossible")

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(module, "DockerCompose", ComposeEnPanne)
    return runner


def test_compose_consigne_les_dependances_avant_le_build(tmp_path, monkeypatch):
    ecrire(tmp_path, COMPOSE_461451)
    monkeypatch.setattr(environnement, "resolveur_repond", lambda ip, **kw: False)
    runner = _runner_en_panne(tmp_path, monkeypatch)
    resultat = runner.run_evaluation()

    etape = next(s for s in resultat["steps"] if s["title"] == "Environnement du harnais")
    assert "dépendances d'environnement : 1" in etape["output"]
    assert "dns: 172.31.0.2" in etape["output"] and "non satisfaite" in etape["output"]
    assert etape["dependances_environnement"][0]["satisfaite"] is False
    assert runner.dependances_environnement == etape["dependances_environnement"]
    assert etape["exit_code"] == 0  # un fait, pas une faute


def test_compose_sans_dependance_inchange(tmp_path, monkeypatch):
    ecrire(tmp_path, "services:\n  api:\n    build: ./api\n")
    runner = _runner_en_panne(tmp_path, monkeypatch)
    resultat = runner.run_evaluation()
    etape = next(s for s in resultat["steps"] if s["title"] == "Environnement du harnais")
    assert "dépendances d'environnement" not in etape["output"]
    assert "dependances_environnement" not in etape
    assert runner.dependances_environnement == []


# --- imputation mecanique -------------------------------------------------------

class RunnerFactice:
    def __init__(self, dependances=()):
        self.steps = []
        self.dependances_environnement = list(dependances)

    def record_step(self, title, command="", output="", exit_code=0, duration=None, **champs):
        self.steps.append({"title": title, "command": command, "output": output,
                           "exit_code": exit_code, **champs})


def dep_dns(satisfaite):
    return {"type": "dns", "service": "bike-api", "valeur": "172.31.0.2", "source": "dns",
            "detail": "résolveur DNS privé imposé", "satisfaite": satisfaite,
            "verification": "requête DNS UDP vers 172.31.0.2:53 depuis l'hôte"}


def enqueter(tmp_path, monkeypatch, dependances, reponses, services=("bike-api", "autre")):
    runner = RunnerFactice(dependances)
    inv = mod.Investigator(runner, str(tmp_path), services=list(services), scoreur=lambda p, a: pytest.fail("scoreur"))
    inv.base_url, inv.api_key = "https://gw.example", "clef"
    flux = iter(reponses)
    messages = []

    def llm(m):
        messages.append([dict(x) for x in m])
        return next(flux)

    monkeypatch.setattr(inv, "_appeler_llm", llm)
    return inv, runner, messages


class Processus:
    def __init__(self, regles):
        self.regles = regles

    def __call__(self, argv, **kwargs):
        for motif, (rc, sortie) in self.regles:
            if motif in " ".join(argv):
                return subprocess.CompletedProcess(argv, rc, stdout=sortie, stderr="")
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr="inconnu")


H_DNS = {"action": "hypothese", "id": "H1", "enonce": "le DNS imposé ne résout pas l'hôte du dataset",
         "faute": "apprenant",
         "test": {"action": "dns", "service": "bike-api", "nom": "archive.ics.uci.edu"},
         "si_vraie": {"exit_code": 2}, "si_fausse": {"exit_code": 0}}
H_LOGS = {"action": "hypothese", "id": "H2", "enonce": "le dataset est injoignable au démarrage",
          "faute": "apprenant",
          "test": {"action": "logs", "service": "bike-api", "lignes": 200},
          "si_vraie": {"contient": "Unable to fetch Bike Sharing dataset"},
          "si_fausse": {"ne_contient_pas": "Unable to fetch Bike Sharing dataset"}}
ECHEC = {"title": "Sonde http://127.0.0.1:8000/predict",
         "output": "non reçu : [Errno 104] Connection reset by peer", "exit_code": 1}


def bloc(sortie):
    debut = sortie.rindex("```json\n") + len("```json\n")
    return json.loads(sortie[debut:sortie.index("\n```", debut)])


def test_461451_dns_non_satisfait_faute_environnement(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run", Processus([
        ("getent", (2, "--- /etc/resolv.conf\nnameserver 172.31.0.2\n"))]))
    inv, runner, messages = enqueter(tmp_path, monkeypatch, [dep_dns(False)], [json.dumps(H_DNS)])
    inv.investiguer([ECHEC])

    assert "172.31.0.2" in messages[0][1]["content"]  # le LLM connait la dependance
    verification, verdict = runner.steps[-2]["output"], runner.steps[-1]["output"]
    assert "dépendance d'environnement non satisfaite" in verification
    assert "faute : environnement" in verdict and "faute : apprenant" not in verdict
    donnees = bloc(verdict)
    assert donnees["faute"] == "environnement" and donnees["statut"] == "cause_etablie"
    assert donnees["dependances_environnement"][0]["valeur"] == "172.31.0.2"


def test_461451_logs_du_service_dependant_faute_environnement(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run", Processus([
        ("docker logs", (0, "Unable to fetch Bike Sharing dataset\n"))]))
    inv, runner, _ = enqueter(tmp_path, monkeypatch, [dep_dns(False)], [json.dumps(H_LOGS)])
    inv.investiguer([ECHEC])
    assert bloc(runner.steps[-1]["output"])["faute"] == "environnement"


def test_461451_dns_satisfait_pas_d_echec_dns(tmp_path, monkeypatch):
    """Sur la machine de production, le resolveur repond : l'hypothese DNS est
    refutee, et une autre cause garde sa faute."""
    monkeypatch.setattr(mod.subprocess, "run", Processus([
        ("getent", (0, "128.195.10.252 archive.ics.uci.edu\n--- /etc/resolv.conf\nnameserver 172.31.0.2\n")),
        ("docker logs", (0, "Unable to fetch Bike Sharing dataset\n"))]))
    inv, runner, _ = enqueter(tmp_path, monkeypatch, [dep_dns(True)], [json.dumps([H_DNS, H_LOGS])])
    inv.investiguer([ECHEC])
    assert runner.steps[0]["verdict"] == "réfutée"
    donnees = bloc(runner.steps[-1]["output"])
    assert donnees["hypothese_etablie"] == "H2" and donnees["faute"] == "apprenant"


def test_autre_service_sans_dependance_garde_sa_faute(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run", Processus([("docker logs", (0, "KeyError: 'model'\n"))]))
    h = {"action": "hypothese", "id": "H1", "enonce": "le service autre plante au chargement",
         "faute": "apprenant", "test": {"action": "logs", "service": "autre", "lignes": 50},
         "si_vraie": {"contient": "KeyError"}, "si_fausse": {"ne_contient_pas": "KeyError"}}
    inv, runner, _ = enqueter(tmp_path, monkeypatch, [dep_dns(False)], [json.dumps(h)])
    inv.investiguer([ECHEC])
    assert bloc(runner.steps[-1]["output"])["faute"] == "apprenant"


def test_sans_dependance_imputation_inchangee(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run", Processus([
        ("getent", (2, "--- /etc/resolv.conf\nnameserver 127.0.0.11\n"))]))
    inv, runner, messages = enqueter(tmp_path, monkeypatch, [], [json.dumps(H_DNS)])
    inv.investiguer([ECHEC])
    donnees = bloc(runner.steps[-1]["output"])
    assert donnees["faute"] == "apprenant" and donnees["dependances_environnement"] == []
    assert "Dépendances d'environnement" not in messages[0][1]["content"]


def test_prompt_enonce_la_regle_environnement():
    assert "environnement" in mod.PROMPT_SYSTEME
    assert "jamais" in mod.PROMPT_SYSTEME.split("dépendance d'environnement", 1)[1][:400]
    assert "environnement" in mod.FAUTES
