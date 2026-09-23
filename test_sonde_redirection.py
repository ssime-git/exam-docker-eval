"""Une redirection est consignée, suivie, et jamais prise pour une panne
(scriptorium #320).

457405, 461091 : nginx répond 301 vers https://127.0.0.1/ sur le port 80.
La sonde suivait vers le 443, non publié sur l'hôte, et enregistrait
« Connection refused ». Règle : on suit la redirection ; si la cible répond,
`code` est le code final (les barèmes ne perdent rien) et le premier saut
est consigné ; si la cible est injoignable depuis le harnais, `code` est le
3xx et la redirection est déclarée active.
"""
import http.server
import logging
import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval.compose_runner import ComposeRunner
from docker_eval.investigator import Investigator
from docker_eval.sonde_http import SAUTS_MAX, sonder


def _port_ferme() -> int:
    """Un port où rien n'écoute : l'équivalent du 443 non publié."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT_FERME = _port_ferme()
CIBLE_INJOIGNABLE = f"https://127.0.0.1:{PORT_FERME}/"


class _Gestionnaire(http.server.BaseHTTPRequestHandler):
    def _rediriger(self, code, location):
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/ok"):
            corps = b"bonjour"
            self.send_response(200)
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
        elif self.path == "/vers-ok":
            self._rediriger(301, "/ok")
        elif self.path == "/api":
            # nginx : `location /api/` sert /api par un 301 relatif vers /api/.
            self._rediriger(301, "/ok/api/")
        elif self.path == "/a":
            self._rediriger(301, "/b")
        elif self.path == "/b":
            self._rediriger(302, "/ok")
        elif self.path == "/boucle":
            self._rediriger(302, "/boucle")
        else:
            # La forme de nginx : `return 301 https://$host$request_uri;`,
            # vers un port que la stack ne publie pas.
            self._rediriger(301, CIBLE_INJOIGNABLE)

    def log_message(self, *_args):
        pass


@pytest.fixture
def serveur():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gestionnaire)
    fil = threading.Thread(target=httpd.serve_forever, daemon=True)
    fil.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


# --- sonder -------------------------------------------------------------------

def test_redirection_vers_une_cible_qui_repond(serveur):
    sonde = sonder(f"http://127.0.0.1:{serveur}/vers-ok")

    assert sonde["code"] == 200
    assert sonde["extrait"] == "bonjour"
    assert sonde["redirection"] == {"code": 301, "location": "/ok"}
    assert "redirection_active" not in sonde


def test_redirection_vers_une_cible_injoignable(serveur):
    sonde = sonder(f"http://127.0.0.1:{serveur}/")

    assert sonde["code"] == 301
    assert sonde["redirection"] == {"code": 301, "location": CIBLE_INJOIGNABLE}
    assert sonde["redirection_active"] is True
    assert "refused" in sonde["raison"].lower()
    assert "erreur" not in sonde


def test_chaine_de_deux_redirections(serveur):
    sonde = sonder(f"http://127.0.0.1:{serveur}/a")

    assert sonde["code"] == 200
    assert sonde["redirection"] == {"code": 301, "location": "/b"}
    assert [(s["code"], s["location"]) for s in sonde["chaine"]] == [(301, "/b"), (302, "/ok")]


def test_boucle_de_redirection_bornee(serveur):
    sonde = sonder(f"http://127.0.0.1:{serveur}/boucle")

    assert sonde["code"] == 302
    assert sonde["redirection_active"] is True
    assert len(sonde["chaine"]) == SAUTS_MAX
    assert "boucle" in sonde["raison"]


def test_sans_redirection_rien_ne_change(serveur):
    sonde = sonder(f"http://127.0.0.1:{serveur}/ok")

    assert sonde == {"code": 200, "extrait": "bonjour"}


# --- les étapes ---------------------------------------------------------------

class _Conteneur:
    def __init__(self, port):
        self.name = "copie-nginx-1"
        self.status = "running"
        self.labels = {"com.docker.compose.service": "nginx"}
        self.attrs = {"State": {"ExitCode": 0},
                      "NetworkSettings": {"Ports": {"80/tcp": [{"HostPort": str(port)}]}}}

    def reload(self):
        pass


def test_etape_racine_redirection_active_n_est_pas_une_panne(serveur, tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services:\n  nginx:\n    image: nginx\n")
    runner = ComposeRunner("copie-test", str(tmp_path), 1, logging.getLogger("sonde"))
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: [_Conteneur(serveur)])
    monkeypatch.setattr(runner, "_capture_logs", lambda: "logs")
    investigations = []
    monkeypatch.setattr(Investigator, "investiguer", lambda self, echecs: investigations.append(echecs))

    resultat = runner._evaluer_services_persistants()

    http_ = next(s for s in resultat["steps"] if s["title"] == f"Sonde http://127.0.0.1:{serveur}/")
    assert http_["exit_code"] == 0
    assert http_["output"].startswith(
        f"code 301 → {CIBLE_INJOIGNABLE} : redirection active "
        "(cible non joignable depuis le harnais : ")
    sonde = next(s for s in resultat["probes"] if s["url"] == f"http://127.0.0.1:{serveur}/")
    assert sonde["code"] == 301 and sonde["redirection_active"] is True
    https_ = next(s for s in resultat["steps"] if s["title"] == f"Sonde https://127.0.0.1:{serveur}/")
    assert https_["exit_code"] == 0
    assert investigations == []


def test_chemin_declare_qui_redirige_garde_son_200(serveur, tmp_path, monkeypatch):
    """Barème nginx : un chemin déclaré servi par un 301 relatif compte son 2xx."""
    (tmp_path / "docker-compose.yml").write_text("services:\n  nginx:\n    image: nginx\n")
    (tmp_path / "nginx.conf").write_text("server {\n  location /api { proxy_pass http://api; }\n}\n")
    runner = ComposeRunner("copie-test", str(tmp_path), 1, logging.getLogger("sonde"))
    # Le serveur de test parle en clair : on le fait passer pour la base TLS
    # déjà sondée, seul le chemin compte ici.
    base = {"service": "copie-nginx-1", "port": "443/tcp",
            "url": f"https://127.0.0.1:{serveur}/", "code": 200}
    import docker_eval.compose_runner as module
    monkeypatch.setattr(module, "sonder",
                        lambda url, **kw: sonder(url.replace("https://", "http://"), **kw))

    resultats = runner._sonder_chemins_declares([base])

    chemin = next(s for s in resultats if s["url"].endswith("/api"))
    assert chemin["code"] == 200
    assert chemin["redirection"] == {"code": 301, "location": "/ok/api/"}
    etape = next(s for s in runner.steps if s["title"] == f"Sonde https://127.0.0.1:{serveur}/api")
    assert etape["exit_code"] == 0
    assert etape["output"].startswith("code 200 : 301 → /ok/api/ puis 200")


def test_investigateur(serveur, tmp_path):
    class RunnerFactice:
        steps = []

    inv = Investigator(RunnerFactice(), str(tmp_path), services=["copie-nginx-1"])

    assert inv._sonde(f"http://127.0.0.1:{serveur}/").startswith(
        f"code 301 → {CIBLE_INJOIGNABLE} : redirection active (cible non joignable")
    assert inv._sonde(f"http://127.0.0.1:{serveur}/vers-ok") == "code 200 : 301 → /ok puis 200\nbonjour"
    assert inv._sonde(f"http://127.0.0.1:{serveur}/ok") == "code 200\nbonjour"
