"""Stack servante : un examen nginx ne se termine pas, il tient debout.

Lancer depuis ce repertoire :

    uvx --with pytest --with testcontainers --with docker --with requests \
        --with pyyaml python test_serving_stack.py
"""
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval.compose_runner import ComposeRunner

log = logging.getLogger("t")

# --- detection de la forme ---------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    f = os.path.join(d, "docker-compose.yml")
    with open(f, "w") as h:
        h.write("services:\n  pipeline:\n    image: x\n  db:\n    image: y\n")
    assert ComposeRunner._services_du_compose(f) == ["db", "pipeline"]
    with open(f, "w") as h:
        h.write("services:\n  nginx:\n    image: x\n  api-v1:\n    image: y\n")
    services = ComposeRunner._services_du_compose(f)
    assert "pipeline" not in services and services == ["api-v1", "nginx"]
print("OK : la presence du service pipeline decide de la forme")

# --- classement des services -------------------------------------------------
classer = ComposeRunner._classer_services
ok, morts = classer({"nginx": ("running", 0), "api": ("running", 0)})
assert ok and not morts
ok, morts = classer({"nginx": ("running", 0), "init-certs": ("exited", 0)})
assert ok, "un one-shot sorti en 0 n'est pas un echec"
ok, morts = classer({"nginx": ("exited", 1), "api": ("running", 0)})
assert not ok and "nginx" in morts
ok, morts = classer({"api": ("dead", 0)})
assert not ok and "api" in morts
print("OK : debout = sain, one-shot code 0 = sain, sorti autrement = mort")
