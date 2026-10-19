#!/usr/bin/env python3
"""Banc de non-régression bentoml, couche moteur (vraies images).

Lance le moteur de ce dépôt (src/docker_eval) sur des copies réelles, compare
les champs clés du rapport à banc/attentes_bentoml.json et affiche un tableau
des écarts. Code de sortie 1 dès qu'une attente « sure » n'est pas tenue.

Rien de l'apprenant ne tourne sur l'hôte : le moteur lui-même s'exécute dans
le conteneur pi-corrector:dev (comme en production), avec la socket Docker de
l'hôte ; ses pytest tournent donc dans ce conteneur, le service dans le sien.

Données lourdes : $BANC_LOURD (défaut /home/ssime/Documents/project/scriptorium-bench)
  archives/<copie>/copie.<ext>   archive Nexus
  extraits/<copie>/              extraction de référence (pi-corrector)
  travail/<copie>/               copie de travail, recréée à chaque passage
  resultats/<horodatage>/<copie>/ eval.json, eval.log, conteneur.log, sortie.txt

  python3 banc/bentoml.py                 # toutes les copies des attentes
  python3 banc/bentoml.py 462649 466697   # quelques copies
  python3 banc/bentoml.py --relire resultats/<horodatage>   # recompare sans relancer
"""
import argparse
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys

ICI = pathlib.Path(__file__).resolve().parent
MOTEUR = ICI.parent
LOURD = pathlib.Path(os.environ.get("BANC_LOURD", "/home/ssime/Documents/project/scriptorium-bench"))
SKILL = pathlib.Path(os.environ.get(
    "BANC_SKILL_BENTOML",
    "/tmp/claude-1000/-home-ssime-Documents-project-scriptorium/a20b29bd-1657-4fcb-a5c9-1c7dd3a251d4/scratchpad/code/bentoml-exam-corrector-skill"))
IMAGE_RUNNER = os.environ.get("BANC_IMAGE_RUNNER", "pi-corrector:dev")
DELAI = int(os.environ.get("BANC_DELAI", "2400"))


def sh(*args, **kw):
    return subprocess.run(list(args), capture_output=True, text=True, **kw)


# --- état Docker, pour ne nettoyer que ce que le passage a créé -------------
def etat_docker():
    images = {}
    for ligne in sh("docker", "images", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}").stdout.split("\n"):
        if ligne.strip():
            ref, ident = ligne.rsplit(" ", 1)
            images.setdefault(ident, set()).add(ref)
    conteneurs = set(sh("docker", "ps", "-aq", "--no-trunc").stdout.split())
    volumes = set(sh("docker", "volume", "ls", "-q").stdout.split())
    return images, conteneurs, volumes


def nettoyer(avant, copie, sortie):
    """Ne retire que ce qui porte la marque du passage : le conteneur du
    moteur (banc-bentoml-<copie>), ceux du service (bentoml_eval_<copie>),
    les images eval_<copie>/… et service_eval:<copie>, et les tags que le tar
    de la copie a chargés. Jamais de prune, rien d'autre : une autre session
    Docker peut tourner en parallèle."""
    images_av, conteneurs_av, _ = avant
    for ligne in sh("docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}").stdout.split("\n"):
        if not ligne.strip():
            continue
        ident, nom = ligne.split(" ", 1)
        if ident not in conteneurs_av and (nom.startswith(f"banc-bentoml-{copie}") or nom.startswith(f"bentoml_eval_{copie}")):
            sh("docker", "rm", "-f", ident)
    tags_copie = set()
    try:
        tags_copie = set(json.loads((sortie / "eval.json").read_text())["result"].get("image_tags") or [])
    except Exception:
        pass
    images, _, _ = etat_docker()
    for ident, refs in images.items():
        nouvelles = refs - images_av.get(ident, set())
        a_nous = {r for r in nouvelles if r.startswith(f"eval_{copie}/") or r == f"service_eval:{copie}" or r in tags_copie}
        if not a_nous:
            continue
        if ident not in images_av and a_nous == nouvelles:
            sh("docker", "rmi", "-f", ident)
        else:
            for ref in a_nous:
                sh("docker", "rmi", ref)


# --- copie de travail -------------------------------------------------------
def copie_de_travail(copie):
    travail = LOURD / "travail" / copie
    if travail.exists():
        sh("chmod", "-R", "u+rwX", str(travail))
        shutil.rmtree(travail)
    extrait = LOURD / "extraits" / copie
    if not extrait.exists():
        raise SystemExit(f"{copie} : {extrait} absent (voir README du dossier lourd : extraction de référence)")
    travail.parent.mkdir(parents=True, exist_ok=True)
    r = sh("cp", "-a", "--reflink=auto", str(extrait), str(travail))
    if r.returncode:
        raise SystemExit(f"{copie} : copie de travail impossible : {r.stderr}")
    return travail


def commande_production(copie, travail):
    """eval-dir et image-tar de la commande de production (_grade_contract.json)."""
    meta = json.loads((ICI / "attentes_bentoml.json").read_text())["copies"][copie]
    eval_dir = travail / meta.get("evalDir", "")
    args = ["--eval-dir", str(eval_dir)]
    if meta.get("imageTar"):
        args += ["--image-tar", str(travail / meta["imageTar"])]
    return args


def conventions_du_skill():
    """Conventions d'examen du frontmatter du skill, servies au moteur comme le
    fait pi-corrector (skills.ts, parseEngineEnv, #49)."""
    texte = (SKILL / "bentoml-exam-corrector" / "SKILL.md").read_text()
    entete = texte.split("---", 2)[1] if texte.startswith("---") else ""
    valeurs = {}
    for ligne in entete.splitlines():
        if ":" in ligne:
            cle, val = ligne.split(":", 1)
            valeurs[cle.strip()] = val.strip()
    table = {"pi_default_service_port": "EXAM_DEFAULT_SERVICE_PORT", "pi_test_packages": "EXAM_TEST_PACKAGES",
             "pi_test_env": "EXAM_TEST_ENV", "pi_stack_mode": "EXAM_STACK_MODE"}
    return {var: valeurs[cle] for cle, var in table.items() if valeurs.get(cle)}


def lancer(copie, sortie):
    travail = copie_de_travail(copie)
    sortie.mkdir(parents=True, exist_ok=True)
    cache = LOURD / "cache-uv"
    maison = LOURD / "maison-runner"
    for d in (cache, maison):
        d.mkdir(parents=True, exist_ok=True)
    gid_docker = os.stat("/var/run/docker.sock").st_gid
    script = SKILL / "bentoml-exam-corrector" / "scripts" / "run_docker_eval.py"
    interieur = [
        "uvx", "--with", "testcontainers", "--with", "docker", "--with", "requests", "--with", "pyyaml",
        "python", str(script), "--exam-type", "bentoml", "--student", copie, "--timeout", "300",
        "--output-log", str(sortie / "eval.log"), "--output-json", str(sortie / "eval.json"),
        "--container-logs", str(sortie / "conteneur.log"),
    ] + commande_production(copie, travail)
    docker = [
        "docker", "run", "--rm", "--name", f"banc-bentoml-{copie}",
        "--user", f"{os.getuid()}:{os.getgid()}", "--group-add", str(gid_docker),
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        # Réseau de l'hôte : le pare-feu de la machine coupe le pont
        # conteneur → port publié sur l'hôte. Le moteur joint le service par
        # localhost, comme lancé sur la VM.
        "--network", "host",
        "-e", "TESTCONTAINERS_HOST_OVERRIDE=localhost",
        "-e", "PI_CORRECTOR_DOCKER_HOST=localhost",
        "-e", f"EXAM_DOCKER_EVAL={MOTEUR / 'src'}",
        "-e", f"UV_CACHE_DIR={cache}", "-e", f"HOME={maison}",
        *[x for var, val in conventions_du_skill().items() for x in ("-e", f"{var}={val}")],
        "-v", f"{LOURD}:{LOURD}", "-v", f"{MOTEUR}:{MOTEUR}:ro", "-v", f"{SKILL}:{SKILL}:ro",
        "-w", str(sortie), "--entrypoint", "bash", IMAGE_RUNNER, "-lc",
        " ".join(_q(a) for a in interieur),
    ]
    avant = etat_docker()
    fichiers_avant = _fichiers(travail)
    debut = datetime.datetime.now()
    try:
        r = subprocess.run(docker, capture_output=True, text=True, timeout=DELAI)
        code, sortie_texte = r.returncode, r.stdout + "\n--- stderr ---\n" + r.stderr
    except subprocess.TimeoutExpired as e:
        sh("docker", "rm", "-f", f"banc-bentoml-{copie}")
        code, sortie_texte = "délai", f"délai de {DELAI} s dépassé\n{e.stdout or ''}\n{e.stderr or ''}"
    finally:
        nettoyer(avant, copie, sortie)
    (sortie / "sortie.txt").write_text(sortie_texte)
    # Ce que le passage a laissé dans la copie, hors artefacts déclarés du
    # correcteur : un __pycache__ ou un .pytest_cache passerait pour livré.
    crees = sorted(f for f in _fichiers(travail) - fichiers_avant
                   if not any(seg in ARTEFACTS_CORRECTEUR or seg.startswith(".bento_extracted_") for seg in f.split("/")))
    (sortie / "lancement.json").write_text(json.dumps({
        "copie": copie, "code": code, "duree_s": (datetime.datetime.now() - debut).seconds,
        "moteur_sha": sh("git", "-C", str(MOTEUR), "rev-parse", "HEAD").stdout.strip(),
        "moteur_modifie": bool(sh("git", "-C", str(MOTEUR), "status", "--porcelain", "src").stdout.strip()),
        "skill_sha": sh("git", "-C", str(SKILL), "rev-parse", "HEAD").stdout.strip(),
        "conventions": conventions_du_skill(),
        "fichiers_crees": crees[:50],
        "moteur_importe": next((l for l in sortie_texte.split("\n") if "docker_eval" in l and "src" in l), None),
    }, indent=2))
    sh("chmod", "-R", "u+rwX", str(travail))
    shutil.rmtree(travail, ignore_errors=True)
    return code


ARTEFACTS_CORRECTEUR = {"pytest_report.xml", "test_results.log", ".pi-evidence", ".bentoml_home"}


def _fichiers(racine):
    sortie = set()
    for dossier, sous, fichiers in os.walk(racine):
        rel = os.path.relpath(dossier, racine)
        for nom in fichiers + [d + "/" for d in sous]:
            sortie.add(nom if rel == "." else f"{rel}/{nom}")
    return sortie


def _q(a):
    return "'" + str(a).replace("'", "'\\''") + "'"


# --- champs clés ------------------------------------------------------------
def champs(rapport):
    r = (rapport or {}).get("result", {}) or {}
    tr = r.get("test_results") or {}
    pr = r.get("pytest_results") or {}
    tql = pr.get("pytest_tel_que_livre") or {}
    sec = tr.get("securite") or {}
    con = tr.get("contrat") or {}
    idc = tr.get("identifiants_dans_la_copie", r.get("identifiants_dans_la_copie"))
    return {
        "image_loaded": r.get("image_loaded"),
        "image_construite_par_le_correcteur": r.get("image_construite_par_le_correcteur"),
        "image_tags": r.get("image_tags"),
        "container_started": r.get("container_started"),
        "api_ready": r.get("api_ready"),
        "fault": r.get("fault"),
        "login_passed": tr.get("login_passed"),
        "predict_passed": tr.get("predict_passed"),
        "corps_source": tr.get("corps_source"),
        "corps_enveloppe": tr.get("corps_enveloppe"),
        "mecanisme": sec.get("mecanisme", tr.get("mecanisme_acces")),
        "securite.predict_sans_token": sec.get("predict_sans_token"),
        "securite.predict_token_falsifie": sec.get("predict_token_falsifie"),
        "securite.login_mauvais_identifiants": sec.get("login_mauvais_identifiants"),
        "securite.refus_dans_un_200": sec.get("refus_dans_un_200"),
        "contrat.prediction": con.get("prediction"),
        "contrat.predict_entree_invalide": con.get("predict_entree_invalide"),
        "identifiants_dans_la_copie": idc,
        "credentials_source": r.get("credentials_source"),
        "pytest.executed": pr.get("executed"),
        "pytest.evaluable": pr.get("evaluable"),
        "pytest.passed": pr.get("passed"),
        "pytest.total": pr.get("total"),
        "pytest.requirements_inutilisable": pr.get("requirements_inutilisable"),
        "pytest_tel_que_livre.passe": tql.get("passe"),
        "pytest_tel_que_livre.passed": tql.get("passed"),
        "pytest_tel_que_livre.total": tql.get("total"),
        "pytest_tel_que_livre.imputable": tql.get("imputable"),
        "pytest_tel_que_livre.cause": tql.get("cause"),
        "pytest_tel_que_livre.variables_documentees": tql.get("variables_documentees"),
        "reparations": [x.get("id") for x in pr.get("reparations") or []] if "pytest_results" in r else None,
        # Champs demandés par le responsable le 18/10, absents du moteur d'aujourd'hui.
        "image_absente": r.get("image_absente"),
        "instruction_recuperation": r.get("instruction_recuperation", "<absent>"),
        "image_source": r.get("image_source"),
        "readme_image_citee": r.get("readme_image_citee"),
        "rapport_present": bool(rapport),
    }


def tenue(attente, valeur):
    op = attente.get("op", "egal")
    v = attente.get("attendu")
    if op == "egal":
        return valeur == v
    if op == "dans":
        return valeur in v
    if op == "entre":
        return isinstance(valeur, (int, float)) and not isinstance(valeur, bool) and v[0] <= valeur <= v[1]
    if op == "contient":
        return isinstance(valeur, (list, str)) and all(x in valeur for x in (v if isinstance(v, list) else [v]))
    if op == "ne_contient_pas":
        return not (isinstance(valeur, (list, str)) and any(x in valeur for x in (v if isinstance(v, list) else [v])))
    if op == "non_vide":
        return bool(valeur)
    if op == "vide":
        return not valeur
    raise ValueError(op)


def comparer(attentes, resultats_dir, copies):
    lignes, ecarts_surs = [], 0
    for copie in copies:
        chemin = resultats_dir / copie / "eval.json"
        rapport = json.loads(chemin.read_text()) if chemin.exists() else None
        valeurs = champs(rapport)
        lancement = resultats_dir / copie / "lancement.json"
        meta = json.loads(lancement.read_text()) if lancement.exists() else {}
        valeurs["fichiers_crees"] = meta.get("fichiers_crees", "<non relevé>")
        for a in attentes["copies"][copie]["attentes"]:
            v = valeurs.get(a["champ"])
            ok = tenue(a, v)
            if not ok and a["confiance"] == "sure":
                ecarts_surs += 1
            lignes.append((copie, a["champ"], a.get("op", "egal"), a.get("attendu"), v, ok, a["confiance"], a.get("pourquoi", "")))
    return lignes, ecarts_surs


def tableau(lignes, tout=False):
    print(f"{'copie':7} {'champ':38} {'confiance':11} {'attendu':28} {'obtenu':28} état")
    for copie, champ, op, att, obt, ok, conf, _ in lignes:
        if ok and not tout:
            continue
        a = (op + " " if op != "egal" else "") + json.dumps(att, ensure_ascii=False)
        print(f"{copie:7} {champ:38} {conf:11} {a[:28]:28} {json.dumps(obt, ensure_ascii=False)[:28]:28} {'ok' if ok else 'ÉCART'}")
    n = len(lignes)
    e = [l for l in lignes if not l[5]]
    print(f"\n{n} attentes, {n - len(e)} tenues, {len(e)} écarts : "
          + ", ".join(f"{k} {sum(1 for l in e if l[6] == k)}" for k in ("sure", "a_confirmer", "ecart_connu")))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("copies", nargs="*")
    p.add_argument("--relire", help="dossier resultats/<horodatage> à recomparer sans relancer")
    p.add_argument("--tout", action="store_true", help="afficher aussi les attentes tenues")
    p.add_argument("--json", help="écrire les lignes du tableau dans ce fichier")
    args = p.parse_args()
    attentes = json.loads((ICI / "attentes_bentoml.json").read_text())
    copies = args.copies or list(attentes["copies"])
    if args.relire:
        resultats = pathlib.Path(args.relire)
        if not resultats.is_absolute():
            resultats = LOURD / resultats
    else:
        resultats = LOURD / "resultats" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        for copie in copies:
            print(f"[banc] {copie} …", flush=True)
            code = lancer(copie, resultats / copie)
            print(f"[banc] {copie} : code {code}", flush=True)
        print(f"[banc] résultats dans {resultats}")
    lignes, ecarts_surs = comparer(attentes, resultats, copies)
    tableau(lignes, args.tout)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(lignes, ensure_ascii=False, indent=1))
    sys.exit(1 if ecarts_surs else 0)


if __name__ == "__main__":
    main()
