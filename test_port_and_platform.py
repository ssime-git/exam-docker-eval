"""Ports hote relaches et choix de plateforme.

Lancer depuis ce repertoire :

    uvx --with testcontainers --with docker --with requests --with pyyaml \
        python test_port_and_platform.py
"""
import os, sys, types, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docker_eval.compose_runner import ComposeRunner
from docker_eval.bentoml_runner import BentoMLRunner

log = logging.getLogger("t")

# --- ne garder que le port du conteneur ------------------------------------
only = ComposeRunner._container_port_only
assert only("3000:3000") == "3000", only("3000:3000")
assert only("127.0.0.1:8080:80") == "80", only("127.0.0.1:8080:80")
assert only("8080:80/tcp") == "80/tcp", only("8080:80/tcp")
assert only("80") == "80", only("80")            # deja sans port hote
assert only(9090) == 9090                        # entier laisse tel quel
assert only({"target": 80, "published": 8080}) == {"target": 80}
assert only({"target": 80}) == {"target": 80}
print("OK : seul le port du conteneur est conserve")

# --- choix de la plateforme ------------------------------------------------
r = BentoMLRunner("457363", "/tmp", 300, log)
r.image_name = "peu:importe"

r._host_arch = lambda: "amd64"

r._image_arch = lambda: "amd64"
assert r._platform_for_image() is None, "meme architecture : rien a forcer"

r._image_arch = lambda: "arm64"
r._qemu_available = lambda: True
assert r._platform_for_image() == "linux/arm64", r._platform_for_image()

r._qemu_available = lambda: False
assert r._platform_for_image() is None, "sans QEMU on ne force rien"

r._image_arch = lambda: None
r._qemu_available = lambda: True
assert r._platform_for_image() is None, "architecture illisible : rien a forcer"
print("OK : plateforme forcee seulement si utile et possible")

# --- port publie relu aupres de docker -------------------------------------
r2 = BentoMLRunner("x", "/tmp", 300, log)
r2.container_name = "peu-importe"
fake = types.ModuleType("subprocess")
class _Res:
    stdout = "0.0.0.0:49154\n[::]:49154\n"
    stderr = ""
fake.run = lambda *a, **k: _Res()
import docker_eval.bentoml_runner as mod
mod.subprocess = fake
assert r2._published_host_port_cli(3000) == 49154
_Res.stdout = ""
try:
    r2._published_host_port_cli(3000)
    raise AssertionError("aurait du lever")
except RuntimeError:
    pass
print("OK : port publie relu, et erreur claire s'il manque")
