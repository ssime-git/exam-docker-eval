"""Sondes de comportement nginx : prouver les exigences en marche, pas par
la présence d'une directive (scriptorium, 469145, grille du 15/10).

La grille donne 35 points au comportement : routage A/B, limitation de débit
qui se déclenche, répliques API v1 qui reçoivent du trafic, 401 sur mauvais
identifiants, exporter et cible Prometheus UP. Le moteur ne relevait que des
codes de sonde à la racine et des requêtes rejouées : le barème lisait le
nginx.conf. Ces tests fixent le contrat `nginx_comportement` que le barème lit.
"""
import base64
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import compose_runner, sondes_nginx
from docker_eval.compose_runner import ComposeRunner
from docker_eval.sondes_nginx import (
    issue_tls_ancien,
    redirection_vers_https,
    routage_ab_declare,
    serveurs_upstream,
)

# Le nginx.conf de 469145, réduit à ce que les sondes lisent.
NGINX = """
http {
    limit_req_zone $binary_remote_addr zone=apilimit:10m rate=10r/s;
    upstream apis-v1 {
        server api-v1:8000;
    }
    upstream apis-v2 {
        server api-v2:8000;
    }
    map $http_x_experiment_group $api_backend {
        default  apis-v1;
        debug    apis-v2;
    }
    server { listen 80; return 301 https://$host$request_uri; }
    server {
        listen 443 ssl;
        location /predict {
            auth_basic "API"; auth_basic_user_file /etc/nginx/.htpasswd;
            limit_req zone=apilimit burst=5 nodelay;
            proxy_pass http://$api_backend;
        }
    }
}
"""
RUN_TESTS = """#!/bin/bash
r=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://localhost/predict" \\
     -H "Content-Type: application/json" -d '{"sentence": "so cool"}' --user admin:admin -k)
r=$(curl -s -X POST "https://localhost/predict" -H "Content-Type: application/json" \\
     -H "X-Experiment-Group: debug" -d '{"sentence": "so cool"}' --user admin:admin -k)
r=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://localhost/predict" \\
     -H "Content-Type: application/json" -d '{"sentence": "test"}' --user admin:wrongpassword -k)
"""
COMPOSE = """services:
  grafana:
    image: grafana/grafana:latest
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=admin
"""


def ecrire(chemin, contenu):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def copie(tmp_path, nginx=True):
    ecrire(tmp_path / "tests" / "run_tests.sh", RUN_TESTS)
    ecrire(tmp_path / "docker-compose.yml", COMPOSE)
    if nginx:
        ecrire(tmp_path / "deployments" / "nginx" / "nginx.conf", NGINX)
    return tmp_path


# --- Lecture du nginx.conf -------------------------------------------------

def test_le_map_donne_l_entete_et_la_valeur_de_la_variante():
    ab = routage_ab_declare(NGINX)
    assert ab["entete"] == "X-Experiment-Group"
    assert ab["valeur"] == "debug"
    assert ab["upstream_defaut"] == "apis-v1"


def test_sans_map_pas_de_routage_ab():
    assert routage_ab_declare("http { server { location / { proxy_pass http://api; } } }") is None


def test_les_serveurs_de_l_upstream_par_defaut_sont_les_services_v1():
    assert serveurs_upstream(NGINX, "apis-v1") == ["api-v1"]


# --- Redirection 80 → 443 -------------------------------------------------

def test_la_redirection_vers_https_se_lit_dans_la_sonde_racine_du_port_80():
    # La cible (443 du conteneur) n'est pas publiée telle quelle : la sonde
    # s'arrête en « redirection active », ce qui n'est pas une panne.
    sondes = [{"service": "p-nginx-1", "port": "80/tcp", "url": "http://127.0.0.1:32768/", "code": 301,
               "chaine": [{"code": 301, "location": "https://127.0.0.1/"}], "redirection_active": True}]
    assert redirection_vers_https(sondes) is True
    sondes[0].update(code=200, chaine=None, redirection_active=None)
    assert redirection_vers_https(sondes) is False
    assert redirection_vers_https([]) is None


# --- TLS ancien -----------------------------------------------------------

def test_le_refus_du_serveur_prouve_le_tls_ancien_refuse():
    assert issue_tls_ancien(None) is False  # poignée de main aboutie en TLS 1.1
    assert issue_tls_ancien(ssl.SSLError(1, "[SSL: TLSV1_ALERT_PROTOCOL_VERSION] tlsv1 alert protocol version")) is True
    assert issue_tls_ancien(ssl.SSLError(1, "[SSL: UNSUPPORTED_PROTOCOL] unsupported protocol")) is True
    # Le client local refuse d'offrir TLS 1.1 : on ne devine pas.
    assert issue_tls_ancien(ssl.SSLError(1, "[SSL: NO_PROTOCOLS_AVAILABLE] no protocols available")) is None
    assert issue_tls_ancien(ConnectionRefusedError(111, "Connection refused")) is None


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl absent")
def test_validite_du_certificat_lue_par_openssl(tmp_path):
    cle, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                    "-days", "30", "-keyout", str(cle), "-out", str(crt)], check=True, capture_output=True)
    assert sondes_nginx.certificat_pem_valide(crt.read_text()) is True
    assert sondes_nginx.certificat_pem_valide("pas un certificat") is None


# --- Les sondes de comportement, de bout en bout sur des doubles ----------

SONDES_RACINE = [
    {"service": "mlops-nginx-exam-2-nginx-1", "port": "80/tcp", "url": "http://127.0.0.1:32768/", "code": 301,
     "chaine": [{"code": 301, "location": "https://127.0.0.1/"}], "redirection_active": True},
    {"service": "mlops-nginx-exam-2-nginx-1", "port": "443/tcp", "url": "https://127.0.0.1:32769/", "code": 404},
    {"service": "mlops-nginx-exam-2-nginx-1", "port": "443/tcp", "url": "http://127.0.0.1:32769/", "code": 400},
    # nginx_exporter contient « nginx » : il ne doit pas servir de base TLS.
    {"service": "mlops-nginx-exam-2-nginx_exporter-1", "port": "9113/tcp", "url": "http://127.0.0.1:32770/",
     "code": 200},
    {"service": "mlops-nginx-exam-2-prometheus-1", "port": "9090/tcp", "url": "http://127.0.0.1:32771/",
     "code": 200},
    {"service": "mlops-nginx-exam-2-grafana-1", "port": "3000/tcp", "url": "http://127.0.0.1:32772/",
     "code": 200},
]

ADMIN = "Basic " + base64.b64encode(b"admin:admin").decode()
# Le préambule go_/process_ d'un vrai /metrics dépasse largement 300 octets.
METRICS = "".join(f"# HELP go_gc_{i} x\ngo_gc_{i} {i}\n" for i in range(60)) + \
    "# HELP nginx_up Status of the last metric scrape\nnginx_up 1\n"
CIBLES = json.dumps({"status": "success", "data": {"activeTargets": [
    {"labels": {"job": "nginx-exporter", "pad": "x" * 400}, "health": "up"},
    {"labels": {"job": "self"}, "health": "down"},
]}})


class FauxConteneur:
    def __init__(self, nom, service, lignes_predict=0):
        self.name = nom
        self.labels = {"com.docker.compose.service": service}
        self.status = "running"
        self._logs = ("INFO:     Started server process [1]\n"
                      + 'INFO:     172.30.0.5:40000 - "POST /predict HTTP/1.0" 200 OK\n' * lignes_predict).encode()

    def logs(self, **kwargs):
        return self._logs


def faux_sonder_469145():
    """nginx de 469145 : limit_req avant auth_basic, 6 passages par rafale."""
    verrou = threading.Lock()
    compteur = {"bons": 0}
    appels = []

    def sonder(url, entetes=None, corps=None, methode=None, timeout=10, contexte=None, taille_extrait=300):
        entetes = entetes or {}
        appels.append((methode or "GET", url, dict(entetes)))

        def rendre(code, corps_reponse=""):
            return {"code": code, "extrait": corps_reponse[:taille_extrait]}

        if url == "https://127.0.0.1:32769/predict" and methode == "POST":
            if entetes.get("Authorization") != ADMIN:
                return rendre(401, "<html>401 Authorization Required</html>")
            with verrou:
                compteur["bons"] += 1
                n = compteur["bons"]
            # 1-2 : A/B ; 3-22 : la rafale (6 passent) ; 23 : après la pause.
            if 9 <= n <= 22:
                return rendre(503, "<html>503 Service Temporarily Unavailable</html>")
            if entetes.get("X-Experiment-Group") == "debug":
                return rendre(200, '{"prediction": "fun", "prediction_proba_dict": {"fun": 0.4}}')
            return rendre(200, '{"prediction": "fun"}')
        if url == "http://127.0.0.1:32770/metrics":
            return rendre(200, METRICS)
        if url == "http://127.0.0.1:32771/api/v1/targets":
            return rendre(200, CIBLES)
        if url.startswith("http://127.0.0.1:32772/api/"):
            if entetes.get("Authorization") != ADMIN:
                return rendre(401, '{"message": "Unauthorized"}')
            if "/api/datasources" in url:
                return rendre(200, json.dumps([{"name": "Prometheus", "type": "prometheus"}]))
            return rendre(200, json.dumps([{"title": "nginx", "type": "dash-db"}, {"title": "api"}]))
        return rendre(404)

    return sonder, appels


def runner_469145(tmp_path, monkeypatch, nginx=True):
    runner = ComposeRunner("469145", str(copie(tmp_path, nginx)), timeout=60, logger=logging.getLogger("t"))
    monkeypatch.setattr(compose_runner, "PAUSE_LIMITATION_S", 0)
    sonder, appels = faux_sonder_469145()
    monkeypatch.setattr(compose_runner, "sonder", sonder)
    monkeypatch.setattr(sondes_nginx, "tls_ancien_refuse", lambda hote, port, timeout=10: True)
    monkeypatch.setattr(sondes_nginx, "certificat_valide", lambda hote, port, timeout=10: True)
    conteneurs = [
        FauxConteneur("mlops-nginx-exam-2-api-v1-1", "api-v1", 7),
        FauxConteneur("mlops-nginx-exam-2-api-v1-2", "api-v1", 6),
        FauxConteneur("mlops-nginx-exam-2-api-v1-3", "api-v1", 0),
        FauxConteneur("mlops-nginx-exam-2-api-v2-1", "api-v2", 9),
        FauxConteneur("mlops-nginx-exam-2-nginx-1", "nginx", 0),
    ]
    monkeypatch.setattr(runner, "_conteneurs_du_projet", lambda: conteneurs)
    return runner, appels


def test_les_sondes_de_comportement_prouvent_les_exigences_de_469145(tmp_path, monkeypatch):
    runner, appels = runner_469145(tmp_path, monkeypatch)
    comportement = runner._sonder_comportement_nginx(SONDES_RACINE)

    assert comportement["ab"] == {"entete": "X-Experiment-Group", "valeur": "debug", "code_defaut": 200,
                                  "code_variante": 200, "corps_differents": True}
    assert comportement["rafale"] == {"envoyees": 20, "codes": {"200": 6, "503": 14},
                                      "limite_declenchee": True, "retour_2xx_apres_pause": True}
    assert comportement["mauvais_identifiants"] == 401
    assert comportement["repartition"] == {
        "conteneurs": {"mlops-nginx-exam-2-api-v1-1": 7, "mlops-nginx-exam-2-api-v1-2": 6,
                       "mlops-nginx-exam-2-api-v1-3": 0},
        "recoivent": 2}
    assert comportement["monitoring"] == {"exporter_nginx_up": True, "prometheus_cibles_up": 1,
                                          "prometheus_cibles_down": 1, "grafana_datasources": 1,
                                          "grafana_dashboards": 2}
    assert comportement["redirection_80_443"] is True
    assert comportement["tls"] == {"tls_ancien_refuse": True, "certificat_valide": True}

    # Le corps et les en-têtes de la requête déclarée partent avec les POST.
    predict = [a for a in appels if a[0] == "POST"]
    assert all(e.get("Content-Type") == "application/json" for _, _, e in predict)
    # Aucun POST vers le port de l'exporter (base TLS mal choisie).
    assert not any("32770/predict" in u for _, u, _ in appels)

    etape = next(s for s in runner.steps if s["title"] == "Sondes de comportement nginx")
    assert etape["exit_code"] == 0
    for mot in ("routage A/B", "X-Experiment-Group: debug", "rafale", "503", "mauvais identifiants",
                "401", "répliques", "nginx_up", "Prometheus", "Grafana", "redirection", "TLS 1.1", "certificat"):
        assert mot in etape["output"], mot


def test_une_sonde_impossible_vaut_null_sans_faire_echouer(tmp_path, monkeypatch):
    runner, _ = runner_469145(tmp_path, monkeypatch)
    # Ni monitoring publié, ni port 80, ni conteneur lisible.
    sondes = [s for s in SONDES_RACINE if "443" in s["port"]]

    def casse():
        raise RuntimeError("docker injoignable")
    monkeypatch.setattr(runner, "_conteneurs_du_projet", casse)
    comportement = runner._sonder_comportement_nginx(sondes)
    assert comportement["repartition"] is None
    assert comportement["monitoring"] == {"exporter_nginx_up": None, "prometheus_cibles_up": None,
                                          "prometheus_cibles_down": None, "grafana_datasources": None,
                                          "grafana_dashboards": None}
    assert comportement["redirection_80_443"] is None
    assert comportement["ab"]["corps_differents"] is True


def test_sans_base_tls_les_sondes_predict_valent_null(tmp_path, monkeypatch):
    runner, _ = runner_469145(tmp_path, monkeypatch)
    sondes = [s for s in SONDES_RACINE if "443" not in s["port"]]
    comportement = runner._sonder_comportement_nginx(sondes)
    assert comportement["ab"] is None and comportement["rafale"] is None
    assert comportement["mauvais_identifiants"] is None
    assert comportement["tls"] == {"tls_ancien_refuse": None, "certificat_valide": None}
    assert comportement["monitoring"]["exporter_nginx_up"] is True


def _evaluer(runner, monkeypatch):
    monkeypatch.setattr(runner, "_record_service_states", lambda: {"nginx": ("running", 0)})
    monkeypatch.setattr(runner, "_sonder_ports_publies", lambda: [dict(s) for s in SONDES_RACINE])
    monkeypatch.setattr(runner, "_sonder_chemins_declares", lambda sondes: [])
    monkeypatch.setattr(runner, "_boucles_de_redemarrage", lambda: set())
    monkeypatch.setattr(runner, "_capture_logs", lambda: "")
    return runner._evaluer_services_persistants()


def test_l_evaluation_porte_nginx_comportement(tmp_path, monkeypatch):
    runner, _ = runner_469145(tmp_path, monkeypatch)
    resultat = _evaluer(runner, monkeypatch)
    assert resultat["nginx_comportement"]["mauvais_identifiants"] == 401
    json.dumps(resultat["nginx_comportement"])  # le barème le lit en JSON


def test_sans_nginx_conf_pas_de_cle_nginx_comportement(tmp_path, monkeypatch):
    runner, _ = runner_469145(tmp_path, monkeypatch, nginx=False)
    assert "nginx_comportement" not in _evaluer(runner, monkeypatch)


def test_post_sans_identifiants_mesure_le_defi_meme_si_get_est_refuse(tmp_path, monkeypatch):
    # 469217 (rejeu du 15/10) : /predict limité au POST, nginx rend 403 au GET
    # avant auth_basic. La seule sonde sans identifiants était ce GET : le
    # barème ne voyait jamais de 401 alors que l'authentification marche.
    runner, appels = runner_469145(tmp_path, monkeypatch)
    sonder_469145 = compose_runner.sonder

    def sonder(url, entetes=None, corps=None, methode=None, timeout=10, contexte=None, taille_extrait=300):
        if (methode or "GET") == "GET" and url.endswith("/predict"):
            appels.append(("GET", url, dict(entetes or {})))
            return {"code": 403, "extrait": "<html>403 Forbidden</html>"}
        return sonder_469145(url, entetes=entetes, corps=corps, methode=methode, timeout=timeout,
                             contexte=contexte, taille_extrait=taille_extrait)
    monkeypatch.setattr(compose_runner, "sonder", sonder)

    comportement = runner._sonder_comportement_nginx(SONDES_RACINE)
    assert comportement["sans_identifiants"] == 401
    # Même corps et mêmes en-têtes que la requête nominale, sans Authorization.
    sans = [e for m, u, e in appels if m == "POST" and u.endswith("/predict") and "Authorization" not in e]
    assert sans and all(e.get("Content-Type") == "application/json" for e in sans)
    # Mesuré avant la rafale : le seau de limit_req est encore plein.
    assert comportement["rafale"]["codes"] == {"200": 6, "503": 14}
    etape = next(s for s in runner.steps if s["title"] == "Sondes de comportement nginx")
    assert "POST sans identifiants : 401" in etape["output"]


def test_sans_base_tls_pas_de_mesure_sans_identifiants(tmp_path, monkeypatch):
    runner, _ = runner_469145(tmp_path, monkeypatch)
    sondes = [s for s in SONDES_RACINE if "443" not in s["port"]]
    assert runner._sonder_comportement_nginx(sondes)["sans_identifiants"] is None
