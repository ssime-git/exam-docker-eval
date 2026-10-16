"""Le ménage des images est idempotent : une image déjà absente n'est pas une erreur.

    uvx --with testcontainers --with docker --with requests --with pyyaml python test_suppression_image_idempotente.py

Le ménage s'exécutait deux fois en fin de run ; le 2e passage journalisait
`WARNING - Image X non supprimée : 404 ... No such image`, dernière ligne de la
sortie, que le correcteur citait comme « cause » de l'échec alors que l'image
avait bien été supprimée juste avant.
"""
import logging
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import docker  # noqa: E402
import docker.errors  # noqa: E402
from docker_eval.bentoml_runner import BentoMLRunner  # noqa: E402


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lignes = []

    def emit(self, record):
        self.lignes.append((record.levelname, record.getMessage()))


class FauxImages:
    def __init__(self, presentes, erreurs=None):
        self.presentes = set(presentes)
        self.erreurs = erreurs or {}
        self.appels = []

    def remove(self, image, force=False):
        self.appels.append(image)
        if image in self.erreurs:
            raise self.erreurs[image]
        if image not in self.presentes:
            raise docker.errors.ImageNotFound(f"404 Client Error: Not Found (\"No such image: {image}\")")
        self.presentes.discard(image)


def runner_avec(images_docker, images_copie):
    journal = Capture()
    logger = logging.getLogger(f"t{id(journal)}")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(journal)
    r = BentoMLRunner("copie-test", tempfile.mkdtemp(), 60, logger)
    for i in images_copie:
        r.record_exam_image(i)
    ancien = docker.from_env
    docker.from_env = lambda: types.SimpleNamespace(images=images_docker)
    return r, journal, ancien


def avertissements(journal):
    return [l for l in journal.lignes if l[0] in ("WARNING", "ERROR")]


# --- deux ménages d'affilée : le 2e ne dit rien d'alarmant ------------------
faux = FauxImages({"eval_1/svc:latest"})
r, journal, ancien = runner_avec(faux, ["eval_1/svc:latest"])
try:
    assert r.remove_exam_images() == []
    assert r.remove_exam_images() == [], "2e passage : aucune erreur retournée"
finally:
    docker.from_env = ancien
assert avertissements(journal) == [], avertissements(journal)
assert faux.appels == ["eval_1/svc:latest"], f"l'image déjà supprimée n'est pas redemandée : {faux.appels}"
print("OK : deux ménages d'affilée, aucun avertissement")

# --- image disparue entre-temps (autre processus) : INFO, pas d'erreur -----
faux = FauxImages(set())
r, journal, ancien = runner_avec(faux, ["eval_1/svc:latest"])
try:
    assert r.remove_exam_images() == []
finally:
    docker.from_env = ancien
assert avertissements(journal) == [], avertissements(journal)
assert any("déjà absente" in m for _, m in journal.lignes), journal.lignes
print("OK : image déjà absente = information, pas erreur")

# --- vraies erreurs : warning, retournées, et réessayées au passage suivant -
conflit = docker.errors.APIError("409 Client Error: conflict: image is being used by running container abc")
faux = FauxImages({"eval_1/svc:latest"}, {"eval_1/svc:latest": conflit})
r, journal, ancien = runner_avec(faux, ["eval_1/svc:latest"])
try:
    erreurs = r.remove_exam_images()
    assert len(erreurs) == 1 and "being used" in erreurs[0], erreurs
    assert len(avertissements(journal)) == 1
    faux.erreurs.clear()
    assert r.remove_exam_images() == [], "l'image en échec est réessayée puis supprimée"
    assert "eval_1/svc:latest" not in faux.presentes
finally:
    docker.from_env = ancien
print("OK : les vraies erreurs restent des avertissements et sont retournées")

# --- daemon injoignable ------------------------------------------------------
faux = FauxImages({"a:1"}, {"a:1": docker.errors.DockerException("Error while fetching server API version: connection refused")})
r, journal, ancien = runner_avec(faux, ["a:1"])
try:
    erreurs = r.remove_exam_images()
finally:
    docker.from_env = ancien
assert len(erreurs) == 1 and len(avertissements(journal)) == 1
print("OK : daemon injoignable = avertissement + erreur retournée")

# --- un message contenant « 404 » mais pas ImageNotFound n'est pas masqué ----
faux = FauxImages({"a:1"}, {"a:1": RuntimeError("404 proxy")})
r, journal, ancien = runner_avec(faux, ["a:1"])
try:
    assert len(r.remove_exam_images()) == 1
finally:
    docker.from_env = ancien
print("OK : seule ImageNotFound est tolérée")
