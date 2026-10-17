"""462113 : tags réellement présents dans le tar de l'image.

Le README lance `lr_classifier_service:latest`, le seul tag du tar est
`lr_classifier_service:qdo3i3fmj6dwm7my`. Le barème doit pouvoir comparer :
`image_tags` est lu dans manifest.json (ou index.json), et tous les
« Loaded image » de `docker load` sont pris, pas seulement le premier.
"""
import io
import json
import logging
import os
import sys
import tarfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner


def tar_avec(chemin, fichiers):
    with tarfile.open(chemin, "w") as archive:
        for nom, contenu in fichiers.items():
            donnees = json.dumps(contenu).encode()
            info = tarfile.TarInfo(nom)
            info.size = len(donnees)
            archive.addfile(info, io.BytesIO(donnees))
    return str(chemin)


def runner_pour(tmp_path, tar):
    runner = BentoMLRunner("462113", str(tmp_path), 60, logging.getLogger("t"))
    runner.image_tar = tar
    return runner


def test_462113_tags_du_manifest(tmp_path):
    tar = tar_avec(tmp_path / "image.tar", {
        "manifest.json": [{"Config": "blobs/sha256/abc",
                           "RepoTags": ["lr_classifier_service:qdo3i3fmj6dwm7my"], "Layers": []}],
        "index.json": {"manifests": [{"annotations": {
            "io.containerd.image.name": "docker.io/library/lr_classifier_service:qdo3i3fmj6dwm7my",
            "org.opencontainers.image.ref.name": "qdo3i3fmj6dwm7my"}}]},
    })
    assert runner_pour(tmp_path, tar)._infos_image()["image_tags"] == [
        "lr_classifier_service:qdo3i3fmj6dwm7my"]


def test_tags_de_l_index_oci_sans_manifest(tmp_path):
    tar = tar_avec(tmp_path / "image.tar", {"index.json": {"manifests": [{"annotations": {
        "io.containerd.image.name": "docker.io/library/svc:1.0"}}]}})
    assert runner_pour(tmp_path, tar)._infos_image()["image_tags"] == ["docker.io/library/svc:1.0"]


def test_tar_sans_tag_rend_une_liste_vide(tmp_path):
    tar = tar_avec(tmp_path / "image.tar", {"manifest.json": [{"RepoTags": None, "Layers": []}]})
    assert runner_pour(tmp_path, tar)._infos_image()["image_tags"] == []


class _R:
    def __init__(self, code=0, sortie=""):
        self.returncode, self.stdout, self.stderr = code, sortie, ""


def test_tous_les_loaded_image_sont_pris(tmp_path, monkeypatch):
    tar = tar_avec(tmp_path / "image.tar", {"manifest.json": []})
    runner = runner_pour(tmp_path, tar)
    commandes = []

    def faux_run(commande, **kwargs):
        commandes.append(list(commande))
        if commande[:2] == ["docker", "load"]:
            return _R(0, "Loaded image: svc:latest\nLoaded image: svc:qdo3\n")
        if commande[:3] == ["docker", "image", "inspect"]:
            return _R(0, "sha256:" + "0" * 64 + "\n")
        return _R(0, "")

    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    monkeypatch.setattr("docker_eval.base_runner.subprocess.run", faux_run)
    image = runner._load_docker_image_cli()
    assert image is not None
    tagues = [c[2] for c in commandes if c[:2] == ["docker", "tag"]]
    # Chaque image chargée est adoptée, donc supprimée au ménage.
    assert tagues == ["svc:latest", "svc:qdo3"]
    assert len(runner._exam_images) == 2
    assert runner._infos_image()["images_chargees"] == ["svc:latest", "svc:qdo3"]
