"""Le crochet d'agent rend la main avec le rendu intact (proto agent unique).

    uvx --with testcontainers --with docker --with requests --with pyyaml python test_agent_hook.py
"""
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from docker_eval.agent_hook import avant_demontage  # noqa: E402


class Faux:
    def __init__(self, eval_dir):
        self.eval_dir = eval_dir
        self.steps = [{"title": "Sonde", "exit_code": 1}]
        self.logger = logging.getLogger("test")
        self.student_name = "copie-x"


with tempfile.TemporaryDirectory() as racine:
    rendu = os.path.join(racine, "rendu")
    os.makedirs(os.path.join(rendu, "conf"))
    with open(os.path.join(rendu, "conf", "nginx.conf"), "w") as f:
        f.write("original\n")
    sortie = os.path.join(racine, "agent")
    # L'« agent » réécrit un fichier, en crée un, en supprime un autre.
    script = os.path.join(racine, "agent.sh")
    with open(script, "w") as f:
        f.write(f"""#!/bin/sh
test -f "$STACK_HANDLE" || exit 3
echo modifie > {rendu}/conf/nginx.conf
echo intrus > {rendu}/nouveau.txt
""")
    os.chmod(script, 0o755)
    os.environ.update(EXAM_AGENT_HOOK=script, EXAM_AGENT_DIR=sortie)
    runner = Faux(rendu)
    avant_demontage(runner, [])
    assert open(os.path.join(rendu, "conf", "nginx.conf")).read() == "original\n"
    assert not os.path.exists(os.path.join(rendu, "nouveau.txt"))
    assert os.path.exists(os.path.join(sortie, "stack.json"))
    import json
    assert json.load(open(os.path.join(sortie, "stack.json")))["projet"] == "eval_copie-x"
    # Un second démontage (runners imbriqués) ne relance pas l'agent.
    avant_demontage(runner, [])
    # Sans EXAM_AGENT_HOOK, rien ne se passe.
    del os.environ["EXAM_AGENT_HOOK"]
    avant_demontage(Faux(rendu), [])
print("test_agent_hook : ok")
