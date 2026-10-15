"""Ce que les sondes de comportement prometheus-grafana lisent et concluent.

La grille prometheus-grafana du 15/10 (467439, 466056) note une stack qui
fonctionne : Prometheus prêt, cibles api et node-exporter UP, métriques
applicatives exposées par l'API, règles d'alerte réellement chargées, Grafana
provisionné avec une source Prometheus et des tableaux de bord qui
interrogent quelque chose, endpoint de dérive qui fait bouger une jauge. Un
mot-clé dans prometheus.yml ou dans un JSON de tableau de bord ne prouve rien
de tout ça : on demande à la stack.

Ce module garde les lectures et les conclusions pures ; les requêtes passent
par le runner (`ComposeRunner._sonder_comportement_prometheus`).
"""

import math
import os
import re

_DOSSIERS_IGNORES = (".git", ".venv", "venv", "__pycache__", "node_modules", "__MACOSX")
NOMS_PROMETHEUS = ("prometheus.yml", "prometheus.yaml")

# Les champs du contrat lu par le barème, tous à None quand rien n'est mesuré.
CHAMPS = ("prometheus_pret", "grafana_sante", "cibles", "metriques_api", "regles", "grafana", "endpoint_drift")

# Métriques que tout exporteur expose : elles ne prouvent pas qu'une API
# instrumente son propre métier. node-exporter et Prometheus lui-même aussi.
_PREFIXES_GENERIQUES = ("go_", "process_", "python_", "promhttp_", "node_", "prometheus_", "scrape_",
                        "net_conntrack_", "grafana_")


def _parcourir(eval_dir: str):
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = sorted(d for d in dossiers if d not in _DOSSIERS_IGNORES)
        for nom in sorted(fichiers):
            yield racine, nom


def fichier_prometheus_yml(eval_dir: str) -> str | None:
    for racine, nom in _parcourir(eval_dir):
        if nom.lower() in NOMS_PROMETHEUS:
            return os.path.join(racine, nom)
    return None


def est_examen_prometheus(eval_dir: str) -> bool:
    """Le type d'examen n'arrive pas au runner : le skill appelle
    `ComposeRunner` sans lui (run_docker_eval.py). Une copie nginx porte aussi
    un prometheus.yml (exporter, 469145) : le nginx.conf la classe nginx."""
    from .sondes_nginx import fichier_nginx_conf
    return fichier_prometheus_yml(eval_dir) is not None and fichier_nginx_conf(eval_dir) is None


def familles_metriques(texte: str, maxi: int = 200) -> tuple:
    """(noms, types) des familles déclarées par `# TYPE` dans un /metrics."""
    noms, types = [], {}
    for nom, genre in re.findall(r"^#\s*TYPE\s+(\S+)\s+(\S+)", texte or "", re.MULTILINE):
        if nom in types:
            continue
        if len(noms) >= maxi:
            break
        noms.append(nom)
        types[nom] = genre
    return noms, types


def est_metrique_applicative(nom: str) -> bool:
    return not nom.startswith(_PREFIXES_GENERIQUES)


def series_drift(texte: str) -> dict:
    """Chaque série dont le nom de famille dit « drift », avec sa valeur.

    Les copies nomment leur jauge comme elles veulent (467439
    `evidently_drift_share`, 466056 `evidently_drifted_columns_share` et
    `evidently_data_drift_detected_status`) : on les compare toutes."""
    series = {}
    for ligne in (texte or "").splitlines():
        if not ligne or ligne.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_:][A-Za-z0-9_:]*(?:\{[^}]*\})?)\s+(\S+)", ligne)
        if not m or "drift" not in m.group(1).split("{", 1)[0].lower():
            continue
        try:
            series[m.group(1)] = float(m.group(2))
        except ValueError:
            continue
    return series


def series_changees(avant: dict, apres: dict) -> list:
    """Les séries de dérive qui ont bougé ou sont apparues.

    Une jauge Evidently jamais posée vaut NaN (467439, e2e du 15/10) : NaN
    != NaN, une jauge restée NaN passerait pour modifiée."""
    def meme(a, b):
        return a == b or (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b))
    return sorted(s for s, v in apres.items() if s not in avant or not meme(avant[s], v))


def resumer_cibles(donnees: dict) -> dict:
    """/api/v1/targets → comptes et jobs par santé.

    Une cible `unknown` (pas encore scrapée) n'est pas up : la compter nulle
    part ferait mentir up + down sur le nombre de cibles."""
    cibles = donnees["data"]["activeTargets"]
    jobs_up, jobs_down = set(), set()
    up = down = 0
    for cible in cibles:
        job = str((cible.get("labels") or {}).get("job") or cible.get("scrapePool") or "?")
        if cible.get("health") == "up":
            up += 1
            jobs_up.add(job)
        else:
            down += 1
            jobs_down.add(job)
    return {"up": up, "down": down, "jobs_up": sorted(jobs_up), "jobs_down": sorted(jobs_down)}


def cibles_inconnues(donnees: dict) -> list:
    return sorted({str((c.get("labels") or {}).get("job") or "?")
                   for c in donnees["data"]["activeTargets"] if c.get("health") == "unknown"})


def duree_prometheus(secondes) -> str | None:
    """`duration` de /api/v1/rules (secondes) → la notation du `for:`."""
    if not secondes:
        return None
    secondes = int(secondes)
    texte = ""
    for unite, taille in (("h", 3600), ("m", 60), ("s", 1)):
        if secondes >= taille:
            texte += f"{secondes // taille}{unite}"
            secondes %= taille
    return texte


def resumer_regles(donnees: dict) -> dict:
    """/api/v1/rules → règles chargées, alertes, règles en erreur."""
    chargees, erreurs, alertes = 0, 0, []
    for groupe in donnees["data"]["groups"]:
        for regle in groupe.get("rules") or []:
            chargees += 1
            if regle.get("health") == "err":
                erreurs += 1
            if regle.get("type") == "alerting":
                alertes.append({
                    "nom": regle.get("name"),
                    "expr": regle.get("query"),
                    "for": duree_prometheus(regle.get("duration")),
                    "severity": (regle.get("labels") or {}).get("severity"),
                    "annotations": bool(regle.get("annotations")),
                })
    return {"chargees": chargees, "alertes": alertes, "erreurs": erreurs}


def requetes_des_panels(tableau: dict, maxi: int = 100) -> list:
    """Les `expr` des cibles de panneaux d'un /api/dashboards/uid/<uid>.

    Panneaux de premier niveau, lignes repliées qui portent leurs panneaux,
    ancien schéma `rows[].panels` : Grafana a changé de forme plusieurs fois,
    on les parcourt toutes."""
    requetes = []

    def parcourir(panneaux):
        for panneau in panneaux or []:
            if not isinstance(panneau, dict):
                continue
            for cible in panneau.get("targets") or []:
                expr = cible.get("expr") if isinstance(cible, dict) else None
                if isinstance(expr, str) and expr.strip() and expr not in requetes and len(requetes) < maxi:
                    requetes.append(expr)
            parcourir(panneau.get("panels"))

    corps = tableau.get("dashboard", tableau) if isinstance(tableau, dict) else {}
    parcourir(corps.get("panels"))
    for ligne in corps.get("rows") or []:
        if isinstance(ligne, dict):
            parcourir(ligne.get("panels"))
    return requetes


# `curl -X POST $(API_URL)/trigger-drift`, `http://localhost:8080/evaluate` :
# l'URL passe souvent par une variable du Makefile, que `requetes_declarees`
# (localhost en dur) ne voit pas.
_APPEL_DERIVE = re.compile(
    r"(?:https?://[^\s/\"']+|\$\(\w+\)|\$\{\w+\}|\$\w+)(/[\w/-]*?(?:drift|evaluate)[\w-]*)")
_ROUTE_DERIVE = re.compile(
    r"@\w+\.(get|post|put)\(\s*[\"'](/[^\"']*(?:drift|evaluate)[^\"']*)[\"']", re.IGNORECASE)


def _par_preference(candidats: list) -> dict | None:
    """Un endpoint qui dit « drift » d'abord : il déclenche la dérive sans
    corps. /evaluate attend un lot de données que le harnais n'a pas."""
    for candidat in candidats:
        if "drift" in candidat["chemin"].lower():
            return candidat
    return candidats[0] if candidats else None


def endpoint_drift_declare(eval_dir: str) -> dict | None:
    """L'endpoint de dérive que la copie déclare : ses commandes (Makefile,
    scripts, README) d'abord, ses routes ensuite. Rend {chemin, methode,
    source} ou None."""
    declares, routes = [], []
    for racine, nom in _parcourir(eval_dir):
        chemin_fichier = os.path.join(racine, nom)
        source = os.path.relpath(chemin_fichier, eval_dir)
        commande = nom == "Makefile" or nom.endswith((".sh", ".md"))
        if not (commande or nom.endswith(".py")):
            continue
        try:
            with open(chemin_fichier, encoding="utf-8", errors="replace") as lecteur:
                texte = lecteur.read()
        except OSError:
            continue
        if commande:
            for ligne in texte.replace("\\\n", " ").splitlines():
                if "curl" not in ligne:
                    continue
                for m in _APPEL_DERIVE.finditer(ligne):
                    methode = re.search(r"-X\s*(\w+)", ligne)
                    candidat = {"chemin": m.group(1), "methode": methode.group(1).upper() if methode else "POST",
                                "source": source}
                    if candidat["chemin"] not in [c["chemin"] for c in declares]:
                        declares.append(candidat)
        else:
            for m in _ROUTE_DERIVE.finditer(texte):
                candidat = {"chemin": m.group(2), "methode": m.group(1).upper(), "source": source}
                if candidat["chemin"] not in [c["chemin"] for c in routes]:
                    routes.append(candidat)
    return _par_preference(declares) or _par_preference(routes)


def lire_env(chemin: str) -> dict:
    """Un .env lu comme compose le lit (clé=valeur, guillemets retirés)."""
    valeurs = {}
    try:
        with open(chemin, encoding="utf-8", errors="replace") as lecteur:
            for ligne in lecteur:
                m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", ligne)
                if m:
                    valeur = m.group(2)
                    if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "'\"":
                        valeur = valeur[1:-1]
                    valeurs[m.group(1)] = valeur
    except OSError:
        pass
    return valeurs


def resoudre_variable(valeur: str, env: dict) -> str | None:
    """`${VAR}`, `${VAR:-défaut}`, `$VAR` résolus contre un .env ; None si
    une variable reste sans valeur (on garde alors le défaut de l'image)."""
    manque = []

    def remplacer(m):
        if m.group(0) == "$$":
            return "$"
        nom = m.group(1) or m.group(4)
        modif, defaut = m.group(2) or "", m.group(3) or ""
        if env.get(nom):
            return env[nom]
        if modif.lstrip(":").startswith("-"):
            return defaut
        manque.append(nom)
        return ""

    resolu = re.sub(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)",
                    remplacer, str(valeur))
    return None if manque or not resolu else resolu


def _oui_non(valeur, oui="oui", non="non", inconnu="non mesuré") -> str:
    return inconnu if valeur is None else (oui if valeur else non)


def _liste(valeurs, maxi=12) -> str:
    valeurs = list(valeurs)
    texte = ", ".join(valeurs[:maxi])
    return texte + (f" (+{len(valeurs) - maxi})" if len(valeurs) > maxi else "") if valeurs else "aucun"


def resume_comportement(c: dict) -> str:
    """L'étape citable : chaque champ de `prometheus_comportement` en clair,
    noms et comptes seulement (jamais d'identifiants)."""
    lignes = [f"Prometheus prêt (/-/ready 200) : {_oui_non(c.get('prometheus_pret'))}",
              f"Grafana en bonne santé (/api/health 200) : {_oui_non(c.get('grafana_sante'))}"]
    cibles = c.get("cibles")
    if cibles is None:
        lignes.append("cibles Prometheus : non mesuré")
    else:
        lignes.append(f"cibles Prometheus : {cibles['up']} up ({_liste(cibles['jobs_up'])}), "
                      f"{cibles['down']} pas up ({_liste(cibles['jobs_down'])})")
    metriques = c.get("metriques_api")
    if metriques is None:
        lignes.append("métriques de l'API : non mesuré (aucun service publié n'expose de métriques applicatives)")
    else:
        applicatives = [n for n in metriques["noms"] if est_metrique_applicative(n)]
        lignes.append(f"métriques de l'API (/metrics → {metriques['code']}) : {len(metriques['noms'])} famille(s), "
                      f"dont applicatives : "
                      + _liste(f"{n} ({metriques['types'].get(n, '?')})" for n in applicatives))
    regles = c.get("regles")
    if regles is None:
        lignes.append("règles Prometheus : non mesuré")
    else:
        alertes = "; ".join(
            f"{a['nom']} [{a['expr']}]" + (f" for {a['for']}" if a.get("for") else "")
            + (f", severity {a['severity']}" if a.get("severity") else "")
            + (", annotations" if a.get("annotations") else ", sans annotations")
            for a in regles["alertes"][:10])
        lignes.append(f"règles Prometheus : {regles['chargees']} chargée(s), {regles['erreurs']} erreur(s) ; "
                      f"{len(regles['alertes'])} alerte(s)" + (f" : {alertes}" if alertes else ""))
    grafana = c.get("grafana")
    if grafana is None:
        lignes.append("Grafana provisionné : non mesuré")
    else:
        lignes.append(
            f"Grafana provisionné : {grafana['datasources']} source(s) de données"
            f" (source Prometheus : {_oui_non(grafana['datasource_prometheus'])}), "
            f"{grafana['dashboards']} tableau(x) de bord, {len(grafana['requetes_panels'])} requête(s) de panneau"
            + (f" : {_liste(grafana['requetes_panels'], maxi=8)}" if grafana["requetes_panels"] else ""))
    derive = c.get("endpoint_drift")
    if derive is None:
        lignes.append("endpoint de dérive : non mesuré (aucun endpoint /…drift… ou /evaluate déclaré, ou API "
                      "introuvable)")
    else:
        lignes.append(f"endpoint de dérive {derive['chemin']} → {derive['code']} ; jauge de dérive modifiée "
                      f"dans /metrics : {_oui_non(derive['metrique_modifiee'])}")
    return "\n".join(lignes)
