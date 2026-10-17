"""Construction depuis les sources : la copie reste intacte, et le dit.

Le builder lançait prepare_data, train_model et `bentoml build` sur le
rendu monté, sans PYTHONDONTWRITEBYTECODE : `src/__pycache__` atterrissait
dans la copie et lui était reproché ensuite. Et rien ne distinguait une
image construite par nous d'une image livrée (`image_loaded`).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner


class _R:
    def __init__(self, code=0, sortie=""):
        self.returncode, self.stdout, self.stderr = code, sortie, ""


def copie_source(tmp_path):
    racine = tmp_path / "rendu"
    (racine / "src").mkdir(parents=True)
    (racine / "src" / "service.py").write_text("import bentoml\n")
    (racine / "src" / "prepare_data.py").write_text("print('ok')\n")
    (racine / "bentofile.yaml").write_text("service: 'src.service:Svc'\n")
    return racine


def faux_docker(commandes):
    def run(commande, **kwargs):
        commandes.append(list(commande))
        if commande[:2] == ["docker", "run"]:
            # Ce que fait le vrai conteneur dans le dossier monté.
            monte = commande[commande.index("-v") + 1].rsplit(":", 1)[0]
            os.makedirs(os.path.join(monte, "src", "__pycache__"), exist_ok=True)
            open(os.path.join(monte, "src", "__pycache__", "service.cpython-311.pyc"), "w").close()
            open(os.path.join(monte, "service.bento"), "w").close()
            return _R(0, "exporte: svc:abc")
        return _R(0, "")
    return run


def arborescence(racine):
    return sorted(os.path.relpath(os.path.join(d, f), racine)
                  for d, _, fichiers in os.walk(racine) for f in fichiers)


def test_construction_sur_une_copie_sans_bytecode(tmp_path, monkeypatch):
    racine = copie_source(tmp_path)
    avant = arborescence(racine)
    runner = BentoMLRunner("466000", str(racine), 60, logging.getLogger("t"))
    commandes = []
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_docker(commandes))
    resultat = runner._build_bento_from_source(str(racine))
    assert resultat["success"] is True
    run = next(c for c in commandes if c[:2] == ["docker", "run"])
    assert "PYTHONDONTWRITEBYTECODE=1" in [run[i + 1] for i, a in enumerate(run) if a == "-e"]
    assert run[run.index("-v") + 1].rsplit(":", 1)[0] != str(racine)
    assert os.path.exists(resultat["bento"])
    runner._ranger_construction()
    # La copie de l'apprenant n'a pas bougé : ni __pycache__ ni .bento.
    assert arborescence(racine) == avant


def test_image_construite_par_le_correcteur_exposee(tmp_path, monkeypatch):
    racine = copie_source(tmp_path)
    runner = BentoMLRunner("466000", str(racine), 60, logging.getLogger("t"))
    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_docker([]))
    runner._find_bento_file = lambda: None
    runner._find_bentoml_image = lambda: None
    runner._auto_containerize_bento = lambda bento_file_path=None: {
        "success": True, "image_name": "service_eval:466000"}
    runner._service_port_from_image = lambda: 3000
    runner._env_required_by_image = lambda: {}
    runner._platform_for_image = lambda: None
    runner._collect_diagnostics = lambda: {}
    runner._capture_container_logs = lambda: ""
    runner.cleanup = lambda force=False: None

    class Conteneur:
        def __init__(self, *a, **kw):
            pass

        def __getattr__(self, nom):
            return lambda *a, **kw: self

        def start(self):
            raise RuntimeError("arrêt du test après la construction")

    monkeypatch.setattr(bentoml_runner, "DockerContainer", Conteneur)
    resultat = runner.run_evaluation()
    assert resultat["image_construite_par_le_correcteur"] is True
    assert not os.path.exists(os.path.join(racine, "service.bento"))
    assert not os.path.exists(os.path.join(racine, "src", "__pycache__"))
