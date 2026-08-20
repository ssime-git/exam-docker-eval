"""A qui la faute est imputee quand une evaluation echoue.

Lancer depuis ce repertoire :

    uvx --with testcontainers --with docker --with requests --with pyyaml \
        python test_fault_attribution.py
"""
import os, sys, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from docker_eval.bentoml_runner import BentoMLRunner

r = BentoMLRunner("x", "/tmp", 300, logging.getLogger("t"))
f = r._attribute_fault

EXEC_FMT = "exec /entrypoint.sh: exec format error\n"

# --- notre environnement -----------------------------------------------------
assert f({"qemu_available": False}, EXEC_FMT) == "systeme", "arch sans emulation = notre faute"
assert f({"container_oom_killed": True}, "") == "systeme", "OOM = notre machine"
assert f({}, "Bind for 0.0.0.0:3000 failed: port is already allocated") == "systeme"
assert f({"docker_error": "socket absent"}, "") == "systeme"
print("OK : les pannes d'environnement ne sont pas imputees a l'apprenant")

# --- la copie ----------------------------------------------------------------
assert f({"container_exit_code": 1}, "Traceback ...") == "apprenant"
assert f({"container_exit_code": 255}, "") == "apprenant"
assert f({"container_exit_code": 127}, "sh: uvicorn: not found") == "apprenant"
print("OK : un programme qui sort en erreur est imputable a la copie")

# --- on ne tranche pas --------------------------------------------------------
assert f({}, "") == "indetermine", "sans preuve, aucune accusation"
assert f({"container_exit_code": 0}, "") == "indetermine"
# emulation disponible : l'exec format error ne suffit plus a accuser personne
assert f({"qemu_available": True}, EXEC_FMT) == "indetermine"
print("OK : dans le doute, indetermine")
