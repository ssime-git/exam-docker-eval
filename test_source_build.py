"""Détection d'un rendu livré en source, et trace conservée sur échec.

    uvx --with testcontainers --with docker --with requests --with pyyaml \
        python test_source_build.py
"""
import os, sys, tempfile, pathlib, logging

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval.bentoml_runner import BentoMLRunner

log = logging.getLogger("t")


def ecrire(racine, rel, contenu=""):
    p = pathlib.Path(racine) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(contenu, encoding="utf-8")


# --- reconnaitre un rendu livre en source ------------------------------------
with tempfile.TemporaryDirectory() as d:
    r = BentoMLRunner("copie-test", d, 300, log)
    assert r._find_bento_source() is None, "un rendu vide n'est pas une source"

    ecrire(d, "examen_bentoml/bentofile.yaml", "service: src.service:Svc\n")
    ecrire(d, "examen_bentoml/src/service.py", "x = 1")
    trouve = r._find_bento_source()
    assert trouve and trouve.endswith("examen_bentoml"), trouve
    print("OK : bentofile.yaml imbrique reconnu comme source constructible")

with tempfile.TemporaryDirectory() as d:
    ecrire(d, ".venv/lib/bentofile.yaml", "bruit")
    r = BentoMLRunner("copie-test", d, 300, log)
    assert r._find_bento_source() is None, "un .venv ne doit pas passer pour une source"
    print("OK : le bruit d'environnement est ignore")

# --- la trace survit a un echec ----------------------------------------------
with tempfile.TemporaryDirectory() as d:
    r = BentoMLRunner("copie-test", d, 300, log)
    r.record_step("Une etape avant l'echec", command="ls", output="ok")
    resultat = r.echec("rien de constructible", image_loaded=False)
    assert resultat["success"] is False and resultat["exit_code"] == 2
    assert resultat["image_loaded"] is False
    titres = [s["title"] for s in resultat["steps"]]
    assert titres == ["Une etape avant l'echec", "L'évaluation s'interrompt"], titres
    assert resultat["steps"][-1]["output"] == "rien de constructible"
    print("OK : un echec rend la trace, l'interruption comprise")


# --- le .bento est cherche recursivement -------------------------------------
with tempfile.TemporaryDirectory() as d:
    r = BentoMLRunner("copie-test", d, 300, log)
    assert r._find_bento_file() is None

    # cas reel : le rendu arrive dans un dossier
    ecrire(d, "examen_bentoml/service.bento", "x")
    assert r._find_bento_file().endswith("examen_bentoml/service.bento"), r._find_bento_file()

    # un .bento a la racine prime sur un artefact enfoui
    ecrire(d, "racine.bento", "x")
    assert r._find_bento_file().endswith("racine.bento"), r._find_bento_file()

    # le magasin bentoml interne ne doit pas etre confondu avec un rendu
    ecrire(d, "examen_bentoml/.bentoml_home/bentos/interne.bento", "x")
    assert ".bentoml_home" not in r._find_bento_file()
    print("OK : .bento trouve dans un sous-dossier, sans confondre le magasin interne")
