"""Deux copies du même examen ne se partagent pas une image.

    uvx --with testcontainers --with docker --with requests --with pyyaml python test_images_par_copie.py

15/10 : 459845 et 458990 livraient toutes deux `admission_prediction_service:latest`.
Corrigées en même temps, le `docker load` de l'une réécrivait le tag de l'autre,
et le ménage de l'une supprimait l'image de l'autre : « Unable to find image »,
7/20 au lieu de 20/20 — ou une copie notée sur l'image de l'autre.
"""
import multiprocessing
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval import base_runner  # noqa: E402
from docker_eval.base_runner import tag_propre_a_la_copie, verrou_images  # noqa: E402

assert tag_propre_a_la_copie("459845", "admission_prediction_service:latest") == "eval_459845/admission_prediction_service:latest"
assert tag_propre_a_la_copie("458990", "admission_prediction_service:latest") != tag_propre_a_la_copie("459845", "admission_prediction_service:latest")
assert tag_propre_a_la_copie("Copie Test", "Registry.io/Org/Svc:V1.2") == "eval_copie_test/registry.io_org_svc:v1.2"
assert tag_propre_a_la_copie("459845", "sha256:aaa111") == "eval_459845/image:latest"
assert tag_propre_a_la_copie("459845", "svc") == "eval_459845/svc:latest"
print("OK : chaque copie a son propre tag")


def tenir(chemin, journal, nom, duree):
    os.environ["EXAM_IMAGES_VERROU"] = chemin
    with verrou_images():
        with open(journal, "a") as j:
            j.write(f"{nom} entre {time.time():.3f}\n")
        time.sleep(duree)
        with open(journal, "a") as j:
            j.write(f"{nom} sort {time.time():.3f}\n")


with tempfile.TemporaryDirectory() as d:
    verrou, journal = os.path.join(d, "images.lock"), os.path.join(d, "journal")
    a = multiprocessing.Process(target=tenir, args=(verrou, journal, "A", 0.5))
    b = multiprocessing.Process(target=tenir, args=(verrou, journal, "B", 0.1))
    a.start(); time.sleep(0.1); b.start(); a.join(); b.join()
    lignes = [l.split()[:2] for l in open(journal)]
    # B attend que A sorte : load + retag ne s'entrelacent jamais
    assert lignes == [["A", "entre"], ["A", "sort"], ["B", "entre"], ["B", "sort"]], lignes
print("OK : deux processus ne chargent pas d'image en même temps")


# Sans archive ni .bento ni source, le repli ne prend que l'image de CETTE copie.
import logging  # noqa: E402
import types  # noqa: E402
from docker_eval.bentoml_runner import BentoMLRunner  # noqa: E402

class FauxClient:
    def __init__(self, tags):
        self.images = types.SimpleNamespace(list=lambda: [types.SimpleNamespace(tags=[t]) for t in tags])

def repli(copie, tags, monkey):
    import docker
    ancien = docker.from_env
    docker.from_env = lambda: FauxClient(tags)
    try:
        return BentoMLRunner(copie, tempfile.mkdtemp(), 60, logging.getLogger("t"))._find_bentoml_image()
    finally:
        docker.from_env = ancien

autres = ["admission_bento_eval:458990", "eval_458990/admission_bento_service:latest"]
assert repli("459845", autres, None) is None, "l'image d'une autre copie n'est jamais prise"
assert repli("459845", autres + ["admission_bento_eval:459845"], None) == "admission_bento_eval:459845"
print("OK : le repli ne prend jamais l'image d'une autre copie")
