"""Reconstruction datée (467094) : une copie qui marchait à la date du rendu
est notée sur une reconstruction aux versions de l'époque.

bike-api plantait sur `ImportError: pyarrow requires NumPy 2.0 or newer,
found 1.26.4` : pyarrow, transitif et non épinglé, publié en 26.0 le 09/10 ;
les fichiers de l'archive datent du 03/10. Le moteur rebâtit une fois la
stack avec les paquets contraints à la date de référence (mtime le plus
récent des fichiers du rendu) et, si elle devient saine, l'évalue.
"""
import datetime as dt
import logging
import os
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import docker_eval.compose_runner as module
from docker_eval import reconstruction_datee as rd
from docker_eval.compose_runner import ComposeRunner

UTC = dt.timezone.utc
RENDU = dt.datetime(2026, 10, 3, 18, 55, tzinfo=UTC)


def poser(chemin, contenu="x", quand=RENDU):
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(contenu)
    os.utime(chemin, (quand.timestamp(), quand.timestamp()))


# --- date de référence -------------------------------------------------------

def test_date_reference_est_le_mtime_le_plus_recent(tmp_path):
    poser(tmp_path / "src/api/main.py", quand=RENDU)
    poser(tmp_path / "README.md", quand=RENDU - dt.timedelta(hours=3))
    ref = rd.date_reference(str(tmp_path), maintenant=RENDU + dt.timedelta(days=13))
    assert ref["date_reference"] == "2026-10-03T18:55:00Z"
    assert "src/api/main.py" in ref["source"]
    assert ref["fichier"] == "src/api/main.py"


def test_date_reference_ignore_appledouble_futur_et_absurde(tmp_path):
    poser(tmp_path / "main.py", quand=RENDU)
    poser(tmp_path / "__MACOSX/._main.py", quand=RENDU + dt.timedelta(days=5))
    poser(tmp_path / "._README.md", quand=RENDU + dt.timedelta(days=5))
    poser(tmp_path / "futur.txt", quand=RENDU + dt.timedelta(days=400))
    poser(tmp_path / "zip_dos.txt", quand=dt.datetime(1980, 1, 1, tzinfo=UTC))
    ref = rd.date_reference(str(tmp_path), maintenant=RENDU + dt.timedelta(days=13))
    assert ref["date_reference"] == "2026-10-03T18:55:00Z"
    assert ref["ignores"] == 2  # futur + avant 2020 ; AppleDouble écartés sans compter


def test_date_reference_ignore_les_fichiers_du_harnais(tmp_path):
    poser(tmp_path / "docker-compose.yml", quand=RENDU)
    poser(tmp_path / "docker-compose.eval.yml", quand=RENDU + dt.timedelta(days=13))
    poser(tmp_path / ".pi-evidence/reparations.json", quand=RENDU + dt.timedelta(days=13))
    ref = rd.date_reference(str(tmp_path), maintenant=RENDU + dt.timedelta(days=13, hours=1))
    assert ref["date_reference"] == "2026-10-03T18:55:00Z"


def test_date_reference_absente_sans_fichier_plausible(tmp_path):
    poser(tmp_path / "vieux.txt", quand=dt.datetime(2001, 1, 1, tzinfo=UTC))
    ref = rd.date_reference(str(tmp_path), maintenant=RENDU)
    assert ref["date_reference"] is None
    assert "aucun" in ref["source"]


def test_date_reference_ne_suit_pas_les_liens(tmp_path):
    poser(tmp_path / "main.py", quand=RENDU)
    cible = tmp_path.parent / "hors_rendu.txt"
    poser(cible, quand=RENDU + dt.timedelta(days=2))
    os.symlink(cible, tmp_path / "lien.txt")
    ref = rd.date_reference(str(tmp_path), maintenant=RENDU + dt.timedelta(days=13))
    assert ref["date_reference"] == "2026-10-03T18:55:00Z"


# --- déclencheur --------------------------------------------------------------

def test_seul_le_conflit_de_versions_declenche():
    assert rd.doit_reconstruire({"cause": "conflit_versions_dependances", "imputation": "indetermine"})
    assert not rd.doit_reconstruire({"cause": "plantage_application", "imputation": "copie"})
    assert not rd.doit_reconstruire({"cause": "reseau_sortant", "imputation": "non_mesure"})
    assert not rd.doit_reconstruire(None)


# --- contraintes ----------------------------------------------------------------

def releases(**versions):
    """{version: date ISO de son premier fichier}."""
    return {"releases": {v: [{"upload_time_iso_8601": d, "yanked": False}] for v, d in versions.items()}}


PYPI = {
    "pyarrow": releases(**{"21.0.0": "2025-07-17T10:00:00Z", "25.0.0": "2026-07-20T10:00:00Z",
                          "26.0.0": "2026-10-09T08:00:00Z", "26.0.1": "2026-10-14T08:00:00Z"}),
    "numpy": releases(**{"1.26.4": "2024-02-05T00:00:00Z", "2.4.0": "2026-10-10T00:00:00Z"}),
    "six": releases(**{"1.17.0": "2024-12-04T00:00:00Z"}),
}

FREEZE = "numpy==1.26.4\npyarrow==26.0.1\nsix==1.17.0\n-e git+https://x/y#egg=z\n"


def test_versions_posterieures_a_la_date():
    assert rd.versions_posterieures(PYPI["pyarrow"], RENDU) == ["26.0.0", "26.0.1"]
    assert rd.versions_posterieures(PYPI["six"], RENDU) == []


def test_contraintes_excluent_les_versions_publiees_apres_le_rendu():
    lignes, contraints, notes = rd.contraintes(FREEZE, {"numpy"}, PYPI.get, RENDU)
    # pyarrow installé en 26.0.1, publié après le rendu : toutes les versions
    # postérieures sont exclues ; numpy est épinglé par la copie, six est d'époque.
    assert lignes == ["pyarrow!=26.0.0", "pyarrow!=26.0.1"]
    assert contraints == [{"paquet": "pyarrow", "installe": "26.0.1", "exclues": ["26.0.0", "26.0.1"]}]
    assert notes == []


def test_paquet_inconnu_de_pypi_est_note_sans_bloquer():
    lignes, contraints, notes = rd.contraintes("mystere==1.0\n", set(), lambda _n: None, RENDU)
    assert lignes == [] and contraints == []
    assert "mystere" in notes[0]


def test_paquets_epingles_lus_dans_les_requirements():
    texte = "fastapi==0.116.1\nuvicorn[standard]==0.35.0\nNumPy == 1.26.4  # pin\nrequests>=2\n-r autre.txt\n"
    assert rd.epingles([texte]) == {"fastapi", "uvicorn", "numpy"}


# --- injection ----------------------------------------------------------------

DOCKERFILE = """FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV PORT=$PORT
"""


def test_dockerfile_date_injecte_apres_chaque_from_sans_toucher_le_reste():
    texte = rd.dockerfile_date(DOCKERFILE, "2026-10-03T18:55:00Z", ["pyarrow!=26.0.0"])
    lignes = texte.splitlines()
    assert lignes[0] == "FROM python:3.12-slim"
    assert "ARG PIP_CONSTRAINT=/tmp/scriptorium-contraintes-datees.txt" in lignes
    assert "ARG UV_EXCLUDE_NEWER=2026-10-03T18:55:00Z" in lignes
    assert "ARG PIP_UPLOADED_PRIOR_TO=2026-10-03T18:55:00Z" in lignes
    assert "pyarrow!=26.0.0" in lignes
    # le reste du Dockerfile, intact et dans l'ordre
    assert texte.endswith("RUN pip install --no-cache-dir -r requirements.txt\nENV PORT=$PORT\n")


def test_dockerfile_date_multi_etapes_et_scratch():
    texte = rd.dockerfile_date("FROM python:3.12 AS b\nRUN pip install x\nFROM scratch\nCOPY --from=b /a /a\n",
                               "2026-10-03T18:55:00Z", ["x!=2"])
    assert texte.count("ARG PIP_CONSTRAINT=") == 1  # pas dans FROM scratch
    assert texte.count("ARG UV_EXCLUDE_NEWER=") == 1


def test_compose_date_derive_les_builds_et_echappe_les_dollars(tmp_path):
    poser(tmp_path / "src/api/Dockerfile", DOCKERFILE)
    poser(tmp_path / "src/api/requirements.txt", "numpy==1.26.4\n")
    doc = {"services": {
        "bike-api": {"build": "./src/api", "healthcheck": {"test": ["CMD", "true"]}},
        "grafana": {"image": "grafana/grafana:12.1.1"},
    }}
    derive = rd.compose_date(doc, str(tmp_path), "2026-10-03T18:55:00Z", {"bike-api": ["pyarrow!=26.0.0"]})
    build = derive["services"]["bike-api"]["build"]
    assert build["context"] == "./src/api"
    assert "dockerfile" not in build
    assert "ENV PORT=$$PORT" in build["dockerfile_inline"]  # compose interpole le YAML
    assert "pyarrow!=26.0.0" in build["dockerfile_inline"]
    assert derive["services"]["grafana"] == {"image": "grafana/grafana:12.1.1"}
    assert doc["services"]["bike-api"]["build"] == "./src/api"  # l'entrée n'est pas modifiée


def test_compose_date_respecte_le_dockerfile_declare(tmp_path):
    poser(tmp_path / "api/docker/Prod.Dockerfile", "FROM python:3.12\nRUN pip install -r r.txt\n")
    doc = {"services": {"api": {"build": {"context": "api", "dockerfile": "docker/Prod.Dockerfile",
                                          "args": {"A": "1"}}}}}
    derive = rd.compose_date(doc, str(tmp_path), "2026-10-03T18:55:00Z", {})
    build = derive["services"]["api"]["build"]
    assert build["args"] == {"A": "1"}
    assert "RUN pip install -r r.txt" in build["dockerfile_inline"]
    assert "PIP_CONSTRAINT" not in build["dockerfile_inline"]  # aucune contrainte : pas de fichier


# --- intégration au moteur ----------------------------------------------------

LOGS_467094 = (
    "Traceback (most recent call last):\n"
    '  File "/usr/local/lib/python3.12/site-packages/pyarrow/__init__.py", line 59, in <module>\n'
    "ImportError: pyarrow requires NumPy 2.0 or newer, found 1.26.4\n"
)


def etat_unhealthy():
    return {
        "State": {"Status": "running", "ExitCode": 1, "OOMKilled": False,
                  "Health": {"Status": "unhealthy", "FailingStreak": 3, "Log": []}},
        "RestartCount": 5,
        "Config": {"Labels": {"com.docker.compose.service": "bike-api",
                              "com.docker.compose.project": "eval_copie-test"},
                   "Env": [], "Healthcheck": {"Test": ["CMD", "true"]}},
    }


class Conteneur:
    def __init__(self, attrs, logs):
        self.name = "eval-bike-api-1"
        self.attrs = attrs
        self.status = attrs["State"]["Status"]
        self.labels = attrs["Config"]["Labels"]
        self._logs = logs

    def reload(self):
        pass

    def logs(self, tail=None):
        return self._logs


def copie_467094(tmp_path):
    poser(tmp_path / "docker-compose.yml",
          "services:\n  bike-api:\n    build: ./src/api\n    ports:\n      - \"8080:8080\"\n")
    poser(tmp_path / "src/api/Dockerfile", DOCKERFILE)
    poser(tmp_path / "src/api/requirements.txt", "numpy==1.26.4\nevidently==0.7.23\n")
    poser(tmp_path / "src/api/main.py", "import evidently\n")


def runner_467094(tmp_path, monkeypatch, reconstruction_saine=True):
    copie_467094(tmp_path)
    runner = ComposeRunner("copie-test", str(tmp_path), 1800, logging.getLogger("datee"))
    lances = []

    class Compose:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            lances.append(self.kwargs["compose_file_name"])
            datee = self.kwargs["compose_file_name"] == "docker-compose.datee.yml"
            if not (datee and reconstruction_saine):
                raise subprocess.CalledProcessError(
                    1, ["docker", "compose", "up"], output=b"",
                    stderr=b"dependency failed to start: container bike-api-1 is unhealthy\n")

        def __exit__(self, *_a):
            return False

        def stop(self):
            pass

    monkeypatch.setattr(module, "DockerCompose", Compose)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner, "cleanup", lambda force=False: None)
    monkeypatch.setattr(runner, "_jouer_lancement", lambda: None)
    monkeypatch.setattr(runner, "_conteneurs_du_projet",
                        lambda: [Conteneur(etat_unhealthy(), LOGS_467094.encode())])
    monkeypatch.setattr(runner, "_retenir_images_de_la_copie", lambda: None)
    monkeypatch.setattr(runner, "_images_des_services_construits", lambda: {"bike-api": "sha256:ancienne"})
    monkeypatch.setattr(runner, "_freeze_image", lambda image: FREEZE)
    monkeypatch.setattr(runner, "_pypi_json", PYPI.get)
    monkeypatch.setattr(runner, "_wait_for_services", lambda: None)
    monkeypatch.setattr(runner, "_completer_environnement_compose", lambda: None)
    monkeypatch.setattr(runner, "_evaluer_services_persistants",
                        lambda: {"success": True, "exit_code": 0, "steps": runner.steps})
    return runner, lances


def test_reconstruction_saine_evalue_la_stack_datee(tmp_path, monkeypatch):
    runner, lances = runner_467094(tmp_path, monkeypatch)
    origine = (tmp_path / "src/api/Dockerfile").read_text()

    result = runner.run_evaluation()

    assert lances == ["docker-compose.eval.yml", "docker-compose.datee.yml"]  # une seule reconstruction
    assert result["success"] is True
    assert "fault" not in result
    rec = result["reconstruction_datee"]
    assert rec["resultat"] == "saine"
    assert rec["date_reference"] == "2026-10-03T18:55:00Z"
    assert "mtime" in rec["source"]
    assert "PIP_CONSTRAINT" in rec["methode"]
    assert rec["paquets_contraints"][0]["paquet"] == "pyarrow"
    assert rec["preuve"] == "ImportError: pyarrow requires NumPy 2.0 or newer, found 1.26.4"
    cause = result["cause_demarrage"]
    assert cause["imputation"] == "copie_datee"
    assert cause["cause"] == "derive_dependance"
    assert cause["service"] == "bike-api"
    assert cause["cause_initiale"]["cause"] == "conflit_versions_dependances"
    rep = next(r for r in result["reparations"] if r["id"] == "reconstruction_datee")
    assert rep["critere_touche"] == "reproductibilite"
    assert "pyarrow requires NumPy" in rep["tel_que_livre"]["extrait"]
    assert "2026-10-03" in rep["applique"]
    assert result["date_reference"]["date_reference"] == "2026-10-03T18:55:00Z"
    # le compose dérivé vit dans la copie de travail ; les fichiers d'origine sont intacts
    derive = yaml.safe_load((tmp_path / "docker-compose.datee.yml").read_text())
    assert "pyarrow!=26.0.1" in derive["services"]["bike-api"]["build"]["dockerfile_inline"]
    assert (tmp_path / "src/api/Dockerfile").read_text() == origine
    # l'image remplacée (même tag, nouvel ID) est retenue pour le ménage
    assert "sha256:ancienne" in runner._exam_images
    diag = next(s for s in result["steps"] if s["title"] == "Diagnostic des services non sains")
    assert diag["exit_code"] == 0
    etape = next(s for s in result["steps"] if s["title"] == "Reconstruction datée")
    assert etape["exit_code"] == 0


def test_reconstruction_en_echec_garde_le_classement(tmp_path, monkeypatch):
    runner, lances = runner_467094(tmp_path, monkeypatch, reconstruction_saine=False)

    result = runner.run_evaluation()

    assert lances == ["docker-compose.eval.yml", "docker-compose.datee.yml"]
    assert result["reconstruction_datee"]["resultat"] == "echec"
    assert result["cause_demarrage"]["imputation"] == "indetermine"
    assert result["cause_demarrage"]["cause"] == "conflit_versions_dependances"
    assert result["fault"] == "indetermine"
    assert not any(r["id"] == "reconstruction_datee" for r in result["reparations"])


def test_pas_de_reconstruction_sans_service_construit(tmp_path, monkeypatch):
    runner, lances = runner_467094(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_images_des_services_construits", lambda: {})

    result = runner.run_evaluation()

    assert lances == ["docker-compose.eval.yml"]
    assert result["cause_demarrage"]["imputation"] == "indetermine"
    assert result["reconstruction_datee"]["resultat"] == "echec"
    assert "aucun service construit" in result["reconstruction_datee"]["extrait"]


def test_pas_de_reconstruction_pour_un_plantage(tmp_path, monkeypatch):
    runner, lances = runner_467094(tmp_path, monkeypatch)
    plantage = "Traceback (most recent call last):\nNameError: name 'x' is not defined\n"
    monkeypatch.setattr(runner, "_conteneurs_du_projet",
                        lambda: [Conteneur(etat_unhealthy(), plantage.encode())])

    result = runner.run_evaluation()

    assert lances == ["docker-compose.eval.yml"]
    assert "reconstruction_datee" not in result
    assert result["cause_demarrage"]["imputation"] == "copie"


def test_reconstruction_garde_le_scale_de_la_recette(tmp_path, monkeypatch):
    """Banc nginx (461091) : le `--scale` de la recette vaut pour chaque `up`
    qui la remplace, reconstruction datée comprise. Vrai DockerCompose, seul
    `subprocess.run` est enregistré."""
    import testcontainers.compose.compose as tc
    from docker_eval import lancement
    runner, _ = runner_467094(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "DockerCompose", tc.DockerCompose)
    up = ["docker", "compose", "--profile", "monitoring", "up", "-d", "--build", "--scale", "bike-api=2"]
    tel = {"commande": "make", "code": 0, "cause": None, "motif": None, "extrait": "", "up": up}
    monkeypatch.setattr(runner, "_jouer_lancement", lambda: {"tel_que_livre": tel, "up": lancement.traduire_up(up)})
    ups = []

    def run(cmd, **kwargs):
        if "up" in cmd:
            ups.append(list(cmd))
            if "docker-compose.datee.yml" not in cmd:
                raise subprocess.CalledProcessError(
                    1, cmd, b"", b"dependency failed to start: container bike-api-1 is unhealthy\n")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    monkeypatch.setattr(tc, "subprocess_run", run)

    result = runner.run_evaluation()

    assert result["reconstruction_datee"]["resultat"] == "saine"
    assert [l[l.index("-f") + 1] for l in ups] == [
        "docker-compose.eval.yml", "docker-compose.eval.yml", "docker-compose.datee.yml"]
    assert all(l[l.index("up") + 1:][:2] == ["--scale", "bike-api=2"] for l in ups)
    assert all(l[l.index("--profile") + 1] == "monitoring" for l in ups)
