"""Les fichiers AppleDouble d'un rendu macOS ne doivent jamais faire tomber
l'évaluation, et une panne de pytest ne doit jamais effacer le reste.

469871 : `tests/._test_api.py` (163 octets binaires, métadonnées macOS) faisait
lever `UnicodeDecodeError` à la préparation des tests ; l'exception remontait
jusqu'à l'échec critique alors que /login et /predict avaient répondu. Le
barème a noté 8/20 une copie dont l'API fonctionnait.

    uvx --with pytest --with testcontainers --with docker --with requests \
        --with pyyaml pytest -q test_appledouble_tests.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import bentoml_runner
from docker_eval.bento_compiled_runner import BentoCompiledRunner
from docker_eval.bentoml_runner import BentoMLRunner

TEST_API = '''import requests

BASE_URL = "http://localhost:3000"


def test_login():
    assert requests.post(f"{BASE_URL}/login").status_code == 200
'''

# Octet 0xa3 en position 45, comme l'en-tête AppleDouble de 469871.
APPLEDOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \x00\x02" + b"\x00" * 19 + b"\xa3" + b"\x00" * 120
assert APPLEDOUBLE[45] == 0xA3 and len(APPLEDOUBLE) >= 163 - 40


def ecrire(chemin, contenu):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    mode, enc = ("wb", None) if isinstance(contenu, bytes) else ("w", "utf-8")
    with open(chemin, mode, encoding=enc) as f:
        f.write(contenu)


class _Resultat:
    def __init__(self, code, sortie=""):
        self.returncode, self.stdout, self.stderr = code, sortie, ""


def _runner(tmp_path):
    return BentoMLRunner("469871", str(tmp_path), 300, logging.getLogger("t"))


def _rendu_macos(tmp_path):
    ecrire(tmp_path / "tests" / "test_api.py", TEST_API)
    ecrire(tmp_path / "tests" / "._test_api.py", APPLEDOUBLE)
    ecrire(tmp_path / "__MACOSX" / "tests" / "._test_api.py", APPLEDOUBLE)
    ecrire(tmp_path / "__MACOSX" / "tests" / "test_ombre.py", b"\xff\xfe" + TEST_API.encode())


def test_la_preparation_saute_les_fichiers_appledouble(tmp_path):
    _rendu_macos(tmp_path)
    _runner(tmp_path)._prepare_tests_for_dynamic_base_url(
        str(tmp_path / "tests"), "http://localhost:32772")
    assert 'os.getenv("BENTOML_BASE_URL", "http://localhost:32772")' in (tmp_path / "tests" / "test_api.py").read_text()
    assert (tmp_path / "tests" / "._test_api.py").read_bytes() == APPLEDOUBLE


def test_la_preparation_saute_le_dossier_macosx(tmp_path):
    _rendu_macos(tmp_path)
    ombre = (tmp_path / "__MACOSX" / "tests" / "test_ombre.py").read_bytes()
    _runner(tmp_path)._prepare_tests_for_dynamic_base_url(str(tmp_path), "http://localhost:32772")
    assert (tmp_path / "__MACOSX" / "tests" / "test_ombre.py").read_bytes() == ombre


def test_un_fichier_de_tests_non_utf8_reste_tel_quel_sans_lever(tmp_path):
    ecrire(tmp_path / "tests" / "test_api.py", TEST_API)
    latin1 = 'BASE_URL = "http://localhost:3000"  # caf\xe9\n'.encode("latin-1")
    ecrire(tmp_path / "tests" / "test_latin1.py", latin1)
    _runner(tmp_path)._prepare_tests_for_dynamic_base_url(
        str(tmp_path / "tests"), "http://localhost:32772")
    assert (tmp_path / "tests" / "test_latin1.py").read_bytes() == latin1
    # Le fichier lisible voisin est quand même réécrit.
    assert "BENTOML_BASE_URL" in (tmp_path / "tests" / "test_api.py").read_text()


def _pytest_lance(tmp_path, monkeypatch, codes):
    """Lance _run_pytest avec un uvx simulé ; rend (résultats, commandes)."""
    runner = _runner(tmp_path)
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    commandes = []

    def faux_run(commande, **kwargs):
        commandes.append(list(commande))
        return _Resultat(codes[min(len(commandes), len(codes)) - 1], "")

    monkeypatch.setattr(bentoml_runner.subprocess, "run", faux_run)
    return runner._run_pytest("http://localhost:32772"), commandes


def test_pytest_ne_recoit_jamais_un_fichier_appledouble(tmp_path, monkeypatch):
    _rendu_macos(tmp_path)
    ecrire(tmp_path / "tests" / "._autre_test.py", APPLEDOUBLE)
    # Code 5 : rien collecté, le harnais relance avec la liste explicite.
    _, commandes = _pytest_lance(tmp_path, monkeypatch, [5, 5])
    assert len(commandes) >= 2
    for commande in commandes:
        assert not [a for a in commande if not a.startswith("--") and os.path.basename(a).startswith("._")], commande
        assert not [a for a in commande if "__MACOSX" in a and not a.startswith("--ignore")], commande
        # Et pytest lui-même n'a pas à collecter `._autre_test.py` (motif *_test.py).
        assert any(a.startswith("--ignore-glob") and "._" in a for a in commande), commande


def test_une_panne_de_preparation_ne_leve_pas_et_dit_pourquoi(tmp_path, monkeypatch):
    ecrire(tmp_path / "tests" / "test_api.py", TEST_API)
    runner = _runner(tmp_path)
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")

    def panne(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xa3", 0, 1, "invalid start byte")

    monkeypatch.setattr(runner, "_prepare_tests_for_dynamic_base_url", panne)
    resultats = runner._run_pytest("http://localhost:32772")
    assert resultats["executed"] is False
    assert resultats["evaluable"] is False
    assert "pytest non exécuté" in resultats["reason"]
    assert "UnicodeDecodeError" in resultats["reason"]


def test_une_panne_de_pytest_ne_touche_pas_aux_resultats_login_predict(tmp_path, monkeypatch):
    """Les trois appelants de _run_pytest rangent son résultat à côté de
    test_results : tant qu'il rend un dict au lieu de lever, login/predict
    survivent. Le barème lit `evaluable` pour sortir la catégorie."""
    ecrire(tmp_path / "tests" / "test_api.py", TEST_API)
    runner = _runner(tmp_path)
    monkeypatch.setattr(bentoml_runner.shutil, "which", lambda nom: "/usr/bin/uvx")
    monkeypatch.setattr(bentoml_runner.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("uvx a disparu")))
    test_results = {"login_passed": True, "predict_passed": True, "all_passed": True}
    pytest_results = runner._run_pytest("http://localhost:32772")
    assert test_results["all_passed"] is True
    assert pytest_results["executed"] is False and "uvx a disparu" in pytest_results["reason"]


def test_le_runner_compile_saute_appledouble_et_ne_leve_pas(tmp_path, monkeypatch):
    _rendu_macos(tmp_path)
    runner = BentoCompiledRunner("469871", str(tmp_path), 300, logging.getLogger("t"))
    import docker_eval.bento_compiled_runner as module
    vus = []

    def faux_run(commande, **kwargs):
        vus.append(list(commande))
        return _Resultat(0, "1 passed in 0.1s")

    monkeypatch.setattr(module.subprocess, "run", faux_run)
    resultats = runner._run_pytest("http://localhost:32772")
    assert resultats["executed"] is True and resultats["passed"] == 1
    # Panne imprévue : un dict « non exécuté », pas une exception.
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boum")))
    resultats = runner._run_pytest("http://localhost:32772")
    assert resultats["executed"] is False and "boum" in resultats["reason"]
