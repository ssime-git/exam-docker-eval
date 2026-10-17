"""BentoMLRunner investigue les étapes en échec, conteneur encore debout."""
import logging
import sys
import types

from docker_eval.bentoml_runner import BentoMLRunner


def _runner(steps):
    r = object.__new__(BentoMLRunner)
    r.steps = list(steps)
    r.eval_dir = "/tmp"
    r.container_name = "bentoml_eval_x"
    r.logger = logging.getLogger("t")
    r.record_step = lambda titre, **kw: r.steps.append({"title": titre, **kw})
    return r


def _faux_investigator(monkeypatch, dispo, appels):
    class Faux:
        def __init__(self, runner, eval_dir, services):
            appels.append(services)

        def disponible(self):
            return dispo

        def investiguer(self, echecs):
            appels.append(len(echecs))

    mod = types.ModuleType("docker_eval.investigator")
    mod.Investigator = Faux
    monkeypatch.setitem(sys.modules, "docker_eval.investigator", mod)


def test_sans_echec_aucune_investigation(monkeypatch):
    appels = []
    _faux_investigator(monkeypatch, True, appels)
    r = _runner([{"title": "ok", "exit_code": 0}])
    r._investiguer_echecs()
    assert appels == []


def test_echec_lance_l_investigation_sur_le_conteneur(monkeypatch):
    appels = []
    _faux_investigator(monkeypatch, True, appels)
    r = _runner([{"title": "ko", "exit_code": 1}])
    r._investiguer_echecs()
    assert appels == [["bentoml_eval_x"], 1]


def test_sans_gateway_trace_non_configuree(monkeypatch):
    appels = []
    _faux_investigator(monkeypatch, False, appels)
    r = _runner([{"title": "ko", "exit_code": 1}])
    r._investiguer_echecs()
    assert r.steps[-1]["title"] == "Investigation non configurée"
    assert r.steps[-1]["exit_code"] == 0
