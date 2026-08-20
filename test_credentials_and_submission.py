"""Identifiants lus dans le rendu, et description de ce qui a ete rendu.

    uvx --with testcontainers --with docker --with requests --with pyyaml \
        python test_credentials_and_submission.py
"""
import os, sys, tempfile, logging, pathlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval.bento_compiled_runner import BentoCompiledRunner
from docker_eval.bentoml_runner import BentoMLRunner

log = logging.getLogger("t")


def write(root, rel, content):
    p = pathlib.Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# --- identifiants : le service fait foi, le test sert de repli ---------------
with tempfile.TemporaryDirectory() as d:
    r = BentoCompiledRunner("copie-test", d, 300, log)

    # rien nulle part
    assert r._find_credentials_in_submission([d]) is None

    # dans un test, en dur : c'est le cas frequent
    write(d, "tests/test_api.py",
          'BASE = "http://localhost:3000"\n'
          'resp = requests.post(BASE + "/login", json={"username": "toto", "password": "secret42"})\n')
    found = r._find_credentials_in_submission([d])
    assert found["username"] == "toto" and found["password"] == "secret42", found
    assert found["in_tests"] is True, found
    print("OK : identifiants lus dans le fichier de test, et signales comme tels")

    # le service declare les siens : ils priment
    write(d, "src/service.py", 'USERS = {"admin": "vrai-mdp"}\n')
    found = r._find_credentials_in_submission([d])
    assert found["username"] == "admin" and found["password"] == "vrai-mdp", found
    assert found["in_tests"] is False, found
    print("OK : le service prime sur les tests")

# --- auth=() et VALID_* ------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    r = BentoCompiledRunner("copie-test", d, 300, log)
    write(d, "tests/t.py", 'requests.get(url, auth=("u1", "p1"))\n')
    assert r._find_credentials_in_submission([d])["password"] == "p1"
    write(d, "src/service.py", 'VALID_USERNAME = "a"\nVALID_PASSWORD = "b"\n')
    assert r._find_credentials_in_submission([d])["username"] == "a"
    print("OK : auth=() et VALID_* reconnus")

# --- description du rendu ----------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    write(d, "README.md", "# Mon rendu\nJ'ai fait ceci.")
    write(d, "src/service.py", "x = 1")
    write(d, ".venv/lib/junk.py", "bruit")
    write(d, "__pycache__/x.pyc", "bruit")
    r = BentoMLRunner("copie-test", d, 300, log)
    described = r.describe_submission()
    paths = [p for p, _ in described["entries"]]
    assert "README.md" in paths and os.path.join("src", "service.py") in paths, paths
    assert not any(".venv" in p or "__pycache__" in p for p in paths), paths
    assert described["readme_name"] == "README.md"
    assert "J'ai fait ceci." in described["readme"]
    r.record_submission_step()
    step = r.steps[0]
    assert "README.md" in step["output"] and "J'ai fait ceci." in step["output"]
    print("OK : arborescence sans le bruit, README inclus")


# --- les motifs sont mutualises et le service prime sur les tests ------------
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval.utils import find_credentials_in_texts, looks_like_test_file

assert looks_like_test_file("tests/test_api.py") is True
assert looks_like_test_file("src/service.py") is False
assert looks_like_test_file("test_login.py") is True

# l'ordre recu decide : le service passe devant
service = ("src/service.py", 'USERS = {"admin": "du-service"}')
test = ("tests/test_api.py", 'json={"username": "u", "password": "du-test"}')
assert find_credentials_in_texts([service, test])["password"] == "du-service"
assert find_credentials_in_texts([test, service])["password"] == "du-test"
assert find_credentials_in_texts([test])["in_tests"] is True
assert find_credentials_in_texts([("vide.py", "")]) is None
print("OK : motifs mutualises, l'ordre decide de la priorite")
