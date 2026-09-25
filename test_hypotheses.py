"""Debogage dirige par hypothese : le LLM concoit l'experience, le harnais juge
(scriptorium #336)."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import investigator as mod

VARIABLES = ("PI_CORRECTOR_VERIFY_BASE_URL", "PI_CORRECTOR_VERIFY_API_KEY",
             "PI_CORRECTOR_VERIFY_MODEL", "PI_CORRECTOR_VERIFY_THRESHOLD",
             "PI_CORRECTOR_VERIFY_MAX_ITERATIONS", "PI_CORRECTOR_INVESTIGATE_MAX_ACTIONS",
             "PI_CORRECTOR_INVESTIGATE_TIMEOUT_SECONDS")

ECHEC_461451 = {"title": "Sonde http://127.0.0.1:8000/predict",
                "output": "non reçu : [Errno 104] Connection reset by peer", "exit_code": 1}
ECHEC_457405 = {"title": "Sonde http://127.0.0.1:8080/predict",
                "output": "code 401\n{\"detail\":\"Not authenticated\"}", "exit_code": 1}


@pytest.fixture(autouse=True)
def sans_env(monkeypatch):
    for nom in VARIABLES:
        monkeypatch.delenv(nom, raising=False)


class RunnerFactice:
    def __init__(self):
        self.steps = []

    def record_step(self, title, command="", output="", exit_code=0, duration=None, **champs):
        self.steps.append({"title": title, "command": command, "output": output,
                           "exit_code": exit_code, "duration": duration, **champs})

    def titres(self):
        return [s["title"] for s in self.steps]


class ScoreurFactice:
    def __init__(self, *soutenus):
        self.soutenus = list(soutenus)
        self.appels = []

    def __call__(self, preuves, affirmation):
        self.appels.append((preuves, affirmation))
        p = self.soutenus.pop(0)
        return {"soutenu": p, "non_soutenu": 1 - p}


def enqueteur(tmp_path, monkeypatch, reponses, scoreur=None, services=("s1",)):
    runner = RunnerFactice()
    inv = mod.Investigator(runner, str(tmp_path), services=list(services), scoreur=scoreur)
    inv.base_url, inv.api_key = "https://gw.example", "clef"
    flux = iter(reponses)
    messages_vus = []

    def llm(messages):
        messages_vus.append([dict(m) for m in messages])
        return next(flux)

    monkeypatch.setattr(inv, "_appeler_llm", llm)
    return inv, runner, messages_vus


def hypothese(id_, enonce, test, si_vraie, si_fausse, faute="apprenant", **extra):
    h = {"action": "hypothese", "id": id_, "enonce": enonce, "faute": faute,
         "test": test, "si_vraie": si_vraie, "si_fausse": si_fausse}
    h.update(extra)
    return h


def bloc_json(sortie):
    """Le contrat des consommateurs : le dernier bloc ```json de l'etape verdict."""
    debut = sortie.rindex("```json\n") + len("```json\n")
    fin = sortie.index("\n```", debut)
    return json.loads(sortie[debut:fin])


class Processus:
    """subprocess.run factice : rend (rc, stdout) selon la commande."""

    def __init__(self, regles):
        self.regles = regles
        self.appels = []

    def __call__(self, argv, **kwargs):
        self.appels.append(argv)
        for motif, (rc, sortie) in self.regles:
            if motif in " ".join(argv):
                return subprocess.CompletedProcess(argv, rc, stdout=sortie, stderr="")
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr="inconnu")


# --- hypothese etablie / refutee / non tranchee ------------------------------

def test_hypothese_dns_etablie_sans_dependance_detectee(tmp_path, monkeypatch):
    """Mecanique d'une hypothese etablie par `dns`. Le runner factice n'a
    detecte aucune dependance d'environnement : la faute proposee reste. Le
    vrai 461451 (dns: 172.31.0.2, resolveur du VPC AWS de la correction) est
    rejoue dans test_dependances_environnement.py : faute environnement."""
    processus = Processus([
        ("getent", (2, "--- /etc/resolv.conf\nnameserver 172.31.0.2\n")),
    ])
    monkeypatch.setattr(mod.subprocess, "run", processus)
    scoreur = ScoreurFactice()
    h1 = hypothese("H1", "le DNS imposé (172.31.0.2) ne résout pas l'hôte du dataset",
                   {"action": "dns", "service": "bike-api", "nom": "archive.ics.uci.edu"},
                   {"exit_code": 2, "contient": "172.31.0.2"}, {"exit_code": 0},
                   revelable="le conteneur ne résout pas l'hôte du dataset")
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h1)], scoreur,
                               services=["bike-api"])
    inv.investiguer([ECHEC_461451])

    assert runner.titres() == [
        "Hypothèse H1 — le DNS imposé (172.31.0.2) ne résout pas l'hôte du dataset",
        "Investigation — vérification du verdict",
        "Investigation — verdict"]
    etape = runner.steps[0]
    assert "getent hosts" in etape["command"] and "archive.ics.uci.edu" in etape["command"]
    assert "si vraie" in etape["output"] and "si fausse" in etape["output"]
    assert "nameserver 172.31.0.2" in etape["output"]
    assert "verdict : établie" in etape["output"]
    assert etape["duration"] is not None
    # le nom passe en argument positionnel, jamais concatene au script
    argv = processus.appels[0]
    assert argv[:3] == ["docker", "exec", "bike-api"] and argv[-1] == "archive.ics.uci.edu"
    assert scoreur.appels == []
    assert "scoreur non sollicité" in runner.steps[1]["output"]
    verdict = runner.steps[-1]["output"]
    assert "cause : le DNS imposé (172.31.0.2) ne résout pas l'hôte du dataset" in verdict
    assert "faute : apprenant" in verdict
    bloc = bloc_json(verdict)
    assert bloc["version"] == 1 and bloc["statut"] == "cause_etablie"
    assert bloc["hypothese_etablie"] == "H1" and bloc["faute"] == "apprenant"
    assert bloc["hypotheses_restantes"] == []


def test_461451_logs_dataset_injoignable_ne_contient_pas_sur_logs_complets(tmp_path, monkeypatch):
    """La ligne du dataset arrive tot : la troncature d'affichage ne doit pas
    faire passer « ne_contient_pas » pour vrai."""
    logs = "Unable to fetch Bike Sharing dataset\n" + "x" * (mod.SORTIE_MAX * 2)
    monkeypatch.setattr(mod.subprocess, "run", Processus([("docker logs", (0, logs))]))
    h = hypothese("H2", "le dataset est injoignable au démarrage",
                  {"action": "logs", "service": "bike-api", "lignes": 200},
                  {"contient": "Unable to fetch Bike Sharing dataset"},
                  {"ne_contient_pas": "Unable to fetch Bike Sharing dataset"})
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h)], ScoreurFactice(),
                               services=["bike-api"])
    inv.investiguer([ECHEC_461451])

    assert "verdict : établie" in runner.steps[0]["output"]
    assert len(runner.steps[0]["output"]) < mod.SORTIE_MAX + 1500
    # la ligne qui prouve reste citable, meme hors de l'extrait affiche
    assert "lignes correspondantes :\nUnable to fetch Bike Sharing dataset" in runner.steps[0]["output"]
    assert bloc_json(runner.steps[-1]["output"])["statut"] == "cause_etablie"


def test_457405_refutee_puis_autre_etablie(tmp_path, monkeypatch):
    """401 sans identifiants ; avec ceux du README, 405 : « identifiants
    absents » est refutee, « la sonde n'envoie pas ceux du README » etablie."""
    vus = []

    def sonde(self, url, identifiants=""):
        vus.append(identifiants)
        return mod.Observation("code 405\nMethod Not Allowed", code=405)

    monkeypatch.setattr(mod.Investigator, "_sonde", sonde)
    test = {"action": "sonde", "url": "http://127.0.0.1:8080/predict", "identifiants": "admin:4dm1N"}
    h1 = hypothese("H1", "la copie ne fournit aucun identifiant valide", test,
                   {"code": 401}, {"code": [200, 405]})
    h2 = hypothese("H2", "la sonde du harnais n'envoyait pas les identifiants du README", test,
                   {"code": [200, 405]}, {"code": 401}, faute="harnais")
    scoreur = ScoreurFactice()
    inv, runner, messages = enqueteur(tmp_path, monkeypatch,
                                      [json.dumps(h1), json.dumps(h2)], scoreur)
    inv.investiguer([ECHEC_457405])

    assert vus == ["admin:4dm1N", "admin:4dm1N"]
    assert "verdict : réfutée" in runner.steps[0]["output"]
    assert "verdict : établie" in runner.steps[1]["output"]
    # le LLM apprend la refutation du harnais, pas l'inverse
    assert "H1 : réfutée" in messages[1][-1]["content"]
    assert scoreur.appels == []
    verdict = runner.steps[-1]["output"]
    assert "faute : harnais" in verdict
    bloc = bloc_json(verdict)
    assert bloc["hypothese_etablie"] == "H2"
    assert [h["id"] for h in bloc["hypotheses_refutees"]] == ["H1"]
    assert bloc["hypotheses_refutees"][0]["experiences"][0]["mesures"]["code"] == 405


def test_plusieurs_hypotheses_dans_une_reponse(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.Investigator, "_sonde",
                        lambda self, url, identifiants="": mod.Observation("code 401", code=401))
    test = {"action": "sonde", "url": "http://127.0.0.1:8080/predict"}
    liste = [hypothese("H1", "route protégée", test, {"code": 500}, {"code": 200}),
             hypothese("H2", "auth exigée", test, {"code": 401}, {"code": 200})]
    inv, runner, messages = enqueteur(tmp_path, monkeypatch, [json.dumps(liste)], ScoreurFactice())
    inv.investiguer([ECHEC_457405])

    assert runner.titres()[:2] == ["Hypothèse H1 — route protégée", "Hypothèse H2 — auth exigée"]
    assert "cause : auth exigée" in runner.steps[-1]["output"]


def test_non_tranchee_cause_non_etablie_avec_hypotheses_restantes(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.Investigator, "_sonde",
                        lambda self, url, identifiants="": mod.Observation("code 500\nboom", code=500))
    h = hypothese("H1", "auth manquante", {"action": "sonde", "url": "http://127.0.0.1:8080/predict"},
                  {"code": 401}, {"code": 200})
    abandon = ('{"action":"verdict","cause":"non établie","faute":"indetermine",'
               '"revelable":"regarde la ligne 12 de service.py","non_revelable":""}')
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h), abandon], ScoreurFactice())
    inv.investiguer([ECHEC_457405])

    assert "verdict : non tranchée" in runner.steps[0]["output"]
    verdict = runner.steps[-1]["output"]
    assert "cause : non établie" in verdict and "faute : indetermine" in verdict
    assert "ligne 12" not in verdict  # le revelable se reduit au symptome
    assert "symptôme observé" in verdict
    assert "H1 — auth manquante" in verdict  # lisible
    bloc = bloc_json(verdict)
    assert bloc["statut"] == "cause_non_etablie" and bloc["cause"] is None
    assert bloc["faute"] == "indetermine"
    restante = bloc["hypotheses_restantes"][0]
    assert restante["id"] == "H1" and restante["resultat"] == "non_tranchee"
    assert restante["experiences"][0]["mesures"]["code"] == 500


def test_budget_epuise_au_milieu_d_une_liste(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CORRECTOR_INVESTIGATE_MAX_ACTIONS", "2")
    monkeypatch.setattr(mod.Investigator, "_sonde",
                        lambda self, url, identifiants="": mod.Observation("code 500", code=500))
    test = {"action": "sonde", "url": "http://127.0.0.1:8080/"}
    liste = [hypothese(f"H{i}", f"piste {i}", test, {"code": 401}, {"code": 200}) for i in (1, 2, 3)]
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(liste)], ScoreurFactice())
    assert inv.actions_max == 2
    inv.investiguer([ECHEC_457405])

    assert [t for t in runner.titres() if t.startswith("Hypothèse")] == \
        ["Hypothèse H1 — piste 1", "Hypothèse H2 — piste 2"]
    verdict = runner.steps[-1]
    assert verdict["title"] == "Investigation — verdict"
    assert "budget d'actions épuisé" in verdict["output"]
    assert "cause : non établie" in verdict["output"]
    assert [h["id"] for h in bloc_json(verdict["output"])["hypotheses_restantes"]] == ["H1", "H2"]


def test_timeout_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CORRECTOR_INVESTIGATE_TIMEOUT_SECONDS", "30")
    inv = mod.Investigator(RunnerFactice(), str(tmp_path), services=[])
    assert inv.timeout_total == 30
    assert inv.actions_max == mod.ACTIONS_MAX


# --- hypotheses rejetees ------------------------------------------------------

@pytest.mark.parametrize("si_vraie,si_fausse", [
    ({"code": 401}, {"code": [401, 200]}),                    # codes non disjoints
    ({"contient": "error"}, {"contient": "timeout"}),         # deux sous-chaines compatibles
    ({"contient": "err"}, {"ne_contient_pas": "error"}),      # "err" present n'exclut pas "error" absent
    ({}, {"code": 200}),                                      # prediction vide : toujours vraie
])
def test_predictions_non_disjointes_rejetees(tmp_path, monkeypatch, si_vraie, si_fausse):
    appels = []
    monkeypatch.setattr(mod.Investigator, "_sonde",
                        lambda self, url, identifiants="": appels.append(url) or mod.Observation("code 401", code=401))
    h = hypothese("H1", "piste", {"action": "sonde", "url": "http://127.0.0.1:8080/"},
                  si_vraie, si_fausse)
    inv, runner, messages = enqueteur(tmp_path, monkeypatch, [json.dumps(h), "pas du json"],
                                      ScoreurFactice())
    inv.investiguer([ECHEC_457405])

    assert appels == []  # jamais executee
    assert "rejetée" in runner.steps[0]["output"]
    assert "disjointes" in runner.steps[0]["output"]
    assert "rejetée" in messages[1][-1]["content"]


def test_predictions_disjointes_par_inclusion_acceptees(tmp_path, monkeypatch):
    """contient « Connection refused » / ne_contient_pas « refused » : exclusives."""
    monkeypatch.setattr(mod.Investigator, "_sonde",
                        lambda self, url, identifiants="": mod.Observation("non reçu : Connection refused"))
    h = hypothese("H1", "rien n'écoute", {"action": "sonde", "url": "http://127.0.0.1:8080/"},
                  {"contient": "Connection refused"}, {"ne_contient_pas": "refused"})
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h)], ScoreurFactice())
    inv.investiguer([ECHEC_457405])
    assert "verdict : établie" in runner.steps[0]["output"]


def test_faute_manquante_rejetee(tmp_path, monkeypatch):
    h = hypothese("H1", "piste", {"action": "sonde", "url": "http://127.0.0.1:8080/"},
                  {"code": 401}, {"code": 200})
    del h["faute"]
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h), "pas du json"], ScoreurFactice())
    inv.investiguer([ECHEC_457405])
    assert "rejetée" in runner.steps[0]["output"] and "faute" in runner.steps[0]["output"]


def test_test_qui_est_un_verdict_rejete(tmp_path, monkeypatch):
    h = hypothese("H1", "piste", {"action": "verdict", "cause": "x"}, {"code": 401}, {"code": 200})
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h), "pas du json"], ScoreurFactice())
    inv.investiguer([ECHEC_457405])
    assert "rejetée" in runner.steps[0]["output"]


def test_refus_du_harnais_jamais_une_observation(tmp_path, monkeypatch):
    """Un conteneur hors perimetre rend « refusé : … » : ne_contient_pas serait
    vrai sur ce texte, l'hypothese ne peut pas s'etablir dessus."""
    h = hypothese("H1", "le dataset est bien arrivé",
                  {"action": "logs", "service": "autre-stack", "lignes": 50},
                  {"ne_contient_pas": "Unable to fetch"}, {"contient": "Unable to fetch"})
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h), "pas du json"], ScoreurFactice())
    inv.investiguer([ECHEC_461451])
    assert "hors du périmètre" in runner.steps[0]["output"]
    assert "verdict : non tranchée" in runner.steps[0]["output"]


def test_metadonnee_absente_non_tranchee(tmp_path, monkeypatch):
    """Une action qui rend une str nue n'a pas de code : jamais une correspondance."""
    monkeypatch.setattr(mod.Investigator, "_sonde", lambda self, url, identifiants="": "code 401")
    h = hypothese("H1", "auth", {"action": "sonde", "url": "http://127.0.0.1:8080/"},
                  {"code": 401}, {"code": 200})
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h), "pas du json"], ScoreurFactice())
    inv.investiguer([ECHEC_457405])
    assert "verdict : non tranchée" in runner.steps[0]["output"]


# --- compatibilite : verdict direct -------------------------------------------

def test_verdict_direct_apres_hypotheses_passe_par_le_scoreur(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.Investigator, "_sonde",
                        lambda self, url, identifiants="": mod.Observation("code 500\nTraceback numpy", code=500))
    h = hypothese("H1", "auth", {"action": "sonde", "url": "http://127.0.0.1:8080/"},
                  {"code": 401}, {"code": 200})
    direct = ('{"action":"verdict","cause":"numpy casse l\'import","faute":"apprenant",'
              '"revelable":"erreur numpy","non_revelable":"la version"}')
    scoreur = ScoreurFactice(0.9)
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h), direct], scoreur)
    inv.investiguer([ECHEC_457405])

    assert len(scoreur.appels) == 1
    assert "Traceback numpy" in scoreur.appels[0][0]  # les tests d'hypothese sont des preuves
    verdict = runner.steps[-1]["output"]
    assert "faute : apprenant" in verdict
    bloc = bloc_json(verdict)
    assert bloc["statut"] == "cause_scoree"
    assert bloc["hypotheses_restantes"][0]["id"] == "H1"


def test_verdict_direct_seul_porte_aussi_le_bloc(tmp_path, monkeypatch):
    direct = ('{"action":"verdict","cause":"port 80 jamais lié","faute":"apprenant",'
              '"revelable":"le port 80","non_revelable":""}')
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [direct], ScoreurFactice(0.9))
    inv.investiguer([ECHEC_461451])
    bloc = bloc_json(runner.steps[-1]["output"])
    assert bloc["statut"] == "cause_scoree" and bloc["hypotheses_restantes"] == []


# --- nouvelles actions de lecture ---------------------------------------------

def test_action_dns_nom_invalide_refuse(tmp_path, monkeypatch):
    processus = Processus([])
    monkeypatch.setattr(mod.subprocess, "run", processus)
    inv = mod.Investigator(RunnerFactice(), str(tmp_path), services=["s1"])
    assert inv._dns("s1", "x; rm -rf /").startswith("refusé")
    assert inv._dns("autre", "example.com").startswith("refusé")
    assert processus.appels == []


def test_action_ports_ss(tmp_path, monkeypatch):
    processus = Processus([("ss -ltn", (0, "State Recv-Q Send-Q Local Address:Port\nLISTEN 0 128 0.0.0.0:8000\n"))])
    monkeypatch.setattr(mod.subprocess, "run", processus)
    inv = mod.Investigator(RunnerFactice(), str(tmp_path), services=["s1"])
    sortie = inv._ports("s1")
    assert "0.0.0.0:8000" in sortie and sortie.exit_code == 0
    assert processus.appels[0][:3] == ["docker", "exec", "s1"]


def test_action_ports_repli_proc_net_tcp(tmp_path, monkeypatch):
    proc = ("__PROC_NET_TCP__\n"
            "  sl  local_address rem_address   st tx_queue rx_queue\n"
            "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000\n"
            "   1: 00000000:1F40 00000000:0000 0A 00000000:00000000\n"
            "   2: 0100007F:1F90 0100007F:D431 01 00000000:00000000\n")
    monkeypatch.setattr(mod.subprocess, "run", Processus([("ss -ltn", (0, proc))]))
    inv = mod.Investigator(RunnerFactice(), str(tmp_path), services=["s1"])
    sortie = inv._ports("s1")
    assert "LISTEN 127.0.0.1:8080" in sortie
    assert "LISTEN 0.0.0.0:8000" in sortie
    assert "D431" not in sortie  # connexion etablie, pas une ecoute


def test_action_env_masque_les_secrets(tmp_path, monkeypatch):
    env = json.dumps(["PATH=/usr/bin", "JWT_SECRET_KEY=s3cr3t-jwt", "api_key=cle-minuscule",
                      "POSTGRES_PASSWORD=motdepasse", "HF_TOKEN=hf_abc", "DATASET_URL=http://x"])
    monkeypatch.setattr(mod.subprocess, "run", Processus([("docker inspect", (0, env))]))
    h = hypothese("H1", "l'URL du dataset est fournie",
                  {"action": "env", "service": "s1"},
                  {"contient": "DATASET_URL="}, {"ne_contient_pas": "DATASET_URL="})
    inv, runner, messages = enqueteur(tmp_path, monkeypatch, [json.dumps(h)], ScoreurFactice())
    inv.investiguer([ECHEC_461451])

    etape, verdict = runner.steps[0]["output"], runner.steps[-1]["output"]
    for secret in ("s3cr3t-jwt", "cle-minuscule", "motdepasse", "hf_abc"):
        assert secret not in etape
        assert secret not in verdict
        assert all(secret not in m["content"] for echange in messages for m in echange)
    assert "PATH=/usr/bin" in etape and "JWT_SECRET_KEY=***" in etape
    assert "verdict : établie" in etape


def test_exec_liste_noire_dans_un_test_d_hypothese(tmp_path, monkeypatch):
    processus = Processus([])
    monkeypatch.setattr(mod.subprocess, "run", processus)
    h = hypothese("H1", "piste", {"action": "exec", "service": "s1", "commande": "cat /etc/hosts > /tmp/x"},
                  {"exit_code": 0}, {"exit_code": 1})
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h), "pas du json"], ScoreurFactice())
    inv.investiguer([ECHEC_461451])
    assert processus.appels == []
    assert "verdict : non tranchée" in runner.steps[0]["output"]


def test_actions_de_lecture_hors_hypothese(tmp_path, monkeypatch):
    processus = Processus([("getent", (0, "93.184.216.34 example.com\n--- /etc/resolv.conf\nnameserver 127.0.0.11\n"))])
    monkeypatch.setattr(mod.subprocess, "run", processus)
    reponses = ['{"action":"dns","service":"s1","nom":"example.com"}',
                '{"action":"verdict","cause":"non établie","faute":"indetermine","revelable":"","non_revelable":""}']
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, reponses, ScoreurFactice())
    inv.investiguer([ECHEC_461451])
    assert runner.titres()[0] == "Investigation 1 : dns"
    assert "93.184.216.34" in runner.steps[0]["output"]


def test_prompt_pousse_vers_les_hypotheses(tmp_path, monkeypatch):
    inv, runner, messages = enqueteur(tmp_path, monkeypatch, ["pas du json"], ScoreurFactice())
    inv.investiguer([ECHEC_461451])
    systeme = messages[0][0]["content"]
    for mot in ('"action":"hypothese"', '"si_vraie"', '"si_fausse"', '"action":"dns"',
                '"revelable"', '"non_revelable"',
                '"action":"ports"', '"action":"env"'):
        assert mot in systeme


# --- contrat lu par scriptorium#340 ------------------------------------------

def test_contrat_des_etapes_pour_la_revue(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.Investigator, "_sonde",
                        lambda self, url, identifiants="": mod.Observation("code 500\nboom", code=500))
    test = {"action": "sonde", "url": "http://127.0.0.1:8080/predict"}
    h1 = hypothese("H1", "auth manquante", test, {"code": 401}, {"code": 200})
    h2 = hypothese("H2", "piste floue", test, {"contient": "a"}, {"contient": "b"})
    abandon = '{"action":"verdict","cause":"non établie","faute":"indetermine","revelable":"","non_revelable":""}'
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps([h1, h2]), abandon], ScoreurFactice())
    inv.investiguer([ECHEC_457405])

    etape = runner.steps[0]
    assert etape["title"] == "Hypothèse H1 — auth manquante"
    assert etape["command"] == "GET http://127.0.0.1:8080/predict"
    assert "\nrésultat : code=500 exit_code=-" in etape["output"]
    assert etape["output"].endswith("\nverdict : non tranchée")
    assert etape["verdict"] == "non tranchée"
    rejetee = runner.steps[1]
    assert rejetee["title"] == "Hypothèse H2 — piste floue" and rejetee["verdict"] == "non tranchée"

    verdict = runner.steps[-1]
    assert verdict["title"] == "Investigation — verdict"
    assert "cause non établie" in verdict["output"]
    restantes = verdict["hypotheses_restantes"]
    assert restantes == bloc_json(verdict["output"])["hypotheses_restantes"]
    assert [(h["id"], h["enonce"]) for h in restantes] == [("H1", "auth manquante"), ("H2", "piste floue")]
    experience = restantes[0]["experiences"][0]
    assert set(experience) >= {"test", "prediction", "resultat", "verdict"}
    assert experience["prediction"] == {"si_vraie": {"code": 401}, "si_fausse": {"code": 200}}
    assert experience["resultat"] == "code=500 exit_code=-"
    assert restantes[1]["experiences"] == []  # piste non testee


# --- une absence ne se prouve pas sur une lecture tronquee --------------------

def _hyp_absence(test):
    return hypothese("H1", "le dataset est arrivé", test,
                     {"ne_contient_pas": "Unable to fetch"}, {"contient": "Unable to fetch"})


def test_logs_au_plafond_de_tail_absence_non_tranchee(tmp_path, monkeypatch):
    logs = "".join(f"ligne {i}\n" for i in range(50))
    monkeypatch.setattr(mod.subprocess, "run", Processus([("docker logs", (0, logs))]))
    h = _hyp_absence({"action": "logs", "service": "s1", "lignes": 50})
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h), "pas du json"], ScoreurFactice())
    inv.investiguer([ECHEC_461451])
    assert "lecture tronquée" in runner.steps[0]["output"]
    assert runner.steps[0]["verdict"] == "non tranchée"


def test_logs_sous_le_plafond_absence_prouvee(tmp_path, monkeypatch):
    logs = "".join(f"ligne {i}\n" for i in range(10))
    monkeypatch.setattr(mod.subprocess, "run", Processus([("docker logs", (0, logs))]))
    h = _hyp_absence({"action": "logs", "service": "s1", "lignes": 50})
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h)], ScoreurFactice())
    inv.investiguer([ECHEC_461451])
    assert runner.steps[0]["verdict"] == "établie"


def test_fichier_long_absence_non_tranchee_presence_prouvee(tmp_path, monkeypatch):
    (tmp_path / "gros.log").write_text("x" * mod.LECTURE_FICHIER_MAX + "Unable to fetch")
    (tmp_path / "debut.log").write_text("Unable to fetch\n" + "x" * mod.LECTURE_FICHIER_MAX)
    inv = mod.Investigator(RunnerFactice(), str(tmp_path), services=[])
    gros = mod._lire(inv._fichier("gros.log"))
    debut = mod._lire(inv._fichier("debut.log"))
    assert gros["tronquee"] and debut["tronquee"]
    si_vraie, si_fausse = {"ne_contient_pas": "Unable to fetch"}, {"contient": "Unable to fetch"}
    assert mod.verdict_mecanique(si_vraie, si_fausse, gros) == "non_tranchee"
    assert mod.verdict_mecanique(si_vraie, si_fausse, debut) == "refutee"


def test_sonde_corps_long_absence_non_tranchee(tmp_path, monkeypatch):
    from docker_eval import sonde_http

    def sonder(url, entetes=None, taille_extrait=300, **kwargs):
        return {"code": 200, "extrait": "a" * taille_extrait}

    monkeypatch.setattr(sonde_http, "sonder", sonder)
    inv = mod.Investigator(RunnerFactice(), str(tmp_path), services=[])
    obs = inv._sonde("http://127.0.0.1:8080/")
    lu = mod._lire(obs)
    assert lu["tronquee"] and len(str(obs)) < 500
    assert mod.verdict_mecanique({"ne_contient_pas": "erreur"}, {"contient": "erreur"}, lu) == "non_tranchee"
    assert mod.verdict_mecanique({"code": 200}, {"code": 500}, lu) == "etablie"


# --- une erreur du CLI docker n'est pas un code de sortie du conteneur --------

@pytest.mark.parametrize("stderr", [
    "Error response from daemon: container 3f2a is not running",
    "Error response from daemon: No such container: s1",
    "OCI runtime exec failed: exec failed: unable to start container process",
])
def test_conteneur_arrete_non_tranchee(tmp_path, monkeypatch, stderr):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(mod.subprocess, "run", run)
    h = hypothese("H1", "le fichier de config manque",
                  {"action": "exec", "service": "s1", "commande": "cat /app/config.yml"},
                  {"exit_code": 1}, {"exit_code": 0})
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [json.dumps(h), "pas du json"], ScoreurFactice())
    inv.investiguer([ECHEC_461451])
    assert runner.steps[0]["verdict"] == "non tranchée"
    assert "le harnais n'a rien observé" in runner.steps[0]["output"]
