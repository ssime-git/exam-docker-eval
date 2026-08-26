"""Le repli de demarrage lent doit voir les logs AUSSI en mode CLI.

`_wait_for_api_ready` accepte un service qui n'a repondu a aucune sonde HTTP
quand trois conditions tiennent : le port TCP est ouvert, le demarrage est
visible dans les logs du conteneur, et on est dans le dernier quart du delai.

Le mode CLI (image Docker imbriquee, pas de testcontainers) ne renseigne pas
`self.container` : les logs y etaient toujours vides, `saw_startup_signal`
toujours faux, et le repli inatteignable. La copie 459884 en est morte.

Lancer depuis ce repertoire :

    uvx --with testcontainers --with docker --with requests --with pyyaml \
        python test_readiness_logs_mode_cli.py
"""
import os, sys, types, logging, socket

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval.bentoml_runner import BentoMLRunner
from docker_eval import bentoml_runner as module
from docker_eval.config import API_STARTUP_TIMEOUT

# Le service demarre (le log le dit) mais ne repond a aucune route sondee.
LOGS = b"Starting production BentoServer from \"admission_prediction_service\"\n"

fake_docker = types.ModuleType("docker")
class _Ctr:
    def logs(self, tail=500):
        return LOGS
class _Client:
    class containers:
        @staticmethod
        def get(name):
            assert name == "bentoml_eval_459884", f"logs lus par un mauvais nom : {name}"
            return _Ctr()
fake_docker.from_env = lambda: _Client()
sys.modules["docker"] = fake_docker

# Aucune sonde HTTP ne repond.
class _RequestException(Exception): pass
def _get(*a, **k):
    raise _RequestException("connection refused")
module.requests = types.SimpleNamespace(
    get=_get, exceptions=types.SimpleNamespace(RequestException=_RequestException))

# Le port TCP est ouvert.
class _Sock:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def settimeout(self, _): pass
    def connect_ex(self, _): return 0
module.socket = types.SimpleNamespace(
    socket=_Sock, AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM)

# On demarre le chronometre dans le dernier quart du delai, et on n'attend pas.
depart = [0.0]
module.time = types.SimpleNamespace(
    time=lambda: depart[0], sleep=lambda _: depart.__setitem__(0, depart[0] + 2))

runner = BentoMLRunner.__new__(BentoMLRunner)
runner.logger = logging.getLogger("test")
runner.container = None                       # mode CLI : jamais renseigne
runner.cli_container_id = "abc123"            # ... mais un conteneur tourne
runner.container_name = "bentoml_eval_459884"
runner._http_timeout = lambda s: s
runner._capture_container_logs = BentoMLRunner._capture_container_logs.__get__(runner)

depart[0] = -(API_STARTUP_TIMEOUT * 0.75 + 4)  # elapsed deja dans le dernier quart
runner._wait_for_api_ready("http://127.0.0.1:32768")
print("OK : en mode CLI, le demarrage vu dans les logs debloque le repli")

# Sans conteneur CLI, rien a lire : le repli reste ferme et le delai expire.
runner.cli_container_id = None
depart[0] = -(API_STARTUP_TIMEOUT * 0.75 + 4)
try:
    runner._wait_for_api_ready("http://127.0.0.1:32768")
except TimeoutError:
    print("OK : sans conteneur, le repli ne s'ouvre pas et le delai expire")
else:
    raise AssertionError("le repli s'est ouvert sans conteneur a lire")
