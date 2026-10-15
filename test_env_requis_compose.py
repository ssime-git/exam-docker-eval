"""Une copie qui ne livre pas ses secrets n'est pas une copie cassée
(scriptorium, copie 469217).

469217 (nginx) fait ce qu'il faut : `.env` est dans `.gitignore`, son Makefile
le génère (`GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 12)`) et `make
start-project` l'appelle, comme le dit son README. Le moteur lançait compose
directement : « required variable GRAFANA_ADMIN_PASSWORD is missing a value »,
stack à terre, tous les critères de comportement à 0 — la faute du harnais.

Règle : on n'exécute pas le Makefile de l'apprenant sur l'hôte. Si `.env`
manque, le harnais fournit les variables que le compose exige sans défaut :
l'exemple livré s'il existe (complété), sinon des valeurs aléatoires. L'étape
consignée nomme les variables, jamais les valeurs.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from docker_eval import compose_runner
from docker_eval.compose_runner import ComposeRunner

# La ligne exacte de 469217 : message avec espaces, accents, parenthèses.
COMPOSE_469217 = """services:
  grafana:
    image: grafana/grafana
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD:?définir GRAFANA_ADMIN_PASSWORD dans .env (make env)}
"""


def ecrire(chemin, contenu=""):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)


def lire(chemin):
    with open(chemin, encoding="utf-8") as f:
        return f.read()


def env_lu(chemin):
    valeurs = {}
    for ligne in lire(chemin).splitlines():
        if "=" in ligne and not ligne.lstrip().startswith("#"):
            nom, _, valeur = ligne.partition("=")
            valeurs[nom.strip()] = valeur.strip()
    return valeurs


# --- l'analyse du compose ----------------------------------------------------

def test_variable_sans_defaut_est_requise():
    from docker_eval.compose_runner import variables_requises
    assert variables_requises(COMPOSE_469217) == ["GRAFANA_ADMIN_PASSWORD"]


def test_formes_requises_et_formes_avec_defaut():
    from docker_eval.compose_runner import variables_requises
    texte = """services:
  api:
    image: "app:${TAG:-latest}"
    command: "run $$HOME ${OPT-x} ${PLUS:+y} ${PLUS2+z}"
    environment:
      A: ${A}
      B: $B
      C: "${C?manque C}"
      D: "${D:?}"
      E: "${A}"
"""
    assert variables_requises(texte) == ["A", "B", "C", "D"]


def test_echappement_dollar_dollar_n_est_pas_une_variable():
    from docker_eval.compose_runner import variables_requises
    texte = "services:\n  a:\n    command: sh -c 'echo $${SECRET} $$OTHER'\n"
    assert variables_requises(texte) == []


def test_un_commentaire_n_exige_rien():
    from docker_eval.compose_runner import variables_requises
    texte = "services:\n  a:\n    image: alpine  # ${COMMENTE}\n"
    assert variables_requises(texte) == []


# --- la préparation dans le runner -------------------------------------------

class _Arret(Exception):
    pass


def _runner(dossier, monkeypatch):
    def compose_factice(**kwargs):
        # Arrête l'exécution au moment où compose démarrerait : on observe ce
        # que le harnais a préparé.
        raise _Arret("arrêt du contrôle avant compose up")
    monkeypatch.setattr(compose_runner, "DockerCompose", compose_factice)
    return ComposeRunner("469217", str(dossier), timeout=60,
                         logger=logging.getLogger("test_env_requis_compose"))


def _etape(runner, titre="Variables d'environnement de la copie"):
    return next((e for e in runner.steps if e["title"] == titre), None)


def test_469217_le_harnais_fournit_le_secret(tmp_path, monkeypatch):
    ecrire(tmp_path / "docker-compose.yml", COMPOSE_469217)
    runner = _runner(tmp_path, monkeypatch)
    runner.run_evaluation()
    env = tmp_path / ".env"
    assert env.exists(), "aucun .env préparé : compose refusera de démarrer"
    valeur = env_lu(env)["GRAFANA_ADMIN_PASSWORD"]
    assert len(valeur) == 24
    etape = _etape(runner)
    assert etape is not None and etape["exit_code"] == 0
    assert "GRAFANA_ADMIN_PASSWORD" in etape["output"]
    assert "généré par le harnais" in etape["output"]
    assert "bonne pratique" in etape["output"]
    # Jamais la valeur, nulle part dans la trace.
    assert valeur not in str(runner.steps)


def test_le_env_va_dans_le_dossier_du_projet(tmp_path, monkeypatch):
    """Rendu emballé : compose lit `.env` dans le dossier du projet."""
    ecrire(tmp_path / "projet" / "docker-compose.yml", COMPOSE_469217)
    runner = _runner(tmp_path, monkeypatch)
    runner.run_evaluation()
    assert (tmp_path / "projet" / ".env").exists()
    assert not (tmp_path / ".env").exists()


def test_l_exemple_livre_est_copie_puis_complete(tmp_path, monkeypatch):
    compose = COMPOSE_469217 + "      - X=${API_KEY}\n      - Y=${DB_PASSWORD}\n"
    ecrire(tmp_path / "docker-compose.yml", compose)
    ecrire(tmp_path / ".env.example",
           "# exemple\nAPI_KEY=cle-de-demo\nexport DB_PASSWORD=\nAUTRE=garde\n")
    runner = _runner(tmp_path, monkeypatch)
    runner.run_evaluation()
    texte = lire(tmp_path / ".env")
    env = env_lu(tmp_path / ".env")
    assert texte.startswith("# exemple\n")
    assert env["API_KEY"] == "cle-de-demo"
    assert env["AUTRE"] == "garde"
    assert len(env["export DB_PASSWORD"]) == 24  # complétée sur place
    assert texte.count("DB_PASSWORD") == 1
    assert len(env["GRAFANA_ADMIN_PASSWORD"]) == 24  # absente : ajoutée
    sortie = _etape(runner)["output"]
    assert ".env.example" in sortie and "exemple livré" in sortie
    assert "DB_PASSWORD" in sortie and "GRAFANA_ADMIN_PASSWORD" in sortie
    for valeur in (env["export DB_PASSWORD"], env["GRAFANA_ADMIN_PASSWORD"], "cle-de-demo"):
        assert valeur not in str(runner.steps)


def test_un_env_present_n_est_pas_touche(tmp_path, monkeypatch):
    ecrire(tmp_path / "docker-compose.yml", COMPOSE_469217)
    ecrire(tmp_path / ".env", "GRAFANA_ADMIN_PASSWORD=celui-de-l-apprenant\n")
    runner = _runner(tmp_path, monkeypatch)
    runner.run_evaluation()
    assert lire(tmp_path / ".env") == "GRAFANA_ADMIN_PASSWORD=celui-de-l-apprenant\n"
    assert _etape(runner) is None


def test_sans_variable_requise_rien_ne_change(tmp_path, monkeypatch):
    ecrire(tmp_path / "docker-compose.yml",
           "services:\n  api:\n    image: \"app:${TAG:-latest}\"\n")
    runner = _runner(tmp_path, monkeypatch)
    runner.run_evaluation()
    assert not (tmp_path / ".env").exists()
    assert _etape(runner) is None
