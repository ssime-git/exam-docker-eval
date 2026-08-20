"""Verifie que le runner ne parle plus de delai quand le conteneur est mort.

Lancer depuis ce repertoire :

    uvx --with testcontainers --with docker --with requests --with pyyaml \
        python test_container_death_reporting.py
"""
import os, sys, types, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docker_eval.bentoml_runner import BentoMLRunner

# docker est importe a l'interieur des methodes : on le substitue apres coup
fake = types.ModuleType("docker")
class _Img:
    attrs = {"Architecture": "arm64"}
class _Ctr:
    status = "exited"
    attrs = {"State": {"ExitCode": 1}}
    def reload(self): pass
    def logs(self, tail=500):
        return b"exec /home/bentoml/bento/env/docker/entrypoint.sh: exec format error\n"
class _Client:
    class containers:
        @staticmethod
        def get(name): return _Ctr()
    class images:
        @staticmethod
        def get(name): return _Img()
fake.from_env = lambda: _Client()
sys.modules["docker"] = fake

r = BentoMLRunner("457363", "/tmp", 300, logging.getLogger("t"))
r.container_name = "peu-importe"
r.image_name = "admission_prediction_service:latest"

died, code = r._container_exit_state()
assert died is True and code == 1, (died, code)

msg = r._describe_container_death(code, 9.0)
assert "9.0s" in msg, msg
assert "300" not in msg, f"parle encore du delai configure : {msg}"
assert "arm64" in msg and "amd64" in msg, msg
assert "exec format error" in msg, msg
print("OK :", msg)

# conteneur introuvable -> on n'accuse pas
fake.from_env = lambda: (_ for _ in ()).throw(RuntimeError("pas de docker"))
assert r._container_exit_state() == (False, None)
print("OK : etat indeterminable, aucune accusation")
