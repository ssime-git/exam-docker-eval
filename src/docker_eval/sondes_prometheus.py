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


def alertes_grafana(regles: list, maxi: int = 50) -> list:
    """/api/v1/provisioning/alert-rules → [{titre, expr}].

    L'expression est le `model.expr` de la première requête qui en porte
    une : les suivantes sont les expressions serveur (seuil, réduction,
    `datasourceUid: __expr__`), sans PromQL."""
    alertes = []
    for regle in regles[:maxi]:
        if not isinstance(regle, dict):
            continue
        expr = next((d["model"]["expr"] for d in regle.get("data") or []
                     if isinstance(d, dict) and isinstance(d.get("model"), dict)
                     and isinstance(d["model"].get("expr"), str)), "")
        alertes.append({"titre": str(regle.get("title") or ""), "expr": expr})
    return alertes


# `curl -X POST $(API_URL)/trigger-drift`, `http://localhost:8080/evaluate` :
# l'URL passe souvent par une variable du Makefile, que `requetes_declarees`
# (localhost en dur) ne voit pas.
_APPEL_DERIVE = re.compile(
    r"(?:https?://[^\s/\"']+|\$\(\w+\)|\$\{\w+\}|\$\w+)(/[\w/-]*?(?:drift|evaluate)[\w-]*)")
_ROUTE_DERIVE = re.compile(
    r"@\w+\.(get|post|put)\(\s*[\"'](/[^\"']*(?:drift|evaluate)[^\"']*)[\"']", re.IGNORECASE)


def _par_preference(candidats: list) -> dict | None:
    """Un endpoint qui dit « drift » d'abord : il déclenche la dérive sans
    corps. /evaluate attend un lot de données « courantes » que le harnais
    doit fabriquer (`requete_depuis_openapi`)."""
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
                    # Le corps que l'apprenant envoie lui-même passe avant
                    # tout corps fabriqué (même lecture que `requetes_declarees`).
                    corps = re.search(r"(?:-d|--data(?:-raw)?)\s+'([^']*)'", ligne) or \
                        re.search(r"(?:-d|--data(?:-raw)?)\s+\"([^\"]*)\"", ligne)
                    if corps:
                        candidat["corps"] = corps.group(1)
                    if candidat["chemin"] not in [c["chemin"] for c in declares]:
                        declares.append(candidat)
        else:
            for m in _ROUTE_DERIVE.finditer(texte):
                candidat = {"chemin": m.group(2), "methode": m.group(1).upper(), "source": source}
                if candidat["chemin"] not in [c["chemin"] for c in routes]:
                    routes.append(candidat)
    return _par_preference(declares) or _par_preference(routes)


# --- Un corps pour /evaluate, tiré du schéma OpenAPI de l'API --------------
#
# L'énoncé : /evaluate « acceptera des données courantes pour une période
# (ex : une semaine de février) ». 467379 (15/10) ne déclare aucun curl : le
# moteur POSTait /evaluate sans corps → 422, la jauge jamais relue. FastAPI
# publie /openapi.json ; on y lit le corps attendu et on en fabrique un
# exemple plausible. Une API sans schéma (Flask) est appelée comme avant.

DEBUT_PERIODE, FIN_PERIODE = "2011-02-01", "2011-02-07"
LIGNES_PAR_LOT = 24
MAX_PROFONDEUR = 8
# Une ligne du jeu Bike Sharing UCI (les colonnes de l'énoncé, la cible
# `cnt`) : février 2011, hiver (season 1), mardi 1er février (weekday 2).
LIGNE_JEU = {"temp": 0.24, "atemp": 0.29, "hum": 0.6, "windspeed": 0.2, "mnth": 2, "hr": 8, "weekday": 2,
             "season": 1, "holiday": 0, "workingday": 1, "weathersit": 1, "yr": 0, "cnt": 40,
             "dteday": DEBUT_PERIODE}
COLONNES_ENONCE = ("temp", "atemp", "hum", "windspeed", "mnth", "hr", "weekday", "season", "holiday",
                   "workingday", "weathersit", "dteday", "cnt")
_NOMS_LOT = {"data", "records", "rows", "lignes", "donnees", "items", "current", "current_data", "batch"}


def _mots(nom: str) -> list:
    """`startDate`, `start_date`, `date-debut` → ['start', 'date']…"""
    return [m.lower() for m in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", nom or "")]


def _resoudre(schema, spec: dict, profondeur: int = 0) -> dict:
    """$ref, allOf, anyOf/oneOf (Optional de Pydantic v2 : la branche non
    nulle) ramenés à un seul schéma."""
    for _ in range(MAX_PROFONDEUR):
        if not isinstance(schema, dict):
            return {}
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            cible = spec
            for morceau in ref[2:].split("/"):
                cible = cible.get(morceau, {}) if isinstance(cible, dict) else {}
            reste = {k: v for k, v in schema.items() if k != "$ref"}
            schema = {**cible, **reste} if isinstance(cible, dict) else reste
            continue
        if isinstance(schema.get("allOf"), list):
            fusion = {k: v for k, v in schema.items() if k != "allOf"}
            for partie in schema["allOf"]:
                partie = _resoudre(partie, spec, profondeur + 1)
                fusion = {**partie, **fusion,
                          "properties": {**(partie.get("properties") or {}), **(fusion.get("properties") or {})},
                          "required": list(partie.get("required") or []) + list(fusion.get("required") or [])}
            if not fusion["properties"]:
                fusion.pop("properties")
            if not fusion["required"]:
                fusion.pop("required")
            schema = fusion
            continue
        for cle in ("anyOf", "oneOf"):
            if isinstance(schema.get(cle), list):
                branches = [b for b in schema[cle] if not (isinstance(b, dict) and b.get("type") == "null")]
                reste = {k: v for k, v in schema.items() if k != cle}
                schema = {**(branches[0] if branches else {}), **reste}
                break
        else:
            return schema
    return schema if isinstance(schema, dict) else {}


def _type(schema: dict) -> str | None:
    genre = schema.get("type")
    if isinstance(genre, list):
        genre = next((g for g in genre if g != "null"), None)
    if genre is None:
        if "properties" in schema or "additionalProperties" in schema:
            return "object"
        if "items" in schema:
            return "array"
    return genre


def _valeur_jeu(nom: str, genre: str | None, i: int = 0):
    """La valeur d'une colonne du jeu Bike Sharing pour la ligne i du lot :
    heures et jours de la semaine de février qui varient, cible aussi."""
    jour = i % 7
    if nom == "dteday":
        return f"2011-02-{1 + jour:02d}"
    if nom == "hr":
        return (8 + 5 * i) % 24
    if nom == "weekday":
        return (2 + jour) % 7
    if nom == "workingday":
        return 0 if (2 + jour) % 7 in (0, 6) else 1
    if nom == "cnt":
        return 40 + 7 * i
    if nom in ("temp", "atemp", "hum", "windspeed"):
        return round(LIGNE_JEU[nom] + 0.01 * (i % 10), 2)
    valeur = LIGNE_JEU[nom]
    if genre == "string":
        return str(valeur)
    return valeur


def _ligne_jeu(schema_ligne: dict, spec: dict, i: int, profondeur: int) -> dict:
    """Une ligne du jeu : les noms de propriétés du schéma quand il en donne,
    sinon ceux du schéma de /predict (BikeSharingInput) ou de l'énoncé."""
    proprietes = schema_ligne.get("properties") or {}
    if not proprietes:
        noms = list(_schema_des_lignes(spec)) or list(COLONNES_ENONCE)
        if "cnt" not in noms:
            noms.append("cnt")
        return {n: _valeur_jeu(n, None, i) for n in noms if n in LIGNE_JEU}
    ligne = {}
    for nom, sous in proprietes.items():
        sous = _resoudre(sous, spec)
        if nom in LIGNE_JEU:
            ligne[nom] = _valeur_jeu(nom, _type(sous), i)
        else:
            ligne[nom] = _exemple(sous, spec, nom, profondeur + 1)
    return ligne


def _schema_des_lignes(spec: dict) -> dict:
    """Le schéma des composants qui ressemble le plus à une ligne du jeu
    (BikeSharingInput dans le gabarit : /predict l'utilise)."""
    meilleur, score = {}, 2
    for schema in ((spec.get("components") or {}).get("schemas") or {}).values():
        proprietes = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(proprietes, dict):
            communes = len(set(proprietes) & set(LIGNE_JEU))
            if communes > score:
                meilleur, score = proprietes, communes
    return meilleur


def _est_lot_de_lignes(nom: str, schema: dict, items: dict) -> bool:
    if _type(items) != "object":
        return False
    proprietes = items.get("properties") or {}
    if proprietes:
        return len(set(proprietes) & set(LIGNE_JEU)) >= 3
    description = str(schema.get("description") or "") + str(schema.get("title") or "")
    return (nom or "").lower() in _NOMS_LOT or "cnt" in description


def _date(nom: str, format_: str | None) -> str:
    mots = _mots(nom)
    fin = any(m in ("end", "fin", "to", "until", "stop") for m in mots)
    jour = FIN_PERIODE if fin else DEBUT_PERIODE
    return f"{jour}T00:00:00" if format_ == "date-time" else jour


def _exemple(schema, spec: dict, nom: str = "", profondeur: int = 0):
    """Un exemple plausible pour un schéma JSON (OpenAPI 3.0/3.1)."""
    schema = _resoudre(schema, spec)
    if profondeur > MAX_PROFONDEUR:
        return None
    for cle in ("example", "default", "const"):
        if cle in schema:
            return schema[cle]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    genre = _type(schema)
    mots = _mots(nom)
    if genre == "object":
        proprietes = schema.get("properties") or {}
        requises = schema.get("required")
        noms = [n for n in proprietes if n in requises] if isinstance(requises, list) else list(proprietes)
        return {n: _exemple(proprietes[n], spec, n, profondeur + 1) for n in noms}
    if genre == "array":
        items = _resoudre(schema.get("items") or {}, spec)
        if _est_lot_de_lignes(nom, schema, items):
            return [_ligne_jeu(items, spec, i, profondeur) for i in range(LIGNES_PAR_LOT)]
        return [_exemple(items, spec, nom, profondeur + 1)]
    if genre in ("integer", "number"):
        if any(m in ("week", "semaine") for m in mots):
            return 1
        if nom in LIGNE_JEU:
            return _valeur_jeu(nom, genre)
        if "minimum" in schema:
            return schema["minimum"]
        return 1 if genre == "integer" else 0.5
    if genre == "boolean":
        return False
    if genre == "string" or genre is None:
        format_ = schema.get("format")
        if format_ in ("date", "date-time") or any(
                m in ("date", "start", "end", "debut", "fin", "from", "to", "week", "semaine", "period", "periode")
                for m in mots):
            return _date(nom, format_)
        return "exemple" if genre == "string" else None
    return None


def requete_depuis_openapi(spec, chemin: str, methode: str = "POST") -> dict | None:
    """La requête qu'un schéma OpenAPI attend pour `chemin` : {methode,
    corps (objet JSON ou None), query (dict)} ; None si le schéma ne décrit
    ni corps ni paramètre de requête (l'appel sans corps reste juste).

    La méthode déclarée d'abord, puis POST, puis GET."""
    if not isinstance(spec, dict):
        return None
    operations = (spec.get("paths") or {}).get(chemin)
    if not isinstance(operations, dict):
        return None
    for candidate in dict.fromkeys(((methode or "POST").upper(), "POST", "GET")):
        operation = operations.get(candidate.lower())
        if not isinstance(operation, dict):
            continue
        corps = None
        contenu = (_resoudre(operation.get("requestBody") or {}, spec).get("content") or {})
        media = contenu.get("application/json") or next(
            (v for k, v in contenu.items() if "json" in k), None)
        if isinstance(media, dict) and "schema" in media:
            corps = _exemple(media["schema"], spec)
        query = {}
        for parametre in list(operations.get("parameters") or []) + list(operation.get("parameters") or []):
            parametre = _resoudre(parametre, spec)
            if parametre.get("in") != "query" or not parametre.get("name"):
                continue
            if "example" in parametre:
                valeur = parametre["example"]
            else:
                valeur = _exemple(parametre.get("schema") or {}, spec, parametre["name"])
            if valeur is not None:
                query[parametre["name"]] = valeur
        if corps is None and not query:
            return None
        return {"methode": candidate, "corps": corps, "query": query}
    return None


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
        alertes = grafana.get("alertes")
        lignes.append("alertes Grafana provisionnées : " + (
            "non mesuré (/api/v1/provisioning/alert-rules sans réponse)" if alertes is None else
            f"{len(alertes)}" + (" : " + "; ".join(f"{a['titre']} [{a['expr']}]" for a in alertes[:10])
                                 if alertes else "")))
    derive = c.get("endpoint_drift")
    if derive is None:
        lignes.append("endpoint de dérive : non mesuré (aucun endpoint /…drift… ou /evaluate déclaré, ou API "
                      "introuvable)")
    else:
        lignes.append(f"endpoint de dérive {derive['chemin']} → {derive['code']} ; jauge de dérive modifiée "
                      f"dans /metrics : {_oui_non(derive['metrique_modifiee'])}")
    return "\n".join(lignes)
