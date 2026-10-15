"""Sondes de comportement prometheus-grafana : prouver les exigences en marche,
pas par la présence d'un mot-clé (scriptorium, 467439 et 466056, grille du
15/10).

La grille donne ses points à une stack qui fonctionne : Prometheus prêt, les
cibles api/node-exporter UP, des métriques applicatives exposées par l'API,
des règles d'alerte chargées, Grafana provisionné (source Prometheus, tableaux
de bord et leurs requêtes), un endpoint de dérive qui fait bouger une jauge.
Le moteur ne relevait que les codes des sondes racine : le barème lisait les
fichiers. Ces tests fixent le contrat `prometheus_comportement` que le barème
lit. Les doubles suivent les formes réelles : /api/v1/targets et
/api/v1/rules de Prometheus, /api/search et /api/dashboards/uid de Grafana,
format d'exposition texte de Prometheus.
"""
import base64
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import compose_runner, sondes_prometheus
from docker_eval.compose_runner import ComposeRunner
from docker_eval.sondes_prometheus import (
    duree_prometheus,
    endpoint_drift_declare,
    est_examen_prometheus,
    familles_metriques,
    requetes_des_panels,
    resumer_cibles,
    resumer_regles,
    series_drift,
)

# Le Makefile de 467439, réduit : l'URL passe par $(API_URL), que
# `requetes_declarees` (localhost en dur) ne voit pas.
MAKEFILE = """API_URL ?= http://localhost:8080
fire-alert:
\t@curl -sf -X POST $(API_URL)/trigger-drift
\t@echo "Drift simulated."
"""
COMPOSE = """services:
  bike-api:
    build: ./src/api
    ports: ["8080:8080"]
  prometheus:
    image: prom/prometheus:v3.15.0
    ports: ["9090:9090"]
  grafana:
    image: grafana/grafana:13.2.3
    ports: ["3000:3000"]
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=${GF_PASS}
  node-exporter:
    image: prom/node-exporter:v1.12.1
    ports: ["9100:9100"]
"""
PROMETHEUS_YML = """global:
  scrape_interval: 15s
rule_files: ["rules/*.yml"]
scrape_configs:
  - job_name: bike-api
    static_configs: [{targets: ['bike-api:8080']}]
"""
SECRET = "s3cr3t-grafana-467439"
AUTH = "Basic " + base64.b64encode(f"admin:{SECRET}".encode()).decode()


def ecrire(chemin, contenu=""):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def copie(tmp_path, nginx=False, prometheus=True):
    ecrire(tmp_path / "docker-compose.yml", COMPOSE)
    ecrire(tmp_path / "Makefile", MAKEFILE)
    # Le .env de la copie (ou celui que le harnais a généré) : la valeur de
    # ${GF_PASS} ne doit jamais apparaître dans une étape.
    ecrire(tmp_path / ".env", f"GF_PASS={SECRET}\n")
    ecrire(tmp_path / "src" / "api" / "main.py",
           '@app.post("/evaluate")\ndef evaluate(p): ...\n@app.post("/trigger-drift")\ndef t(): ...\n')
    if prometheus:
        ecrire(tmp_path / "deployment" / "prometheus" / "prometheus.yml", PROMETHEUS_YML)
    if nginx:
        ecrire(tmp_path / "deployment" / "nginx" / "nginx.conf", "http { }")
    return tmp_path


# --- Détection de l'examen ------------------------------------------------

def test_un_prometheus_yml_sans_nginx_conf_est_un_examen_prometheus(tmp_path):
    assert est_examen_prometheus(str(copie(tmp_path))) is True


def test_un_nginx_conf_fait_un_examen_nginx_meme_avec_prometheus(tmp_path):
    assert est_examen_prometheus(str(copie(tmp_path, nginx=True))) is False


def test_sans_prometheus_yml_pas_d_examen_prometheus(tmp_path):
    assert est_examen_prometheus(str(copie(tmp_path, prometheus=False))) is False


# --- Lectures pures -------------------------------------------------------

# Format d'exposition texte, tel que prometheus_client le rend.
METRIQUES_AVANT = """# HELP python_gc_objects_collected_total Objects collected during gc
# TYPE python_gc_objects_collected_total counter
python_gc_objects_collected_total{generation="0"} 312.0
# HELP api_requests_total Total requests
# TYPE api_requests_total counter
api_requests_total{endpoint="/predict",method="POST",status_code="200"} 12.0
# HELP api_requests_created Total requests
# TYPE api_requests_created gauge
api_requests_created{endpoint="/predict",method="POST",status_code="200"} 1.7e+09
# HELP api_request_duration_seconds Latency
# TYPE api_request_duration_seconds histogram
api_request_duration_seconds_bucket{endpoint="/predict",le="0.1"} 10.0
api_request_duration_seconds_bucket{endpoint="/predict",le="+Inf"} 12.0
api_request_duration_seconds_count{endpoint="/predict"} 12.0
api_request_duration_seconds_sum{endpoint="/predict"} 0.8
# HELP model_rmse_score RMSE
# TYPE model_rmse_score gauge
model_rmse_score 0.0
# HELP evidently_drift_share Share of drifted features
# TYPE evidently_drift_share gauge
evidently_drift_share 0.0
# HELP evidently_data_drift_detected_status 1 if drift
# TYPE evidently_data_drift_detected_status gauge
evidently_data_drift_detected_status 0.0
"""
METRIQUES_APRES = METRIQUES_AVANT.replace("evidently_drift_share 0.0", "evidently_drift_share 0.91") \
    .replace("evidently_data_drift_detected_status 0.0", "evidently_data_drift_detected_status 1.0")


def test_les_familles_de_metriques_se_lisent_dans_les_lignes_type():
    noms, types = familles_metriques(METRIQUES_AVANT)
    assert "api_requests_total" in noms and "evidently_drift_share" in noms
    assert types["api_requests_total"] == "counter"
    assert types["api_request_duration_seconds"] == "histogram"
    assert types["model_rmse_score"] == "gauge"
    noms, _ = familles_metriques(METRIQUES_AVANT, maxi=2)
    assert len(noms) == 2


def test_les_series_de_derive_sont_toutes_celles_dont_le_nom_dit_drift():
    assert series_drift(METRIQUES_AVANT) == {"evidently_drift_share": 0.0,
                                             "evidently_data_drift_detected_status": 0.0}
    assert series_drift(METRIQUES_APRES)["evidently_drift_share"] == 0.91


def test_une_jauge_restee_nan_n_est_pas_modifiee():
    # e2e 467439 : les jauges Evidently valent NaN avant la première dérive.
    avant = series_drift("evidently_drift_share NaN\nevidently_x_drift 1.0\n")
    assert sondes_prometheus.series_changees(avant, dict(avant)) == []
    apres = series_drift("evidently_drift_share 0.9\nevidently_x_drift 1.0\nnew_drift 0\n")
    assert sondes_prometheus.series_changees(avant, apres) == ["evidently_drift_share", "new_drift"]


# Forme réelle de /api/v1/targets (Prometheus 2.x/3.x).
CIBLES = {"status": "success", "data": {"activeTargets": [
    {"discoveredLabels": {"__address__": "bike-api:8080"}, "labels": {"instance": "bike-api:8080", "job": "bike-api"},
     "scrapePool": "bike-api", "scrapeUrl": "http://bike-api:8080/metrics", "lastError": "", "health": "up"},
    {"labels": {"instance": "node-exporter:9100", "job": "node-exporter"}, "scrapePool": "node-exporter",
     "lastError": "", "health": "up"},
    {"labels": {"instance": "localhost:9090", "job": "prometheus"}, "scrapePool": "prometheus",
     "lastError": "", "health": "up"},
    {"labels": {"instance": "cadvisor:8080", "job": "cadvisor"}, "scrapePool": "cadvisor",
     "lastError": "dial tcp: lookup cadvisor: no such host", "health": "down"},
], "droppedTargets": []}}


def test_les_cibles_se_comptent_par_sante_et_par_job():
    assert resumer_cibles(CIBLES) == {"up": 3, "down": 1,
                                      "jobs_up": ["bike-api", "node-exporter", "prometheus"],
                                      "jobs_down": ["cadvisor"]}


def test_une_cible_pas_encore_scrapee_n_est_pas_up():
    donnees = {"status": "success", "data": {"activeTargets": [
        {"labels": {"job": "bike-api"}, "health": "unknown"}]}}
    resume = resumer_cibles(donnees)
    assert resume["up"] == 0 and resume["down"] == 1 and resume["jobs_down"] == ["bike-api"]


# Forme réelle de /api/v1/rules : `duration` en secondes, `query` l'expression.
REGLES = {"status": "success", "data": {"groups": [
    {"name": "bike-api-alerts", "file": "/etc/prometheus/rules/alert_rules.yml", "interval": 15, "rules": [
        {"state": "inactive", "name": "BikeApiDown", "query": "up{job=\"bike-api\"} == 0", "duration": 60,
         "keepFiringFor": 0, "labels": {"severity": "critical"},
         "annotations": {"summary": "API down"}, "alerts": [], "health": "ok", "type": "alerting"},
        {"state": "inactive", "name": "ModelDataDriftHigh", "query": "evidently_drift_share > 0.8",
         "duration": 30, "labels": {"severity": "warning"}, "annotations": {}, "alerts": [], "health": "ok",
         "type": "alerting"},
        {"name": "job:api_requests:rate5m", "query": "sum(rate(api_requests_total[5m]))", "health": "err",
         "lastError": "boom", "type": "recording"},
    ]}]}}


def test_les_regles_disent_les_alertes_et_leurs_erreurs():
    assert resumer_regles(REGLES) == {
        "chargees": 3,
        "alertes": [
            {"nom": "BikeApiDown", "expr": "up{job=\"bike-api\"} == 0", "for": "1m", "severity": "critical",
             "annotations": True},
            {"nom": "ModelDataDriftHigh", "expr": "evidently_drift_share > 0.8", "for": "30s",
             "severity": "warning", "annotations": False},
        ],
        "erreurs": 1,
    }


def test_duree_prometheus():
    assert duree_prometheus(60) == "1m"
    assert duree_prometheus(90) == "1m30s"
    assert duree_prometheus(3600) == "1h"
    assert duree_prometheus(0) is None and duree_prometheus(None) is None


# Forme réelle de /api/dashboards/uid/<uid> : panneaux de premier niveau,
# lignes repliées qui portent leurs panneaux, ancien schéma `rows`.
TABLEAU = {"meta": {"slug": "api"}, "dashboard": {"uid": "api", "title": "API", "panels": [
    {"type": "timeseries", "targets": [{"refId": "A", "expr": "rate(api_requests_total[5m])"}]},
    {"type": "row", "collapsed": True, "panels": [
        {"type": "stat", "targets": [{"refId": "A", "expr": "evidently_drift_share"},
                                     {"refId": "B", "expr": "rate(api_requests_total[5m])"}]}]},
]}}
ANCIEN = {"dashboard": {"rows": [{"panels": [{"targets": [{"expr": "up"}]}]}]}}


def test_les_requetes_des_panneaux_se_lisent_a_tous_les_niveaux():
    assert requetes_des_panels(TABLEAU) == ["rate(api_requests_total[5m])", "evidently_drift_share"]
    assert requetes_des_panels(ANCIEN) == ["up"]


def test_l_endpoint_de_derive_se_lit_dans_le_makefile(tmp_path):
    assert endpoint_drift_declare(str(copie(tmp_path)))["chemin"] == "/trigger-drift"
    assert endpoint_drift_declare(str(copie(tmp_path)))["methode"] == "POST"


def test_sans_makefile_l_endpoint_de_derive_se_lit_dans_les_routes(tmp_path):
    copie(tmp_path)
    os.remove(tmp_path / "Makefile")
    assert endpoint_drift_declare(str(tmp_path)) == {"chemin": "/trigger-drift", "methode": "POST",
                                                     "source": os.path.join("src", "api", "main.py")}


# --- Les sondes de comportement, de bout en bout sur des doubles ----------

SONDES_RACINE = [
    # L'API entraîne au démarrage : la racine peut ne pas encore répondre.
    {"service": "eval_467439-bike-api-1", "port": "8080/tcp", "url": "http://127.0.0.1:32780/",
     "code": "non reçu", "erreur": "Connection refused"},
    {"service": "eval_467439-bike-api-1", "port": "8080/tcp", "url": "https://127.0.0.1:32780/",
     "code": "non reçu", "erreur": "Connection refused"},
    {"service": "eval_467439-prometheus-1", "port": "9090/tcp", "url": "http://127.0.0.1:32781/", "code": 200},
    {"service": "eval_467439-grafana-1", "port": "3000/tcp", "url": "http://127.0.0.1:32782/", "code": 200},
    {"service": "eval_467439-node-exporter-1", "port": "9100/tcp", "url": "http://127.0.0.1:32783/",
     "code": 200},
]
NODE = "# HELP node_cpu_seconds_total x\n# TYPE node_cpu_seconds_total counter\nnode_cpu_seconds_total 1\n"
RECHERCHE = [
    {"id": 1, "uid": "api", "title": "API", "uri": "db/api", "url": "/d/api/api", "type": "dash-db", "tags": []},
    {"id": 2, "uid": "infra", "title": "Infra", "url": "/d/infra/infra", "type": "dash-db", "tags": []},
]
INFRA = {"dashboard": {"uid": "infra", "panels": [
    {"type": "gauge", "targets": [{"expr": "100 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100"}]}]}}


def faux_sonder_467439():
    etat = {"derive": False}
    appels = []

    def sonder(url, entetes=None, corps=None, methode=None, timeout=10, contexte=None, taille_extrait=300):
        entetes = entetes or {}
        appels.append((methode or "GET", url, dict(entetes), timeout))

        def rendre(code, corps_reponse=""):
            if not isinstance(corps_reponse, str):
                corps_reponse = json.dumps(corps_reponse)
            return {"code": code, "extrait": corps_reponse[:taille_extrait]}

        if url == "http://127.0.0.1:32780/metrics":
            return rendre(200, METRIQUES_APRES if etat["derive"] else METRIQUES_AVANT)
        if url == "http://127.0.0.1:32780/trigger-drift" and methode == "POST":
            etat["derive"] = True
            return rendre(200, '{"drift_detected": 1}')
        if url == "http://127.0.0.1:32783/metrics":
            return rendre(200, NODE)
        if url == "http://127.0.0.1:32781/-/ready":
            return rendre(200, "Prometheus Server is Ready.\n")
        if url == "http://127.0.0.1:32781/api/v1/targets":
            return rendre(200, CIBLES)
        if url == "http://127.0.0.1:32781/api/v1/rules":
            return rendre(200, REGLES)
        if url == "http://127.0.0.1:32782/api/health":
            return rendre(200, {"database": "ok", "version": "13.2.3"})
        if url.startswith("http://127.0.0.1:32782/api/"):
            if entetes.get("Authorization") != AUTH:
                return rendre(401, {"message": "Unauthorized"})
            if url.endswith("/api/datasources"):
                return rendre(200, [{"uid": "prom", "name": "Prometheus", "type": "prometheus",
                                     "url": "http://prometheus:9090", "isDefault": True}])
            if url.endswith("/api/search?type=dash-db"):
                return rendre(200, RECHERCHE)
            if url.endswith("/api/dashboards/uid/api"):
                return rendre(200, TABLEAU)
            if url.endswith("/api/dashboards/uid/infra"):
                return rendre(200, INFRA)
        return {"code": "non reçu", "erreur": "Connection refused", "extrait": "Connection refused"}

    return sonder, appels


def runner_467439(tmp_path, monkeypatch, **options):
    runner = ComposeRunner("467439", str(copie(tmp_path, **options)), timeout=60, logger=logging.getLogger("t"))
    sonder, appels = faux_sonder_467439()
    monkeypatch.setattr(compose_runner, "sonder", sonder)
    monkeypatch.setattr(compose_runner, "ATTENTE_API_S", 0)
    monkeypatch.setattr(compose_runner, "ATTENTE_CIBLES_S", 0)
    return runner, appels


def test_les_sondes_de_comportement_prouvent_les_exigences_de_467439(tmp_path, monkeypatch):
    runner, appels = runner_467439(tmp_path, monkeypatch)
    c = runner._sonder_comportement_prometheus(SONDES_RACINE)

    assert set(c) == {"prometheus_pret", "grafana_sante", "cibles", "metriques_api", "regles", "grafana",
                      "endpoint_drift"}
    assert c["prometheus_pret"] is True and c["grafana_sante"] is True
    assert c["cibles"] == {"up": 3, "down": 1, "jobs_up": ["bike-api", "node-exporter", "prometheus"],
                           "jobs_down": ["cadvisor"]}
    # L'API est celle dont /metrics expose des métriques applicatives, même
    # si sa racine ne répondait pas encore : pas node-exporter.
    assert c["metriques_api"]["code"] == 200
    assert "api_requests_total" in c["metriques_api"]["noms"]
    assert "node_cpu_seconds_total" not in c["metriques_api"]["noms"]
    assert c["metriques_api"]["types"]["api_request_duration_seconds"] == "histogram"
    assert c["regles"]["chargees"] == 3 and c["regles"]["erreurs"] == 1
    assert c["regles"]["alertes"][0]["for"] == "1m"
    assert c["grafana"] == {"datasources": 1, "datasource_prometheus": True, "dashboards": 2,
                            "requetes_panels": ["rate(api_requests_total[5m])", "evidently_drift_share",
                                                "100 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100"]}
    assert c["endpoint_drift"] == {"chemin": "/trigger-drift", "code": 200, "metrique_modifiee": True}

    # Le rapport Evidently est lent : l'appel de dérive a un long délai.
    derive = [a for a in appels if a[1].endswith("/trigger-drift")]
    assert derive and derive[0][3] >= 60
    # La dérive passe en dernier : les métriques relevées sont celles d'avant.
    assert appels.index(derive[0]) > max(i for i, a in enumerate(appels) if "32782" in a[1] or "32781" in a[1])

    etape = next(s for s in runner.steps if s["title"] == "Sondes de comportement Prometheus")
    assert etape["exit_code"] == 0
    for mot in ("Prometheus prêt", "Grafana", "bike-api", "node-exporter", "cadvisor", "api_requests_total",
                "BikeApiDown", "1 erreur", "source Prometheus", "2 tableau", "/trigger-drift", "jauge"):
        assert mot in etape["output"], mot
    # Jamais de secret dans une étape.
    assert SECRET not in json.dumps(runner.steps)


def test_les_identifiants_grafana_viennent_du_env_de_la_copie(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch)
    assert runner._identifiants_grafana() == f"admin:{SECRET}"


def test_une_cible_encore_inconnue_est_resondee(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch)
    sonder_base = compose_runner.sonder
    vus = {"n": 0}
    inconnue = {"status": "success", "data": {"activeTargets": [
        {"labels": {"job": "bike-api"}, "health": "unknown"}, {"labels": {"job": "prometheus"}, "health": "up"}]}}

    def sonder(url, **kw):
        if url.endswith("/api/v1/targets"):
            vus["n"] += 1
            corps = inconnue if vus["n"] == 1 else CIBLES
            return {"code": 200, "extrait": json.dumps(corps)}
        return sonder_base(url, **kw)
    monkeypatch.setattr(compose_runner, "sonder", sonder)
    monkeypatch.setattr(compose_runner, "ATTENTE_CIBLES_S", 1)
    monkeypatch.setattr(compose_runner, "PAS_ATTENTE_S", 0)
    c = runner._sonder_comportement_prometheus(SONDES_RACINE)
    assert vus["n"] >= 2 and c["cibles"]["up"] == 3


def test_rien_de_joignable_tout_vaut_null_sans_faire_echouer(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch)
    monkeypatch.setattr(compose_runner, "sonder",
                        lambda url, **kw: {"code": "non reçu", "erreur": "refused", "extrait": "refused"})
    c = runner._sonder_comportement_prometheus(SONDES_RACINE)
    assert c == {"prometheus_pret": None, "grafana_sante": None, "cibles": None, "metriques_api": None,
                 "regles": None, "grafana": None, "endpoint_drift": None}
    etape = next(s for s in runner.steps if s["title"] == "Sondes de comportement Prometheus")
    assert etape["exit_code"] == 0 and "non mesuré" in etape["output"]


def test_un_json_tronque_ou_illisible_vaut_null_jamais_zero(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch)
    sonder_base = compose_runner.sonder

    def sonder(url, **kw):
        if url.endswith(("/api/v1/rules", "/api/v1/targets")):
            return {"code": 200, "extrait": '{"status": "success", "data": {"gro'}
        return sonder_base(url, **kw)
    monkeypatch.setattr(compose_runner, "sonder", sonder)
    c = runner._sonder_comportement_prometheus(SONDES_RACINE)
    assert c["regles"] is None and c["cibles"] is None
    assert c["prometheus_pret"] is True


def test_un_prometheus_qui_repond_mal_n_est_pas_pret(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch)
    sonder_base = compose_runner.sonder

    def sonder(url, **kw):
        if url.endswith("/-/ready"):
            return {"code": 503, "extrait": "Service Unavailable"}
        return sonder_base(url, **kw)
    monkeypatch.setattr(compose_runner, "sonder", sonder)
    assert runner._sonder_comportement_prometheus(SONDES_RACINE)["prometheus_pret"] is False


def test_un_endpoint_de_derive_en_erreur_ne_conclut_rien_sur_la_jauge(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch)
    sonder_base = compose_runner.sonder

    def sonder(url, **kw):
        if url.endswith("/trigger-drift"):
            return {"code": 422, "extrait": '{"detail": "missing body"}'}
        return sonder_base(url, **kw)
    monkeypatch.setattr(compose_runner, "sonder", sonder)
    c = runner._sonder_comportement_prometheus(SONDES_RACINE)
    assert c["endpoint_drift"] == {"chemin": "/trigger-drift", "code": 422, "metrique_modifiee": None}


def _evaluer(runner, monkeypatch):
    monkeypatch.setattr(runner, "_record_service_states", lambda: {"bike-api": ("running", 0)})
    monkeypatch.setattr(runner, "_sonder_ports_publies", lambda: [dict(s) for s in SONDES_RACINE])
    monkeypatch.setattr(runner, "_sonder_chemins_declares", lambda sondes: [])
    monkeypatch.setattr(runner, "_boucles_de_redemarrage", lambda: set())
    monkeypatch.setattr(runner, "_capture_logs", lambda: "")
    return runner._evaluer_services_persistants()


def test_l_evaluation_porte_prometheus_comportement(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch)
    resultat = _evaluer(runner, monkeypatch)
    assert resultat["prometheus_comportement"]["cibles"]["up"] == 3
    assert "nginx_comportement" not in resultat
    json.dumps(resultat["prometheus_comportement"])  # le barème le lit en JSON


def test_une_erreur_interne_rend_tous_les_champs_null(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch)

    def casse(sondes):
        raise RuntimeError("bug du harnais")
    monkeypatch.setattr(runner, "_sonder_comportement_prometheus", casse)
    resultat = _evaluer(runner, monkeypatch)
    assert resultat["prometheus_comportement"] == dict.fromkeys(
        ("prometheus_pret", "grafana_sante", "cibles", "metriques_api", "regles", "grafana", "endpoint_drift"))
    etape = next(s for s in resultat["steps"] if s["title"] == "Sondes de comportement Prometheus")
    assert etape["exit_code"] == 0


def test_examen_nginx_pas_de_cle_prometheus_comportement(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch, nginx=True)
    monkeypatch.setattr(runner, "_sonder_comportement_nginx", lambda sondes: {})
    assert "prometheus_comportement" not in _evaluer(runner, monkeypatch)


def test_sans_prometheus_yml_pas_de_cle_prometheus_comportement(tmp_path, monkeypatch):
    runner, _ = runner_467439(tmp_path, monkeypatch, prometheus=False)
    assert "prometheus_comportement" not in _evaluer(runner, monkeypatch)
