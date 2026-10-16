"""Le lancement de l'énoncé, joué tel que livré (scriptorium, politique du 16/10).

« On note ce qui a été livré, on mesure le reste avec la réparation. » Pour
nginx, l'énoncé fait lancer le projet par `make start-project` ; pour
prometheus-grafana, par « une simple commande `make` ». Le moteur lançait
`docker compose up --build` directement et fabriquait le `.env` manquant :
469217 génère son `.env` par `make env`, appelé par `make start-project`, et
la question « la copie se lance-t-elle avec sa propre commande ? » n'était
jamais posée.

Le passage : la vraie commande, jouée par `make` dans le conteneur jetable de
`make test` (alpine, sans socket Docker, copie par archive), avec un `docker`
factice en tête du PATH. Les prérequis (certificats, `make env`) tournent
pour de vrai ; le `docker compose up` que la recette atteint est enregistré
puis rendu au moteur, qui le joue lui-même (projet, ports relâchés, ménage).
Jamais de socket Docker dans un conteneur qui exécute du code d'apprenant.
"""
import io
import json
import logging
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import compose_runner, lancement
from docker_eval.compose_runner import ComposeRunner

import test_sondes_nginx as tsn


def ecrire(chemin, contenu=""):
    os.makedirs(os.path.dirname(str(chemin)), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def lire(chemin):
    with open(chemin, encoding="utf-8") as f:
        return f.read()


# --- C5 : les identifiants de la copie d'abord ---------------------------------

def test_les_identifiants_de_la_copie_passent_avant_ceux_du_cours(tmp_path, monkeypatch):
    runner, _ = tsn.runner_469145(tmp_path, monkeypatch)
    ecrire(tmp_path / "tests" / "run_tests.sh", tsn.RUN_TESTS.replace("admin:admin", "alice:secret"))
    candidats = runner._identifiants_candidats(runner._requete_predict())
    assert [c for c, _, _ in candidats] == ["alice:secret", "admin:admin"]
    assert candidats[0][1] == "copie" and candidats[-1][1] == "cours"


def test_admin_admin_ecrit_dans_la_copie_vient_de_la_copie(tmp_path, monkeypatch):
    # 469145 et 469217 écrivent `--user admin:admin` / `-u admin:admin` : c'est
    # leur couple, pas « celui du cours ». L'ancien code l'excluait de la liste
    # de la copie parce qu'il valait le couple du cours.
    runner, _ = tsn.runner_469145(tmp_path, monkeypatch)
    comportement = runner._sonder_comportement_nginx(tsn.SONDES_RACINE)
    assert comportement["identifiants_acceptes"] is True
    assert comportement["identifiants_source"] == "copie"


def test_admin_admin_absent_de_la_copie_vient_du_cours(tmp_path, monkeypatch):
    runner, _ = tsn.runner_469145(tmp_path, monkeypatch)
    ecrire(tmp_path / "tests" / "run_tests.sh", tsn.RUN_TESTS.replace(" --user admin:admin", ""))
    comportement = runner._sonder_comportement_nginx(tsn.SONDES_RACINE)
    assert comportement["identifiants_acceptes"] is True
    assert comportement["identifiants_source"] == "cours"
    etape = next(s for s in runner.steps if s["title"] == "Sondes de comportement nginx")
    assert "identifiants du cours" in etape["output"]


def test_le_couple_du_cours_cite_dans_le_readme_vient_de_la_copie(tmp_path, monkeypatch):
    runner, _ = tsn.runner_469145(tmp_path, monkeypatch)
    ecrire(tmp_path / "tests" / "run_tests.sh", tsn.RUN_TESTS.replace(" --user admin:admin", ""))
    ecrire(tmp_path / "README.md", "## Accès\n\nIdentifiants de l'API : `admin` / mot de passe `admin`.\n")
    comportement = runner._sonder_comportement_nginx(tsn.SONDES_RACINE)
    assert comportement["identifiants_source"] == "copie"


def test_aucun_couple_accepte_pas_de_source(tmp_path, monkeypatch):
    import base64
    autre = "Basic " + base64.b64encode(b"bob:inconnu").decode()
    runner, _ = tsn.runner_469145(tmp_path, monkeypatch, identifiants=autre)
    comportement = runner._sonder_comportement_nginx(tsn.SONDES_RACINE)
    assert comportement["identifiants_acceptes"] is False
    assert comportement["identifiants_source"] is None


# --- la commande de l'énoncé ---------------------------------------------------

def test_la_commande_de_lancement_suit_l_enonce(tmp_path):
    assert lancement.commande_de_lancement(str(tmp_path)) is None
    ecrire(tmp_path / "prometheus" / "prometheus.yml", "scrape_configs: []\n")
    assert lancement.commande_de_lancement(str(tmp_path)) == "make"
    ecrire(tmp_path / "deployments" / "nginx" / "nginx.conf", "events {}\n")
    assert lancement.commande_de_lancement(str(tmp_path)) == "make start-project"


# --- l'enregistrement du docker factice ----------------------------------------

SEP, FIN = "\x1f", "\x1e\n"


def appel(*argv):
    return SEP.join(argv) + FIN


def test_le_docker_factice_ne_parle_a_aucun_demon():
    shim = lancement.SHIM_DOCKER
    assert "docker.sock" not in shim and "DOCKER_HOST" not in shim
    # `compose up` rend la main au moteur : make s'arrête là.
    assert f"exit {lancement.CODE_UP}" in shim and f"exit {lancement.CODE_NON_REPRODUIT}" in shim


def test_les_appels_enregistres_se_relisent():
    texte = appel("docker", "compose", "down") + appel("docker", "compose", "up", "-d", "--build")
    assert lancement.lire_appels(texte) == [["docker", "compose", "down"],
                                             ["docker", "compose", "up", "-d", "--build"]]


def test_up_traduit_en_arguments_du_moteur():
    assert lancement.traduire_up(["docker", "compose", "up", "-d", "--build"]) == {
        "fichiers": [], "profils": [], "env_files": [], "services": []}
    assert lancement.traduire_up(["docker-compose", "-f", "a.yml", "--file=b.yml", "--profile", "mon",
                                  "--env-file", ".env.prod", "-p", "autre", "up", "-d", "nginx", "api"]) == {
        "fichiers": ["a.yml", "b.yml"], "profils": ["mon"], "env_files": [".env.prod"],
        "services": ["nginx", "api"]}
    # Une option que le moteur ne sait pas rejouer : pas de traduction approximative.
    assert lancement.traduire_up(["docker", "compose", "--project-directory", "ailleurs", "up"]) is None
    assert lancement.traduire_up(["docker", "compose", "up", "--scale", "api=3"]) is None


def test_lancement_atteint_le_up():
    appels = [["docker", "compose", "down"], ["docker", "compose", "up", "-d", "--build"]]
    tel = lancement.classer("make start-project", 2, "openssl req ...\nmake: *** [Makefile:12: start-project] Error 97\n",
                            appels)
    assert tel["code"] == 0 and tel["cause"] is None
    assert tel["up"] == ["docker", "compose", "up", "-d", "--build"]
    assert "docker compose up -d --build" in tel["extrait"]


def test_un_prerequis_en_echec_avant_le_up_est_la_copie():
    sortie = "bash: scripts/gen_certs.sh: No such file or directory\nmake: *** [Makefile:8: certs] Error 1\n"
    tel = lancement.classer("make start-project", 2, sortie, [])
    assert tel["code"] == 2 and tel["cause"] == "copie" and tel["motif"] == "prerequis_en_echec"
    assert "gen_certs.sh" in tel["extrait"]


def test_cible_absente_et_make_sans_up():
    tel = lancement.classer("make start-project", 2, "make: *** No rule to make target 'start-project'.  Stop.\n", [])
    assert tel["cause"] == "copie" and tel["motif"] == "cible_absente"
    # `make` dont la cible par défaut est `help` : rien n'est lancé.
    tel = lancement.classer("make", 0, "  start   Démarre la stack\n", [])
    assert tel["code"] == 0 and tel["cause"] == "copie" and tel["motif"] == "ne_lance_pas_la_stack"


def test_ce_que_notre_outillage_empeche_n_est_pas_un_zero():
    # Outil absent de notre image, commande docker non rejouable, délai.
    tel = lancement.classer("make start-project", 2, "python3: not found\nmake: *** [Makefile:3: env] Error 127\n", [])
    assert tel["code"] is None and tel["cause"] == "non_mesure" and tel["motif"] == "outil_absent"
    tel = lancement.classer("make start-project", 2, "make: *** [Makefile:5: certs] Error 98\n",
                            [["docker", "run", "--rm", "alpine/openssl"]])
    assert tel["code"] is None and tel["cause"] == "non_mesure" and tel["motif"] == "commande_docker_non_reproduite"
    tel = lancement.classer("make start-project", 124, "", [])
    assert tel["code"] is None and tel["motif"] == "delai"


def test_les_secrets_sont_masques():
    texte = "GRAFANA_ADMIN_PASSWORD=8f3a9c\nexport API_TOKEN=abc\nDB_SECRET: zz\nnormal=1\n"
    masque = lancement.masquer_secrets(texte)
    for valeur in ("8f3a9c", "abc", "zz"):
        assert valeur not in masque
    assert "normal=1" in masque and "GRAFANA_ADMIN_PASSWORD=***" in masque


# --- le rapatriement des fichiers produits par la recette ----------------------

def tar_de(fichiers: dict, liens: dict | None = None) -> bytes:
    tampon = io.BytesIO()
    with tarfile.open(fileobj=tampon, mode="w") as archive:
        for nom, contenu in fichiers.items():
            donnees = contenu.encode() if isinstance(contenu, str) else contenu
            info = tarfile.TarInfo(nom)
            info.size = len(donnees)
            archive.addfile(info, io.BytesIO(donnees))
        for nom, cible in (liens or {}).items():
            info = tarfile.TarInfo(nom)
            info.type = tarfile.SYMTYPE
            info.linkname = cible
            archive.addfile(info)
    return tampon.getvalue()


def test_seuls_les_fichiers_reguliers_produits_reviennent(tmp_path):
    ecrire(tmp_path / "Makefile", "start-project:\n")
    ecrire(tmp_path / "certs" / "nginx.crt", "ancien")
    donnees = tar_de({
        "travail/Makefile": "start-project:\n",           # inchangé
        "travail/.env": "GRAFANA_ADMIN_PASSWORD=x\n",     # nouveau
        "travail/certs/nginx.crt": "nouveau",             # modifié
        "travail/../evade": "non",                        # hors du projet
        "travail/gros.bin": b"0" * (lancement.TAILLE_MAX_FICHIER + 1),
    }, liens={"travail/lien": "/etc/passwd"})
    nouveaux, originaux = lancement.rapatrier(donnees, str(tmp_path))
    assert sorted(os.path.relpath(c, tmp_path) for c in nouveaux) == [".env"]
    assert list(originaux) == [str(tmp_path / "certs" / "nginx.crt")]
    assert originaux[str(tmp_path / "certs" / "nginx.crt")] == b"ancien"
    assert lire(tmp_path / "certs" / "nginx.crt") == "nouveau"
    assert not (tmp_path / "lien").exists() and not (tmp_path.parent / "evade").exists()
    assert not (tmp_path / "gros.bin").exists()


# --- le conteneur jetable ------------------------------------------------------

MAKEFILE_469217 = (
    "start-project: certs env\n\tdocker compose up -d --build\n\t@$(MAKE) wait\n"
    "env: .env\n.env:\n\t@echo \"GRAFANA_ADMIN_PASSWORD=$$(openssl rand -hex 12)\" > .env\n"
    "certs:\n\t@true\n")


class DemonLancement(tsn.FauxDemon):
    """FauxDemon + get_archive : /travail après la recette, et l'enregistrement."""

    def __init__(self, code=2, sortie=b"make: *** [Makefile:2: start-project] Error 97\n",
                 travail=None, appels=""):
        super().__init__(lambda timeout: {"StatusCode": code})
        demon = self
        self.archives = []
        travail = travail if travail is not None else {"travail/.env": "GRAFANA_ADMIN_PASSWORD=abc\n"}
        Base = type(self.containers.create("x"))

        class Conteneur(Base):
            def put_archive(self, chemin, donnees):
                demon.archives.append((chemin, donnees))
                return True

            def logs(self):
                return sortie

            def get_archive(self, chemin):
                if chemin == "/travail":
                    return iter([tar_de(travail)]), {}
                if chemin == lancement.FICHIER_APPELS:
                    return iter([tar_de({"appels": appels})]), {}
                raise RuntimeError(f"pas de {chemin}")

        class Conteneurs:
            def create(self, image, **kwargs):
                demon.cree = dict(kwargs, image=image)
                return Conteneur()

        self.containers = Conteneurs()
        self.cree = None


def runner_lancement(tmp_path, monkeypatch, demon, makefile=MAKEFILE_469217):
    ecrire(tmp_path / "docker-compose.yml", tsn.COMPOSE_469145)
    ecrire(tmp_path / "deployments" / "nginx" / "nginx.conf", tsn.NGINX)
    if makefile is not None:
        ecrire(tmp_path / "Makefile", makefile)
    runner = ComposeRunner("469217", str(tmp_path), timeout=60, logger=logging.getLogger("t"))
    monkeypatch.setattr(compose_runner, "client_docker", lambda: demon)
    return runner


def test_le_lancement_tourne_dans_le_conteneur_jetable_sans_le_demon(tmp_path, monkeypatch):
    demon = DemonLancement(appels=appel("docker", "compose", "up", "-d", "--build"))
    runner = runner_lancement(tmp_path, monkeypatch, demon)
    passage = runner._jouer_lancement()
    cree = demon.cree
    assert not cree.get("volumes") and "docker.sock" not in json.dumps(cree)
    assert cree.get("privileged") in (None, False)
    assert cree.get("network_mode") != "host"
    script = cree["command"][-1]
    assert f"timeout {lancement.DELAI_LANCEMENT_S} make start-project" in script
    # Le docker factice est posé par archive, en tête du PATH.
    noms = set()
    for _, donnees in demon.archives:
        noms |= set(tarfile.open(fileobj=io.BytesIO(donnees)).getnames())
    assert {"usr/local/bin/docker", "usr/local/bin/docker-compose", "travail/Makefile"} <= noms
    assert demon.retire is True
    tel = passage["tel_que_livre"]
    assert tel["code"] == 0 and tel["cause"] is None and tel["commande"] == "make start-project"
    # Le .env produit par `make env` revient dans le projet ; jamais sa valeur dans la trace.
    assert lire(tmp_path / ".env") == "GRAFANA_ADMIN_PASSWORD=abc\n"
    assert "abc" not in json.dumps(runner.steps)
    runner._retirer_rapatries()
    assert not (tmp_path / ".env").exists()


def test_sans_makefile_le_lancement_echoue_sans_conteneur(tmp_path, monkeypatch):
    demon = DemonLancement()
    runner = runner_lancement(tmp_path, monkeypatch, demon, makefile=None)
    tel = runner._jouer_lancement()["tel_que_livre"]
    assert tel["cause"] == "copie" and tel["motif"] == "makefile_absent" and tel["code"] == 2
    assert demon.cree is None


def test_image_indisponible_non_mesure(tmp_path, monkeypatch):
    demon = DemonLancement()

    class ImagesCassees:
        def get(self, tag):
            raise RuntimeError("absente")

        def build(self, **kwargs):
            raise RuntimeError("pull access denied")
    demon.images = ImagesCassees()
    runner = runner_lancement(tmp_path, monkeypatch, demon)
    tel = runner._jouer_lancement()["tel_que_livre"]
    assert tel["code"] is None and tel["cause"] == "non_mesure" and demon.cree is None


# --- dans run_evaluation : lancement tel que livré et réparations ---------------

class _Arret(Exception):
    pass


def _run(tmp_path, monkeypatch, passage, compose_text=None, config=(1, "required variable X is missing a value")):
    """run_evaluation jusqu'au premier `up` : on relève les arguments du moteur."""
    from test_env_requis_compose import COMPOSE_469217
    ecrire(tmp_path / "docker-compose.yml", compose_text or COMPOSE_469217)
    ecrire(tmp_path / "deployments" / "nginx" / "nginx.conf", tsn.NGINX)
    ecrire(tmp_path / "Makefile", MAKEFILE_469217)
    appels = []

    def compose_factice(**kwargs):
        appels.append(kwargs)
        raise _Arret("arrêt avant compose up")
    monkeypatch.setattr(compose_runner, "DockerCompose", compose_factice)
    runner = ComposeRunner("469217", str(tmp_path), timeout=60, logger=logging.getLogger("t"))

    def jouer():
        for nom, contenu in passage.get("produit", {}).items():
            ecrire(tmp_path / nom, contenu)
            runner._fichiers_rapatries.append(str(tmp_path / nom))
        return {"tel_que_livre": dict(passage["tel_que_livre"]), "up": passage.get("up")}
    monkeypatch.setattr(runner, "_jouer_lancement", jouer)
    monkeypatch.setattr(runner, "_config_tel_que_livre", lambda fichiers, env_files: config)
    return runner, runner.run_evaluation(), appels


ATTEINT = {"commande": "make start-project", "code": 0, "cause": None, "motif": None,
           "extrait": "make a atteint docker compose up -d --build",
           "up": ["docker", "compose", "up", "-d", "--build"]}


def test_469217_make_env_produit_le_env_aucune_reparation(tmp_path, monkeypatch):
    passage = {"tel_que_livre": ATTEINT, "up": lancement.traduire_up(ATTEINT["up"]),
               "produit": {".env": "GRAFANA_ADMIN_PASSWORD=de-make-env\n"}}
    runner, resultat, appels = _run(tmp_path, monkeypatch, passage)
    assert resultat["lancement_tel_que_livre"]["code"] == 0
    assert resultat["lancement_tel_que_livre"]["cause"] is None
    assert resultat["reparations"] == []
    # Un seul up, celui que le moteur lançait déjà.
    assert len(appels) == 1 and appels[0]["compose_file_name"] in ("docker-compose.yml", "docker-compose.eval.yml")
    assert not any(e["title"] == "Variables d'environnement de la copie" for e in runner.steps)


def test_make_qui_ne_produit_pas_le_env_coute_le_lancement(tmp_path, monkeypatch):
    passage = {"tel_que_livre": ATTEINT, "up": lancement.traduire_up(ATTEINT["up"])}
    runner, resultat, appels = _run(tmp_path, monkeypatch, passage,
                                    config=(1, "required variable GRAFANA_ADMIN_PASSWORD is missing a value"))
    tel = resultat["lancement_tel_que_livre"]
    assert tel["code"] == 1 and tel["cause"] == "copie" and tel["motif"] == "variables_requises"
    assert "GRAFANA_ADMIN_PASSWORD" in tel["extrait"]
    [reparation] = resultat["reparations"]
    assert reparation["id"] == "env_fabrique" and reparation["critere_touche"] == "services_debout"
    assert reparation["tel_que_livre"]["code"] == 1 and "GRAFANA_ADMIN_PASSWORD" in reparation["applique"]
    assert len(appels) == 1  # le reste est mesuré avec notre .env


def test_make_en_echec_avant_le_up_compose_a_la_place(tmp_path, monkeypatch):
    tel = {"commande": "make start-project", "code": 2, "cause": "copie", "motif": "prerequis_en_echec",
           "extrait": "bash: scripts/gen.sh: No such file or directory", "up": None}
    runner, resultat, appels = _run(tmp_path, monkeypatch, {"tel_que_livre": tel})
    assert resultat["lancement_tel_que_livre"]["code"] == 2
    ids = [r["id"] for r in resultat["reparations"]]
    assert ids == ["compose_au_lieu_de_make", "env_fabrique"]
    compose = resultat["reparations"][0]
    assert compose["tel_que_livre"] == {"commande": "make start-project", "code": 2,
                                        "extrait": "bash: scripts/gen.sh: No such file or directory"}
    assert "docker compose" in compose["applique"]
    assert len(appels) == 1


def test_lancement_non_mesure_n_est_pas_une_reparation_imputee(tmp_path, monkeypatch):
    tel = {"commande": "make start-project", "code": None, "cause": "non_mesure", "motif": "outil_absent",
           "extrait": "python3: not found", "up": None}
    _, resultat, _ = _run(tmp_path, monkeypatch, {"tel_que_livre": tel},
                          compose_text="services:\n  nginx:\n    image: nginx\n")
    assert resultat["lancement_tel_que_livre"]["cause"] == "non_mesure"
    assert [r["id"] for r in resultat["reparations"]] == ["compose_au_lieu_de_make"]


def test_les_fichiers_et_profils_de_la_recette_sont_ceux_du_moteur(tmp_path, monkeypatch):
    ecrire(tmp_path / "docker-compose.prod.yml", "services:\n  nginx:\n    ports:\n      - \"443:443\"\n")
    up = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml",
          "--profile", "monitoring", "up", "-d"]
    passage = {"tel_que_livre": dict(ATTEINT, up=up), "up": lancement.traduire_up(up),
               "produit": {".env": "GRAFANA_ADMIN_PASSWORD=x\n"}}
    _, resultat, appels = _run(tmp_path, monkeypatch, passage)
    fichiers = appels[0]["compose_file_name"]
    assert isinstance(fichiers, list) and len(fichiers) == 2
    # Chaque fichier a ses ports hôte relâchés, y compris la surcharge.
    assert "443:443" not in lire(tmp_path / fichiers[1]) if os.path.exists(tmp_path / fichiers[1]) else True
    assert appels[0]["profiles"] == ["monitoring"]


# --- le docker factice, joué pour de vrai (notre script, pas celui de la copie) -

def test_le_docker_factice_respecte_son_contrat(tmp_path):
    import subprocess
    journal = tmp_path / "appels"
    script = tmp_path / "docker"
    script.write_text(lancement.SHIM_DOCKER.replace(lancement.FICHIER_APPELS, str(journal)))

    def jouer(*args, prog="docker"):
        cible = tmp_path / prog
        if prog != "docker":
            cible.write_text(script.read_text())
        return subprocess.run(["sh", str(cible), *args], capture_output=True, text=True).returncode

    # 467094 : COMPOSE := $(shell docker compose version … && echo "docker compose" || …)
    assert jouer("compose", "version") == 0
    assert jouer("compose", "-p", "mlops", "down") == 0
    assert jouer("compose", "-f", "a.yml", "--profile", "p", "up", "-d", "--build") == lancement.CODE_UP
    assert jouer("up", "-d", prog="docker-compose") == lancement.CODE_UP
    assert jouer("run", "--rm", "-v", "/:/h", "alpine") == lancement.CODE_NON_REPRODUIT
    assert jouer("compose", "exec", "nginx", "nginx", "-t") == lancement.CODE_NON_REPRODUIT
    appels = lancement.lire_appels(journal.read_text())
    assert appels[2] == ["docker", "compose", "-f", "a.yml", "--profile", "p", "up", "-d", "--build"]
    assert appels[3] == ["docker-compose", "up", "-d"]
    assert [lancement.est_up(a) for a in appels] == [False, False, True, True, False, False]
    assert [lancement.est_non_reproduit(a) for a in appels] == [False, False, False, False, True, True]


def test_467094_les_services_de_la_recette_sont_rejoues():
    appels = [["docker", "compose", "version"],
              ["docker", "compose", "up", "--build", "-d", "bike-api", "prometheus", "node-exporter", "grafana"]]
    tel = lancement.classer("make", 2, "make: *** [Makefile:15: all] Error 97\n", appels)
    assert tel["code"] == 0 and tel["cause"] is None
    assert lancement.traduire_up(tel["up"])["services"] == ["bike-api", "prometheus", "node-exporter", "grafana"]
