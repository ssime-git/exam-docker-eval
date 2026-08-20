"""Collecte pytest : les tests de l'apprenant doivent pouvoir s'importer.

Le defaut d'origine : la copie patchee des tests partait dans un repertoire
temporaire detache du projet. Le conftest de l'apprenant remonte vers `../src`
pour importer son code ; depuis le detache, la remontee ne menait nulle part,
la collecte echouait, et zero test collecte se lisait comme « pas de tests ».

Lancer depuis ce repertoire :

    uvx --with pytest --with testcontainers --with docker --with requests \
        --with pyyaml python test_pytest_collection.py
"""
import logging
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval.bentoml_runner import BentoMLRunner

log = logging.getLogger("t")

# --- racine du projet depuis le dossier de tests ---------------------------
with tempfile.TemporaryDirectory() as racine:
    projet = os.path.join(racine, "examen_bentoml")
    tests = os.path.join(projet, "tests")
    os.makedirs(tests)
    r = BentoMLRunner("copie-test", racine, 300, log)
    assert r._project_root_for_tests(tests) == projet, r._project_root_for_tests(tests)
    fichier = os.path.join(tests, "test_x.py")
    open(fichier, "w").close()
    assert r._project_root_for_tests(fichier) == projet
    # Sans parent exploitable, on retombe sur le repertoire d'evaluation.
    assert r._project_root_for_tests("/") == racine
print("OK : la racine du projet est le parent du dossier de tests")

# --- lecture du rapport JUnit ----------------------------------------------
r = BentoMLRunner("copie-test", "/tmp", 300, log)
assert r._read_junit_report("/n/existe/pas.xml") is None

with tempfile.TemporaryDirectory() as d:
    chemin = os.path.join(d, "r.xml")
    with open(chemin, "w") as f:
        f.write(
            '<testsuites><testsuite tests="5" failures="1" errors="1" skipped="1">'
            "</testsuite></testsuites>"
        )
    assert r._read_junit_report(chemin) == (2, 1, 1, 5), r._read_junit_report(chemin)

    with open(chemin, "w") as f:
        f.write('<testsuite tests="3" failures="0" errors="0" skipped="0"></testsuite>')
    assert r._read_junit_report(chemin) == (3, 0, 0, 3)

    with open(chemin, "w") as f:
        f.write("pas du xml")
    assert r._read_junit_report(chemin) is None
print("OK : le compte des tests se lit dans le rapport JUnit")

# --- le cas reel : un conftest qui importe le code de l'apprenant ----------
# Sans PYTHONPATH ni cwd sur le projet, la collecte echoue ; avec, elle passe.
with tempfile.TemporaryDirectory() as racine:
    projet = os.path.join(racine, "examen_bentoml")
    os.makedirs(os.path.join(projet, "src"))
    os.makedirs(os.path.join(projet, "tests"))
    open(os.path.join(projet, "src", "__init__.py"), "w").close()
    with open(os.path.join(projet, "src", "auth.py"), "w") as f:
        f.write("def jeton():\n    return 'ok'\n")
    with open(os.path.join(projet, "tests", "conftest.py"), "w") as f:
        f.write("from src.auth import jeton  # noqa: F401\n")
    with open(os.path.join(projet, "tests", "test_auth.py"), "w") as f:
        f.write("from src.auth import jeton\n\n\ndef test_jeton():\n    assert jeton() == 'ok'\n")

    junit = os.path.join(racine, "r.xml")
    base = [sys.executable, "-m", "pytest", os.path.join(projet, "tests"),
            "-q", f"--junit-xml={junit}"]

    # detache : ni cwd ni PYTHONPATH sur le projet
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    detache = subprocess.run(base, cwd=racine, env=env, capture_output=True, text=True)
    assert detache.returncode != 0, detache.stdout

    # rattache : ce que fait le correcteur maintenant
    env["PYTHONPATH"] = projet
    attache = subprocess.run(base, cwd=projet, env=env, capture_output=True, text=True)
    assert attache.returncode == 0, attache.stdout + attache.stderr
    assert r._read_junit_report(junit) == (1, 0, 0, 1), r._read_junit_report(junit)
print("OK : les tests de l'apprenant s'importent depuis la racine du projet")
