"""Rejouer une requête déclarée sur le port hôte d'ORIGINE du Compose
(scriptorium, 467379, 15/10).

Le compose de 467379 publie "8082:8080", "9092:9090", "3032:3000" et
"9100:9100" ; son Makefile fait `curl -f http://localhost:8082/`, etc. Le
moteur relâche les ports hôte (Docker en tire d'autres, 32768…), puis
cherchait le port déclaré parmi les ports CONTENEUR : seul 9100 (hôte =
conteneur) était rejoué, les trois autres sortaient « non rejouée : le port
8082 n'est publié par aucun conteneur », et l'aval en faisait un bloquant
« Le service ne répond pas » sur une copie qui marche.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import compose_runner
from docker_eval.compose_runner import ComposeRunner

COMPOSE_467379 = """services:
  api:
    build: .
    ports:
      - "8082:8080"
  prometheus:
    image: prom/prometheus
    ports:
      - "9092:9090"
  grafana:
    image: grafana/grafana
    ports:
      - "3032:3000"
  node-exporter:
    image: prom/node-exporter
    ports:
      - "9100:9100"
"""
MAKEFILE_467379 = """check:
\tcurl -f http://localhost:8082/
\tcurl -f http://localhost:9092/-/ready
\tcurl -f http://localhost:3032/api/health
\tcurl -f http://localhost:9100/metrics
\tcurl -f http://localhost:7777/absent
"""

# Ce que relit `_sonder_ports_publies` après relâchement : ports conteneur,
# ports hôte tirés par Docker.
SONDES_467379 = [
    {"service": "eval_467379-api-1", "compose_service": "api", "port": "8080/tcp",
     "url": "http://127.0.0.1:32801/", "code": 200},
    {"service": "eval_467379-prometheus-1", "compose_service": "prometheus", "port": "9090/tcp",
     "url": "http://127.0.0.1:32802/", "code": 302},
    {"service": "eval_467379-grafana-1", "compose_service": "grafana", "port": "3000/tcp",
     "url": "http://127.0.0.1:32803/", "code": 302},
    {"service": "eval_467379-node-exporter-1", "compose_service": "node-exporter", "port": "9100/tcp",
     "url": "http://127.0.0.1:32804/", "code": 200},
]


def ecrire(chemin, contenu):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def _etapes(runner):
    return {s.get("title") or s.get("titre"): s for s in runner.steps}


def test_467379_le_port_hote_d_origine_est_rejoue_sur_le_port_releche(tmp_path, monkeypatch):
    ecrire(tmp_path / "docker-compose.yml", COMPOSE_467379)
    ecrire(tmp_path / "Makefile", MAKEFILE_467379)
    runner = ComposeRunner("467379", str(tmp_path), timeout=60, logger=logging.getLogger("t"))
    appels = []

    def faux_sonder(url, **k):
        appels.append(url)
        return {"code": 200}
    monkeypatch.setattr(compose_runner, "sonder", faux_sonder)

    runner._sonder_chemins_declares(SONDES_467379)
    etapes = _etapes(runner)
    attendus = {
        "GET /": "http://127.0.0.1:32801/",
        "GET /-/ready": "http://127.0.0.1:32802/-/ready",
        "GET /api/health": "http://127.0.0.1:32803/api/health",
        "GET /metrics": "http://127.0.0.1:32804/metrics",
    }
    for requete, url in attendus.items():
        etape = etapes[f"Requête déclarée (Makefile) : {requete}"]
        assert "non rejouée" not in etape["output"], etape
        assert url in appels, (requete, appels)
        assert "code 200" in etape["output"]
    # La commande consignée dit le détour : port déclaré → port relâché.
    assert "8082" in etapes["Requête déclarée (Makefile) : GET /"]["command"]


def test_un_port_publie_nulle_part_reste_non_rejoue_et_le_dit(tmp_path, monkeypatch):
    ecrire(tmp_path / "docker-compose.yml", COMPOSE_467379)
    ecrire(tmp_path / "Makefile", MAKEFILE_467379)
    runner = ComposeRunner("467379", str(tmp_path), timeout=60, logger=logging.getLogger("t"))
    monkeypatch.setattr(compose_runner, "sonder", lambda url, **k: {"code": 200})
    runner._sonder_chemins_declares(SONDES_467379)
    etape = _etapes(runner)["Requête déclarée (Makefile) : GET /absent"]
    sortie = etape["output"]
    assert sortie.startswith("non rejouée") and etape["exit_code"] == 0
    # C'est le Makefile qui vise un port non publié : pas une panne du service.
    assert "Makefile" in sortie and "7777" in sortie
    assert "ne publie" in sortie and "pas une panne" in sortie


def test_sans_service_compose_dans_la_sonde_le_port_conteneur_suffit(tmp_path, monkeypatch):
    """Sondes anciennes (sans `compose_service`) : la correspondance passe
    par le port conteneur publié."""
    ecrire(tmp_path / "docker-compose.yml", COMPOSE_467379)
    ecrire(tmp_path / "Makefile", "t:\n\tcurl -f http://localhost:9092/-/ready\n")
    runner = ComposeRunner("467379", str(tmp_path), timeout=60, logger=logging.getLogger("t"))
    appels = []
    monkeypatch.setattr(compose_runner, "sonder", lambda url, **k: appels.append(url) or {"code": 200})
    sondes = [{k: v for k, v in s.items() if k != "compose_service"} for s in SONDES_467379]
    runner._sonder_chemins_declares(sondes)
    assert appels == ["http://127.0.0.1:32802/-/ready"]


def test_formes_de_ports_compose(tmp_path):
    ecrire(tmp_path / ".env", "API_PORT=8085\n")
    ecrire(tmp_path / "docker-compose.yml", """services:
  a:
    ports:
      - "8082:8080"
      - "127.0.0.1:9092:9090"
      - 3032:3000/tcp
      - "[::1]:5001:5000"
      - "6000-6001:7000-7001"
      - "${API_PORT}:8000"
      - "${GRAFANA_PORT:-3033}:3000"
      - "4000"
      - 4100
      - "127.0.0.1::4200"
  b:
    ports:
      - published: 8443
        target: 443
      - published: "9443"
        target: 9443
        protocol: tcp
      - target: 1234
""")
    from docker_eval.compose_runner import ports_hote_publies
    publies = ports_hote_publies(str(tmp_path))
    assert publies == {
        8082: [("a", 8080)],
        9092: [("a", 9090)],
        3032: [("a", 3000)],
        5001: [("a", 5000)],
        6000: [("a", 7000)],
        6001: [("a", 7001)],
        8085: [("a", 8000)],
        3033: [("a", 3000)],
        8443: [("b", 443)],
        9443: [("b", 9443)],
    }


def test_sans_compose_lisible_rien_n_est_publie(tmp_path):
    from docker_eval.compose_runner import ports_hote_publies
    assert ports_hote_publies(str(tmp_path)) == {}
    ecrire(tmp_path / "docker-compose.yml", "services: [\n")
    assert ports_hote_publies(str(tmp_path)) == {}


def test_port_publie_dont_le_conteneur_manque_n_est_pas_un_non_rejoue(tmp_path, monkeypatch):
    """8082 publie api:8080, mais aucun conteneur api debout : c'est une
    vraie absence de service, pas une absence de mesure. La sortie ne
    commence pas par « non rejouée » (que l'aval range en bruit)."""
    ecrire(tmp_path / "docker-compose.yml", COMPOSE_467379)
    ecrire(tmp_path / "Makefile", "t:\n\tcurl -f http://localhost:8082/\n")
    runner = ComposeRunner("467379", str(tmp_path), timeout=60, logger=logging.getLogger("t"))
    monkeypatch.setattr(compose_runner, "sonder", lambda url, **k: {"code": 200})
    runner._sonder_chemins_declares([s for s in SONDES_467379 if s["compose_service"] != "api"])
    etape = _etapes(runner)["Requête déclarée (Makefile) : GET /"]
    assert not etape["output"].startswith("non rejouée")
    assert "api:8080" in etape["output"] and etape["exit_code"] == 1


def test_hote_prioritaire_sur_un_port_conteneur_homonyme(tmp_path, monkeypatch):
    """`"8080:80"` sur nginx, et une api dont le port CONTENEUR est 8080 :
    `curl localhost:8080` parle à nginx:80."""
    ecrire(tmp_path / "docker-compose.yml",
           "services:\n  nginx:\n    ports: ['8080:80']\n  api:\n    expose: ['8080']\n")
    ecrire(tmp_path / "Makefile", "t:\n\tcurl -f http://localhost:8080/health\n")
    runner = ComposeRunner("x", str(tmp_path), timeout=60, logger=logging.getLogger("t"))
    appels = []
    monkeypatch.setattr(compose_runner, "sonder", lambda url, **k: appels.append(url) or {"code": 200})
    runner._sonder_chemins_declares([
        {"service": "x-api-1", "compose_service": "api", "port": "8080/tcp",
         "url": "http://127.0.0.1:32900/", "code": 200},
        {"service": "x-nginx-1", "compose_service": "nginx", "port": "80/tcp",
         "url": "http://127.0.0.1:32901/", "code": 200},
    ])
    assert appels == ["http://127.0.0.1:32901/health"]
