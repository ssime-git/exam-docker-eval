"""L'action `exec` de l'investigateur est une lecture, jamais une écriture.

Rejeu du 25/09 (461451) : un test proposé par le LLM a lancé `apt-get install`
dans le conteneur de l'apprenant. L'ancienne liste noire lexicale ne
connaissait ni apt-get, ni pip, ni sed -i : on passe à une liste blanche.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval.investigator import commande_de_lecture  # noqa: E402


@pytest.mark.parametrize("commande", [
    "cat /etc/nginx/nginx.conf",
    "ls -la /app",
    "head -n 50 /var/log/app.log",
    "tail -n 20 /app/logs/api.log",
    "grep -i error /var/log/app.log",
    "ps aux",
    "env",
    "printenv PATH",
    "getent hosts api",
    "cat /etc/resolv.conf",
    "ss -ltn",
    "netstat -ltn",
    "nginx -T",
    "find /app -name '*.py'",
    "cat /app/config.yml | grep port",
    "curl -s http://localhost:8000/health",
    "wc -l /app/data.csv",
    "python --version",
    "pip list",
    "id",
    "whoami",
    "df -h",
    "printenv HOME PATH",
    "sort /app/a.txt",
    "uniq -c /app/a.txt",
    "date +%s",
    "hostname -I",
])
def test_lectures_autorisees(commande):
    assert commande_de_lecture(commande) is None, commande


@pytest.mark.parametrize("commande", [
    "apt-get install -y dnsutils",
    "apt update && apt install curl",
    "pip install requests",
    "npm install",
    "sed -i 's/a/b/' /app/x.conf",
    "python -c 'open(\"/app/x\",\"w\").write(\"y\")'",
    "echo x > /app/x",
    "cat a >> b",
    "ls; rm -rf /app",
    "ls && touch /app/x",
    "ls || touch /app/x",
    "cat $(touch /app/x)",
    "cat `touch /app/x`",
    "find /app -delete",
    "find /app -exec rm {} \\;",
    "curl -o /app/x http://example.com",
    "curl -X POST http://localhost:8000/predict",
    "curl -d 'a=1' http://localhost:8000/predict",
    "kill 1",
    "nginx -s reload",
    "pip uninstall -y requests",
    "cat /app/a | tee /app/b",
    "env rm -rf /app",
    "printenv; rm x",
    "sort -o /app/x /app/y",
    "sort --output=/app/x /app/y",
    "uniq /app/a /app/b",
    "date -s '2020-01-01'",
    "hostname pirate",
    "ss -K dst 1.2.3.4",
    "",
])
def test_ecritures_refusees(commande):
    assert commande_de_lecture(commande) is not None, commande
