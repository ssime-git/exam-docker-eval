"""Le verdict de l'investigateur est score contre ses observations (scriptorium #307)."""
import io
import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import investigator as mod

VARIABLES = ("PI_CORRECTOR_VERIFY_BASE_URL", "PI_CORRECTOR_VERIFY_API_KEY",
             "PI_CORRECTOR_VERIFY_MODEL", "PI_CORRECTOR_VERIFY_THRESHOLD",
             "PI_CORRECTOR_VERIFY_MAX_ITERATIONS")

ECHEC = {"title": "Sonde http://127.0.0.1:80/", "output": "non reçu : connection refused\ndetail"}
ACCUSE = ('{"action":"verdict","cause":"port 80 jamais lié","faute":"apprenant",'
          '"revelable":"le port 80 ne répond pas, voir la directive listen",'
          '"non_revelable":"la directive à ajouter"}')
PRUDENT = ('{"action":"verdict","cause":"non établie","faute":"indetermine",'
           '"revelable":"le port 80 ne répond pas","non_revelable":""}')
LOGS = '{"action":"logs","service":"s1","lignes":20}'


@pytest.fixture(autouse=True)
def sans_env(monkeypatch):
    for nom in VARIABLES:
        monkeypatch.delenv(nom, raising=False)


class RunnerFactice:
    def __init__(self):
        self.steps = []

    def record_step(self, title, command="", output="", exit_code=0, duration=0):
        self.steps.append({"title": title, "command": command, "output": output,
                           "exit_code": exit_code})

    def titres(self):
        return [s["title"] for s in self.steps]

    def etape(self, titre):
        return [s for s in self.steps if s["title"] == titre]


class ScoreurFactice:
    """Rend les P(soutenu) dans l'ordre et garde ce qu'on lui a donne."""

    def __init__(self, *soutenus):
        self.soutenus = list(soutenus)
        self.appels = []

    def __call__(self, preuves, affirmation):
        self.appels.append((preuves, affirmation))
        p = self.soutenus.pop(0)
        return {"soutenu": p, "non_soutenu": 1 - p}


def enqueteur(tmp_path, monkeypatch, reponses, scoreur=None):
    runner = RunnerFactice()
    inv = mod.Investigator(runner, str(tmp_path), services=["s1"], scoreur=scoreur)
    inv.base_url, inv.api_key = "https://gw.example", "clef"
    flux = iter(reponses)
    messages_vus = []

    def llm(messages):
        messages_vus.append([dict(m) for m in messages])
        return next(flux)

    monkeypatch.setattr(inv, "_appeler_llm", llm)
    monkeypatch.setattr(inv, "_logs", lambda service, lignes: "bind() to 0.0.0.0:80 failed")
    return inv, runner, messages_vus


def test_verdict_soutenu_accepte(tmp_path, monkeypatch):
    scoreur = ScoreurFactice(0.9)
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [LOGS, ACCUSE], scoreur)
    inv.investiguer([ECHEC])

    assert runner.titres() == ["Investigation 1 : logs",
                               "Investigation — vérification du verdict",
                               "Investigation — verdict"]
    verification = runner.steps[1]
    assert "soutenu" in verification["output"] and "0.90" in verification["output"]
    assert verification["exit_code"] == 0
    verdict = runner.steps[-1]["output"]
    assert "cause : port 80 jamais lié" in verdict and "faute : apprenant" in verdict
    # contexte etroit : la cause comme affirmation, l'observation et l'echec comme preuves
    preuves, affirmation = scoreur.appels[0]
    assert affirmation == "port 80 jamais lié"
    assert "bind() to 0.0.0.0:80 failed" in preuves
    assert "connection refused\ndetail" in preuves


def test_non_soutenu_puis_action_puis_soutenu(tmp_path, monkeypatch):
    scoreur = ScoreurFactice(0.3, 0.8)
    inv, runner, messages = enqueteur(tmp_path, monkeypatch, [ACCUSE, LOGS, ACCUSE], scoreur)
    inv.investiguer([ECHEC])

    assert runner.titres() == ["Investigation — vérification du verdict",
                               "Investigation 1 : logs",
                               "Investigation — vérification du verdict",
                               "Investigation — verdict"]
    assert "P(soutenu)=0.30" in runner.steps[0]["output"]
    # le challenge suit la reponse du LLM, jamais deux messages user d'affilee
    apres_challenge = messages[1]
    assert apres_challenge[-2] == {"role": "assistant", "content": ACCUSE}
    assert apres_challenge[-1]["role"] == "user"
    assert "n'est pas établie par tes observations (P=0.30)" in apres_challenge[-1]["content"]
    assert "faute : apprenant" in runner.steps[-1]["output"]
    assert "bind() to 0.0.0.0:80 failed" in scoreur.appels[1][0]


def test_non_soutenu_jusqu_au_budget_declasse(tmp_path, monkeypatch):
    scoreur = ScoreurFactice(0.2, 0.3, 0.4)
    inv, runner, _ = enqueteur(tmp_path, monkeypatch,
                               [ACCUSE, LOGS, ACCUSE, LOGS, ACCUSE], scoreur)
    inv.investiguer([ECHEC])

    assert len(scoreur.appels) == 3  # 1 + MAX_ITERATIONS
    assert runner.titres()[-1] == "Investigation — verdict"
    assert [t for t in runner.titres() if t.startswith("Investigation ") and ":" in t] == \
        ["Investigation 1 : logs", "Investigation 2 : logs"]
    verdict = runner.steps[-1]["output"]
    assert "cause : hypothèse : port 80 jamais lié" in verdict
    assert "faute : indetermine" in verdict
    assert "directive listen" not in verdict  # revelable reduit au symptome
    assert "Sonde http://127.0.0.1:80/" in verdict
    assert "déclassé" in runner.steps[-2]["output"]


def test_verdict_repete_sans_action_declasse(tmp_path, monkeypatch):
    """#306 : reformuler avec la meme preuve ne compte pas."""
    scoreur = ScoreurFactice(0.3)
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [ACCUSE, ACCUSE], scoreur)
    inv.investiguer([ECHEC])

    assert len(scoreur.appels) == 1
    assert "faute : indetermine" in runner.steps[-1]["output"]
    assert "sans observation nouvelle" in runner.steps[-2]["output"]


def test_challenge_puis_reponse_inexploitable_garde_le_verdict_declasse(tmp_path, monkeypatch):
    scoreur = ScoreurFactice(0.3)
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [ACCUSE, "pas du json"], scoreur)
    inv.investiguer([ECHEC])

    assert "Investigation interrompue" in runner.titres()
    assert runner.titres()[-1] == "Investigation — verdict"
    assert "cause : hypothèse : port 80 jamais lié" in runner.steps[-1]["output"]
    assert "faute : indetermine" in runner.steps[-1]["output"]


def test_actions_epuisees_apres_challenge_declasse(tmp_path, monkeypatch):
    scoreur = ScoreurFactice(0.3)
    reponses = [LOGS] * (mod.ACTIONS_MAX - 1) + [ACCUSE, LOGS, LOGS]
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, reponses, scoreur)
    inv.investiguer([ECHEC])

    actions = [t for t in runner.titres() if t.startswith("Investigation ") and " : " in t]
    assert len(actions) == mod.ACTIONS_MAX
    assert runner.titres()[-1] == "Investigation — verdict"
    assert "faute : indetermine" in runner.steps[-1]["output"]


def test_erreur_du_scoreur_declasse(tmp_path, monkeypatch):
    def scoreur(preuves, affirmation):
        raise ConnectionRefusedError("llama-server absent")

    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [ACCUSE], scoreur)
    inv.investiguer([ECHEC])

    assert "llama-server absent" in runner.steps[-2]["output"]
    assert "faute : indetermine" in runner.steps[-1]["output"]


def test_indetermine_non_score(tmp_path, monkeypatch):
    scoreur = ScoreurFactice()
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [PRUDENT], scoreur)
    inv.investiguer([ECHEC])

    assert scoreur.appels == []
    assert runner.titres() == ["Investigation — vérification du verdict",
                               "Investigation — verdict"]
    assert "non scorée" in runner.steps[0]["output"]
    assert "cause : non établie" in runner.steps[-1]["output"]


def test_verification_non_configuree(tmp_path, monkeypatch):
    inv, runner, _ = enqueteur(tmp_path, monkeypatch, [ACCUSE])
    assert inv.scoreur is None
    inv.investiguer([ECHEC])

    assert runner.titres() == ["Vérification du verdict non configurée",
                               "Investigation — verdict"]
    assert runner.steps[0]["exit_code"] == 0
    assert "PI_CORRECTOR_VERIFY_BASE_URL" in runner.steps[0]["output"]
    assert "faute : apprenant" in runner.steps[-1]["output"]


def test_configuration_par_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CORRECTOR_VERIFY_BASE_URL", "http://127.0.0.1:8089/v1/")
    monkeypatch.setenv("PI_CORRECTOR_VERIFY_THRESHOLD", "0.7")
    monkeypatch.setenv("PI_CORRECTOR_VERIFY_MAX_ITERATIONS", "1")
    inv = mod.Investigator(RunnerFactice(), str(tmp_path), services=[])
    assert inv.scoreur is not None
    assert inv.seuil == 0.7 and inv.iterations_max == 1


def _logprob(token, p):
    return {"token": token, "logprob": math.log(p)}


class ReponseFactice(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("cle", ["", "sk-verif"])
def test_scoreur_http(monkeypatch, cle):
    envoye = {}

    def urlopen(requete, timeout):
        envoye["url"] = requete.full_url
        envoye["corps"] = json.loads(requete.data)
        envoye["entetes"] = dict(requete.header_items())
        tops = [_logprob("A", 0.6), _logprob(" B", 0.2), _logprob("Hello", 0.1),
                _logprob("D", 0.1), _logprob("A", 0.05)]
        return ReponseFactice(json.dumps(
            {"choices": [{"logprobs": {"content": [{"top_logprobs": tops}]}}]}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    distribution = mod.scorer_affirmation("preuve X", "affirmation Y",
                                          base_url="http://127.0.0.1:8089/v1/",
                                          api_key=cle, model="")

    assert envoye["url"] == "http://127.0.0.1:8089/v1/chat/completions"
    corps = envoye["corps"]
    assert corps["max_tokens"] == 1 and corps["temperature"] == 0
    assert corps["logprobs"] is True and corps["top_logprobs"] == 20
    assert corps["chat_template_kwargs"] == {"enable_thinking": False}
    assert "model" not in corps
    contenu = corps["messages"][0]["content"]
    assert contenu.startswith("Preuves d'exécution d'une correction d'examen :\n\npreuve X")
    assert "Affirmation du feedback à vérifier : affirmation Y" in contenu
    assert "A. soutenu : une preuve établit directement l'affirmation" in contenu
    assert contenu.endswith("Réponds par une seule lettre.")
    if cle:
        assert envoye["entetes"]["Authorization"] == "Bearer sk-verif"
    else:
        assert "Authorization" not in envoye["entetes"]
    assert set(distribution) == {"soutenu", "non_soutenu", "inverifiable"}
    assert distribution["soutenu"] == pytest.approx(0.6 / 0.9)
    assert sum(distribution.values()) == pytest.approx(1)


def test_scoreur_http_sans_lettre(monkeypatch):
    def urlopen(requete, timeout):
        tops = [_logprob("Le", 0.9)]
        return ReponseFactice(json.dumps(
            {"choices": [{"logprobs": {"content": [{"top_logprobs": tops}]}}]}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    assert mod.scorer_affirmation("p", "a", base_url="http://x/v1", api_key="", model="m") == {}
