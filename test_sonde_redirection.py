"""Une redirection est une réponse, pas une panne (scriptorium #320).

457405, 461091 : nginx répond 301 vers https://127.0.0.1/ sur le port 80.
La sonde suivait la redirection vers le 443, non publié sur l'hôte, et
enregistrait « Connection refused ». Ici, un petit serveur local renvoie
le même 301 : la sonde doit s'arrêter dessus et le consigner.
"""
import http.server
import logging
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval.compose_runner import ComposeRunner
from docker_eval.investigator import Investigator
from docker_eval.sonde_http import sonder


class _Gestionnaire(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/ok"):
            corps = b"bonjour"
            self.send_response(200)
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return
        # La forme exacte de nginx : `return 301 https://$host$request_uri;`
        self.send_response(301)
        self.send_header("Location", "https://127.0.0.1/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def serveur():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Gestionnaire)
    fil = threading.Thread(target=httpd.serve_forever, daemon=True)
    fil.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_sonder_ne_suit_pas_la_redirection(serveur):
    sonde = sonder(f"http://127.0.0.1:{serveur}/")

    assert sonde["code"] == 301
    assert sonde["location"] == "https://127.0.0.1/"
    assert "erreur" not in sonde


def test_sonder_rend_le_corps_d_un_200(serveur):
    sonde = sonder(f"http://127.0.0.1:{serveur}/ok")

    assert sonde["code"] == 200
    assert sonde["extrait"] == "bonjour"
    assert "location" not in sonde


class _Conteneur:
    def __init__(self, port):
        self.name = "copie-nginx-1"
        self.status = "running"
        self.labels = {"com.docker.compose.service": "nginx"}
        self.attrs = {"State": {"ExitCode": 0},
                      "NetworkSettings": {"Ports": {"80/tcp": [{"HostPort": str(port)}]}}}

    def reload(self):
        pass


def test_etape_de_sonde_racine_consigne_la_redirection(serveur, tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services:\n  nginx:\n    image: nginx\n")
    runner = ComposeRunner("copie-test", str(tmp_path), 1, logging.getLogger("sonde"))
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [_Conteneur(serveur)])
    monkeypatch.setattr(runner, "_capture_logs", lambda: "logs")
    investigations = []
    monkeypatch.setattr(Investigator, "investiguer", lambda self, echecs: investigations.append(echecs))

    resultat = runner._evaluer_services_persistants()

    http_ = next(s for s in resultat["steps"] if s["title"] == f"Sonde http://127.0.0.1:{serveur}/")
    assert http_["exit_code"] == 0
    assert http_["output"].startswith("code 301 → https://127.0.0.1/ : redirection active")
    sonde = next(s for s in resultat["probes"] if s["url"] == f"http://127.0.0.1:{serveur}/")
    assert sonde["code"] == 301 and sonde["location"] == "https://127.0.0.1/"
    # Le https sur ce port en clair échoue mécaniquement : bruit annoté, pas
    # un échec qui déclencherait une investigation.
    https_ = next(s for s in resultat["steps"] if s["title"] == f"Sonde https://127.0.0.1:{serveur}/")
    assert https_["exit_code"] == 0
    assert investigations == []


def test_chemins_declares_ne_suivent_pas_la_redirection(serveur, tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services:\n  nginx:\n    image: nginx\n")
    (tmp_path / "nginx.conf").write_text("server {\n  location /api/ { proxy_pass http://api; }\n}\n")
    runner = ComposeRunner("copie-test", str(tmp_path), 1, logging.getLogger("sonde"))
    # Le serveur de test parle en clair : on le fait passer pour la base TLS
    # déjà sondée, seul le chemin compte ici.
    base = {"service": "copie-nginx-1", "port": "443/tcp",
            "url": f"https://127.0.0.1:{serveur}/", "code": 200}
    import docker_eval.compose_runner as module
    monkeypatch.setattr(module, "sonder",
                        lambda url, **kw: sonder(url.replace("https://", "http://"), **kw))

    resultats = runner._sonder_chemins_declares([base])

    chemin = next(s for s in resultats if s["url"].endswith("/api/"))
    assert chemin["code"] == 301 and chemin["location"] == "https://127.0.0.1/"
    etape = next(s for s in runner.steps if s["title"] == f"Sonde https://127.0.0.1:{serveur}/api/")
    assert etape["exit_code"] == 0
    assert etape["output"].startswith("code 301 → https://127.0.0.1/ : redirection active")


def test_investigateur_ne_suit_pas_la_redirection(serveur, tmp_path):
    class RunnerFactice:
        steps = []

    inv = Investigator(RunnerFactice(), str(tmp_path), services=["copie-nginx-1"])

    assert inv._sonde(f"http://127.0.0.1:{serveur}/").startswith(
        "code 301 → https://127.0.0.1/ : redirection active")
    assert inv._sonde(f"http://127.0.0.1:{serveur}/ok") == "code 200\nbonjour"
