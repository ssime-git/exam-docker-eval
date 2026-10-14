"""Crochet d'agent avant démontage (proto « agent unique », scriptorium).

Si EXAM_AGENT_HOOK est posé, le moteur passe la main à cette commande juste
avant de démonter la stack : les conteneurs tournent encore, l'agent peut y
exécuter des commandes, relire les logs, sonder, modifier puis resonder.

Ce que le moteur garantit autour de l'agent :
- le démontage a lieu après, quoi qu'il arrive (délai, plantage de l'agent) ;
- le rendu sur l'hôte est restauré s'il a changé : les scorers relisent les
  fichiers de la copie, un correctif d'essai écrit à travers un volume monté
  ne doit pas bouger la note ;
- rien de l'agent n'entre dans `steps` : la note ne lit que le travail du
  moteur. Le journal de l'agent vit à part, dans EXAM_AGENT_DIR.

Contrat de la commande : elle reçoit STACK_HANDLE (chemin d'un JSON qui
décrit la stack et les étapes déjà jouées) et EXAM_AGENT_DIR (où écrire).
Délai : EXAM_AGENT_TIMEOUT secondes (900 par défaut).
"""

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time

TIMEOUT_DEFAUT = 900
# Les gros fichiers (images .tar, .bento) ne sont pas copiés : un agent n'a
# aucune raison de les réécrire, et les copier coûterait des minutes.
TAILLE_MAX_INSTANTANE = 50 * 1024 * 1024


def _empreinte(racine: str) -> dict:
    vues = {}
    for courant, dossiers, fichiers in os.walk(racine):
        dossiers.sort()
        for nom in sorted(fichiers):
            chemin = os.path.join(courant, nom)
            try:
                stat = os.lstat(chemin)
            except OSError:
                continue
            vues[os.path.relpath(chemin, racine)] = (stat.st_size, stat.st_mtime_ns)
    return vues


def _instantane(racine: str, cible: str) -> None:
    def ignorer(courant, noms):
        return [
            nom for nom in noms
            if os.path.isfile(os.path.join(courant, nom))
            and not os.path.islink(os.path.join(courant, nom))
            and os.path.getsize(os.path.join(courant, nom)) > TAILLE_MAX_INSTANTANE
        ]
    shutil.copytree(racine, cible, symlinks=True, ignore=ignorer)


def _restaurer(racine: str, instantane: str, avant: dict) -> list:
    apres = _empreinte(racine)
    changes = sorted(
        chemin for chemin in set(avant) | set(apres)
        if avant.get(chemin) != apres.get(chemin)
    )
    for chemin in changes:
        cible = os.path.join(racine, chemin)
        source = os.path.join(instantane, chemin)
        if os.path.lexists(cible) and chemin not in avant:
            os.remove(cible)
        elif os.path.lexists(source):
            os.makedirs(os.path.dirname(cible), exist_ok=True)
            if os.path.lexists(cible):
                os.remove(cible)
            shutil.copy2(source, cible, follow_symlinks=False)
    return changes


def _decrire(nom: str) -> dict:
    sortie = subprocess.run(
        ["docker", "inspect", nom], capture_output=True, text=True, timeout=30
    )
    if sortie.returncode != 0:
        return {"nom": nom, "erreur": sortie.stderr.strip()[:300]}
    info = json.loads(sortie.stdout)[0]
    return {
        "nom": nom,
        "image": info.get("Config", {}).get("Image"),
        "etat": info.get("State", {}).get("Status"),
        "code_sortie": info.get("State", {}).get("ExitCode"),
        "service": info.get("Config", {}).get("Labels", {}).get("com.docker.compose.service"),
        "reseaux": sorted((info.get("NetworkSettings", {}).get("Networks") or {}).keys()),
        "ports": info.get("NetworkSettings", {}).get("Ports"),
    }


def avant_demontage(runner, conteneurs: list) -> None:
    commande = os.environ.get("EXAM_AGENT_HOOK")
    if not commande or getattr(runner, "_agent_passe", False):
        return
    runner._agent_passe = True
    logger = runner.logger
    dossier = os.environ.get("EXAM_AGENT_DIR") or tempfile.mkdtemp(prefix="exam-agent-")
    os.makedirs(dossier, exist_ok=True)
    delai = int(os.environ.get("EXAM_AGENT_TIMEOUT", TIMEOUT_DEFAUT))

    poignee = {
        "eval_dir": os.path.abspath(runner.eval_dir),
        "runner": type(runner).__name__,
        # Le projet compose de cette copie : l'agent en dérive le sien, pour
        # que deux corrections parallèles ne relancent pas la même stack.
        "projet": getattr(runner, "project_name", None) or f"eval_{runner.student_name}",
        "conteneurs": [_decrire(nom) for nom in conteneurs],
        "steps": runner.steps,
    }
    chemin_poignee = os.path.join(dossier, "stack.json")
    with open(chemin_poignee, "w", encoding="utf-8") as fichier:
        json.dump(poignee, fichier, ensure_ascii=False, indent=2, default=str)

    sauvegarde = tempfile.mkdtemp(prefix="exam-agent-rendu-")
    instantane = os.path.join(sauvegarde, "rendu")
    _instantane(runner.eval_dir, instantane)
    avant = _empreinte(runner.eval_dir)

    logger.info(f"[agent-hook] passe la main : {commande} ({len(conteneurs)} conteneur(s), {delai} s)")
    debut = time.time()
    env = {**os.environ, "STACK_HANDLE": chemin_poignee, "EXAM_AGENT_DIR": dossier}
    try:
        with open(os.path.join(dossier, "agent.log"), "w", encoding="utf-8") as journal:
            code = subprocess.run(
                shlex.split(commande), env=env, stdout=journal, stderr=subprocess.STDOUT,
                timeout=delai,
            ).returncode
        logger.info(f"[agent-hook] agent terminé, code {code}, {time.time() - debut:.0f} s")
    except subprocess.TimeoutExpired:
        logger.warning(f"[agent-hook] agent coupé après {delai} s")
    except Exception as erreur:
        logger.warning(f"[agent-hook] agent en erreur : {erreur}")
    finally:
        # Filet : la stack que l'agent a relancée (outil compose) ne survit pas.
        projet = os.path.join(dossier, "compose_project")
        if os.path.exists(projet):
            nom = open(projet, encoding="utf-8").read().strip()
            subprocess.run(["docker", "compose", "-p", nom, "down", "-v", "--remove-orphans"],
                           capture_output=True, timeout=180)
        try:
            changes = _restaurer(runner.eval_dir, instantane, avant)
            if changes:
                logger.info(f"[agent-hook] rendu restauré : {changes[:10]}")
        finally:
            shutil.rmtree(sauvegarde, ignore_errors=True)
