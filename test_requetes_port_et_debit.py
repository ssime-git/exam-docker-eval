"""Rejouer les requêtes déclarées vers le bon port, et sans prendre la
limitation de débit de la copie pour une panne (scriptorium, 469145, 15/10).

Le run_tests.sh de 469145 enchaîne des POST /predict sur https://localhost
(nginx, `limit_req ... burst=5 nodelay`), puis interroge Prometheus sur
http://localhost:9090/api/v1/status/runtimeinfo. Rejoués en rafale sur le
443 du proxy : deux 503 (la limitation que l'énoncé demande) et un 404
(Prometheus n'est pas derrière nginx) — trois « ce qui bloque » à tort.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import compose_runner
from docker_eval.compose_runner import ComposeRunner, racine_du_port, requetes_declarees

RUN_TESTS = """#!/bin/bash
response_v1=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://localhost/predict" \\
  --user admin:admin -H "Content-Type: application/json" -d '{"sentence": "ok"}' -k)
response_prometheus=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:9090/api/v1/status/runtimeinfo)
response_grafana=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/health)
"""
NGINX = "http {\n  limit_req_zone $binary_remote_addr zone=apilimit:10m rate=10r/s;\n" \
        "  server { location /predict { limit_req zone=apilimit burst=5 nodelay; } }\n}\n"


def ecrire(chemin, contenu):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def copie(tmp_path):
    ecrire(tmp_path / "tests" / "run_tests.sh", RUN_TESTS)
    ecrire(tmp_path / "deployments" / "nginx" / "nginx.conf", NGINX)
    return tmp_path



def test_requetes_declarees_gardent_leur_port(tmp_path):
    ports = {r["chemin"]: r["port"] for r in requetes_declarees(str(copie(tmp_path)))}
    assert ports == {"/predict": None, "/api/v1/status/runtimeinfo": 9090, "/api/health": 3000}


SONDES_RACINE = [
    {"service": "mlops-nginx", "port": "443/tcp", "url": "https://127.0.0.1:32769/", "code": 404},
    {"service": "mlops-nginx", "port": "443/tcp", "url": "http://127.0.0.1:32769/", "code": 400},
    {"service": "prometheus", "port": "9090/tcp", "url": "http://127.0.0.1:32770/", "code": 200},
    {"service": "prometheus", "port": "9090/tcp", "url": "https://127.0.0.1:32770/",
     "note": "échec attendu : ce port répond en http"},
]


def test_racine_du_port():
    assert racine_du_port(SONDES_RACINE, 9090) == "http://127.0.0.1:32770"
    assert racine_du_port(SONDES_RACINE, 3000) is None


def test_rejeu_vers_le_bon_port_et_apres_la_limitation(tmp_path, monkeypatch):
    runner = ComposeRunner("469145", str(copie(tmp_path)), timeout=60, logger=logging.getLogger("t"))
    monkeypatch.setattr(compose_runner, "chemins_nginx_declares", lambda d: ["/predict"])
    monkeypatch.setattr(compose_runner, "PAUSE_LIMITATION_S", 0)
    appels = []

    def faux_sonder(url, entetes=None, corps=None, methode="GET", timeout=10, contexte=None):
        appels.append((methode, url))
        if url.endswith("/predict") and methode == "POST":
            # Le premier POST tombe dans la rafale : 503 ; le second passe.
            return {"code": 503 if sum(1 for m, u in appels if u == url and m == "POST") == 1 else 200}
        if url == "http://127.0.0.1:32770/api/v1/status/runtimeinfo":
            return {"code": 200}
        return {"code": 401}
    monkeypatch.setattr(compose_runner, "sonder", faux_sonder)

    runner._sonder_chemins_declares(SONDES_RACINE)
    titres = {s["title"] if "title" in s else s.get("titre"): s for s in runner.steps}
    predict = titres["Requête déclarée (tests/run_tests.sh) : POST /predict"]
    assert "code 200" in predict["output"] and "limitation de débit" in predict["output"]
    prom = titres["Requête déclarée (tests/run_tests.sh) : GET /api/v1/status/runtimeinfo"]
    assert "127.0.0.1:32770" in prom["command"] and "404" not in prom["output"]
    grafana = titres["Requête déclarée (tests/run_tests.sh) : GET /api/health"]
    assert "non rejouée" in grafana["output"] and grafana["exit_code"] == 0
    assert not any(u.startswith("https://127.0.0.1:32769/api/") for _, u in appels), appels


def test_un_405_authentifie_dit_que_l_authentification_passe():
    """469145 : 401 sans identifiants, 405 avec (GET sur une route POST) —
    lu « identifiants introuvables » alors que l'authentification passait."""
    from docker_eval.compose_runner import sens_sonde_authentifiee
    assert "authentification acceptée" in sens_sonde_authentifiee(401, 405)
    assert "GET" in sens_sonde_authentifiee(401, 405)
    assert sens_sonde_authentifiee(401, 401) == ""
    assert "authentification acceptée" in sens_sonde_authentifiee(401, 200)


def test_la_requete_negative_de_la_copie_attend_son_401(tmp_path, monkeypatch):
    """Le test 3 de 469145 envoie admin:wrongpassword et attend 401."""
    ecrire(tmp_path / "tests" / "run_tests.sh",
           'r=$(curl -s -X POST "https://localhost/predict" -d \'{"s": 1}\' --user admin:wrongpassword)\n')
    runner = ComposeRunner("469145", str(tmp_path), timeout=60, logger=logging.getLogger("t"))
    monkeypatch.setattr(compose_runner, "chemins_nginx_declares", lambda d: ["/predict"])
    monkeypatch.setattr(compose_runner, "sonder", lambda url, **k: {"code": 401})
    runner._sonder_chemins_declares(SONDES_RACINE)
    etape = next(s for s in runner.steps if "Requête déclarée" in (s.get("title") or s.get("titre") or ""))
    assert "requête négative" in etape["output"] and "attendue" in etape["output"]
