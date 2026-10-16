"""Pourquoi un service Compose n'est pas devenu sain : relevé et imputation.

467094 : `docker compose up --wait` échouait sur « container bike-api-1 is
unhealthy ». Le conteneur était « running » (restart: always relance l'app
après chaque plantage) et le moteur ne relevait que les conteneurs arrêtés :
ni logs ni historique du healthcheck, cause inconnue, 2/20.

Ce module ne parle pas à Docker. Il lit les `attrs` d'un `docker inspect` et
le texte des logs, et rend une cause et une imputation :

- `copie` : la copie a planté, ou son healthcheck appelle une commande
  absente de son image ;
- `non_mesure` : la cause est de notre côté (réseau sortant de la machine de
  correction, délai insuffisant même après réessai, mémoire de la machine
  épuisée) ; la copie n'a pas été mesurée et ne doit pas perdre de points
  pour cela ;
- `indetermine` : ni l'un ni l'autre n'est établi (conflit de versions entre
  dépendances, healthcheck qui échoue sans plantage).

L'ordre des règles compte : une erreur réseau produit aussi un traceback et
un code de sortie non nul. Lue comme un plantage, elle donnerait un zéro pour
notre absence de réseau.
"""

import ipaddress
import re

NS = 1_000_000_000

COPIE = "copie"
NON_MESURE = "non_mesure"
INDETERMINE = "indetermine"

# Le vocabulaire de `result["fault"]`, déjà lu par pi-corrector et posé par
# bentoml_runner._attribute_fault.
FAUTE_DU_CONTRAT = {COPIE: "apprenant", NON_MESURE: "systeme", INDETERMINE: "indetermine"}

# Erreurs réseau, dans les formats de requests/urllib3/urllib, curl, wget et
# des résolveurs. L'hôte est extrait à part pour écarter localhost et les
# services de la stack.
_MOTIFS_RESEAU = re.compile(
    r"NameResolutionError|Temporary failure in name resolution|Name or service not known"
    r"|nodename nor servname|getaddrinfo (?:ENOTFOUND|EAI_AGAIN|failed)|Could not resolve host"
    r"|unable to resolve host|Failed to resolve|Network is unreachable|No route to host"
    r"|Max retries exceeded with url|ConnectTimeout|ReadTimeout|Connection timed out"
    r"|timed out|ConnectionRefusedError|Connection refused|ECONNREFUSED|ETIMEDOUT"
    r"|urlopen error|SSLError|CERTIFICATE_VERIFY_FAILED",
    re.IGNORECASE,
)
_HOTES = re.compile(
    r"host='([^']+)'|Failed to resolve '([^']+)'|https?://([A-Za-z0-9.\-\[\]:]+?)(?::\d+)?[/'\"\s>]"
    r"|Could not resolve host:?\s*([A-Za-z0-9.\-]+)|getaddrinfo \w+ ([A-Za-z0-9.\-]+)"
    r"|resolve host ([A-Za-z0-9.\-]+)"
)
# Erreurs de résolution sans hôte lisible : seules celles-ci, sans hôte
# interne nommé, suffisent à conclure au réseau sortant.
_RESOLUTION = re.compile(
    r"Temporary failure in name resolution|EAI_AGAIN|Could not resolve host", re.IGNORECASE)

# Dépendances installées incompatibles entre elles : un paquet non épinglé,
# publié après le rendu, peut casser une copie qui marchait (467094 : pyarrow
# 26.0.0 exige NumPy 2, la copie épingle numpy 1.26.4).
_MOTIFS_VERSIONS = re.compile(
    r"requires [\w.\-]+ [\d.]+ or newer|A module that was compiled using NumPy 1\.x cannot be run"
    r"|numpy\.dtype size changed, may indicate binary incompatibility"
    r"|ContextualVersionConflict|VersionConflict|is incompatible with|requires .* but you have",
    re.IGNORECASE,
)

# Sensible à la casse : « HTTP Error 404: Not Found » n'est pas une commande
# absente.
_MOTIFS_COMMANDE_ABSENTE = re.compile(
    r"\b[\w.-]+: not found|command not found|executable file not found")

_TRACEBACK = re.compile(
    r"Traceback \(most recent call last\)|^\w*(?:Error|Exception)\b.*$|panic:|Segmentation fault",
    re.MULTILINE,
)


def _secondes(valeur):
    return round(valeur / NS, 3) if isinstance(valeur, (int, float)) and valeur else None


def definition_healthcheck(config) -> dict | None:
    """La définition du healthcheck (Config.Healthcheck, durées en ns)."""
    if not config or not config.get("Test") or config.get("Test") == ["NONE"]:
        return None
    return {
        "test": config.get("Test"),
        "interval_s": _secondes(config.get("Interval")) or 30.0,
        "timeout_s": _secondes(config.get("Timeout")) or 30.0,
        "start_period_s": _secondes(config.get("StartPeriod")) or 0.0,
        "retries": config.get("Retries") or 3,
    }


def _sante(attrs: dict) -> dict:
    return (attrs.get("State") or {}).get("Health") or {}


def services_non_sains(etats: dict, boucles=frozenset()) -> list:
    """Les services à diagnostiquer, depuis {nom: attrs d'inspect}.

    Non sain : healthcheck unhealthy ou starting, sorti en erreur, ou en
    boucle de redémarrage observée (`boucles`, restart: always masque un
    plantage en « running »). Un service sain, ou qui a redémarré une fois
    en attendant sa dépendance puis tient, n'est pas en cause. Un service
    seulement créé est une victime de sa dépendance : rien à lire.
    """
    retenus = []
    for nom, attrs in sorted(etats.items()):
        state = attrs.get("State") or {}
        statut = state.get("Status")
        sante = _sante(attrs).get("Status")
        if statut == "created" or sante == "healthy":
            continue
        if (sante in ("unhealthy", "starting")
                or statut in ("restarting", "dead")
                or statut == "exited" and state.get("ExitCode") not in (0, None)
                or nom in boucles):
            retenus.append(nom)
    return retenus


_NOMS_SECRETS = re.compile(r"PASS|PWD|SECRET|TOKEN|KEY|CREDENTIAL|AUTH", re.IGNORECASE)


def masquer_secrets(texte: str, attrs: dict) -> str:
    """Masquer les valeurs des variables d'environnement d'allure secrète
    (mot de passe Grafana, jetons) : `_preparer_env` promet de ne jamais
    consigner une valeur, et un healthcheck interpolé ou un log peut en
    contenir une."""
    for entree in (attrs.get("Config") or {}).get("Env") or []:
        nom, _, valeur = str(entree).partition("=")
        if len(valeur) >= 4 and _NOMS_SECRETS.search(nom):
            texte = texte.replace(valeur, "***")
    return texte


def _hote_externe(hote: str) -> bool:
    hote = hote.strip("[]").lower()
    if not hote or hote in ("localhost", "0.0.0.0", "::1") or "." not in hote and ":" not in hote:
        return False  # localhost, ou nom de service de la stack (sans point)
    try:
        ip = ipaddress.ip_address(hote)
    except ValueError:
        return not hote.endswith((".local", ".internal", ".localhost"))
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def _ligne(texte: str, motif: re.Pattern) -> str:
    for ligne in texte.splitlines():
        if motif.search(ligne):
            return ligne.strip()[:300]
    return ""


def _reseau_sortant(logs: str) -> str:
    """La ligne qui prouve une erreur réseau vers l'extérieur, ou ''."""
    hotes_internes = False
    for ligne in logs.splitlines():
        if not _MOTIFS_RESEAU.search(ligne):
            continue
        hotes = [h for groupe in _HOTES.findall(ligne) for h in groupe if h]
        if any(_hote_externe(h) for h in hotes):
            return ligne.strip()[:300]
        hotes_internes = hotes_internes or bool(hotes)
    if not hotes_internes:
        return _ligne(logs, _RESOLUTION)
    return ""


def _derniere_erreur(logs: str) -> str:
    lignes = [l.strip() for l in logs.splitlines() if l.strip()]
    for ligne in reversed(lignes):
        if re.match(r"^[\w.]*(?:Error|Exception|Exit)\b", ligne) or "Error:" in ligne:
            return ligne[:300]
    return lignes[-1][:300] if lignes else ""


def classer_service(attrs: dict, logs: str) -> dict:
    """Cause et imputation pour un service, depuis son inspect et ses logs."""
    logs = logs or ""
    state = attrs.get("State") or {}
    sante = _sante(attrs)
    statut_sante = sante.get("Status")
    sorties_sante = "\n".join(str(e.get("Output") or "") for e in sante.get("Log") or [])
    codes_sante = [e.get("ExitCode") for e in sante.get("Log") or []]
    redemarrages = attrs.get("RestartCount") or 0
    code = state.get("ExitCode")
    sorti_en_erreur = state.get("Status") in ("exited", "dead", "restarting") and code not in (0, None)
    plante = bool(_TRACEBACK.search(logs)) or sorti_en_erreur or redemarrages > 0

    def cause(nom, imputation, preuve, reessayable=False):
        return {"cause": nom, "imputation": imputation, "preuve": preuve,
                "reessayable": reessayable}

    preuve = _reseau_sortant(logs)
    if preuve:
        return cause("reseau_sortant", NON_MESURE,
                     f"erreur réseau vers un hôte externe : {preuve}")
    if state.get("OOMKilled"):
        # Le moteur ne pose pas de limite mémoire : c'est la machine de
        # correction qui manque de mémoire (même règle que bentoml_runner).
        return cause("memoire_epuisee", NON_MESURE,
                     f"conteneur tué faute de mémoire (OOMKilled, code {code})")
    preuve = _ligne(logs, _MOTIFS_VERSIONS)
    if preuve:
        return cause("conflit_versions_dependances", INDETERMINE,
                     f"dépendances installées incompatibles : {preuve}. Un paquet non épinglé "
                     "publié après le rendu peut en être la cause : à trancher en reconstruisant "
                     "avec les versions disponibles à la date du rendu")
    if 127 in codes_sante or _MOTIFS_COMMANDE_ABSENTE.search(sorties_sante):
        return cause("healthcheck_commande_absente", COPIE,
                     "la commande du healthcheck n'existe pas dans l'image : "
                     + (_ligne(sorties_sante, _MOTIFS_COMMANDE_ABSENTE) or "code de sortie 127"))
    if plante:
        return cause("plantage_application", COPIE,
                     f"l'application plante (code {code}, {redemarrages} redémarrage(s)) : "
                     f"{_derniere_erreur(logs)}")
    if statut_sante == "starting":
        return cause("delai_depasse", NON_MESURE,
                     "healthcheck encore en démarrage quand compose a conclu", reessayable=True)
    if statut_sante == "unhealthy":
        derniere = (sante.get("Log") or [{}])[-1]
        return cause("healthcheck_echoue", INDETERMINE,
                     "healthcheck en échec sans plantage ni redémarrage (dernier essai : code "
                     f"{derniere.get('ExitCode')}, {str(derniere.get('Output') or '').strip()[:200]})",
                     reessayable=True)
    return cause("non_etablie", INDETERMINE, "aucune cause lisible dans l'état ni dans les logs")


_RANG = {NON_MESURE: 0, COPIE: 1, INDETERMINE: 2}


def classer_demarrage(causes: dict) -> dict:
    """La cause retenue pour la stack, depuis {service: cause}.

    Une cause non mesurée l'emporte : la note ne peut pas reposer sur une
    stack que nous n'avons pas pu faire tourner. Puis une faute établie de
    la copie, puis l'indéterminé.
    """
    if not causes:
        return {"imputation": INDETERMINE, "cause": "non_etablie", "service": None,
                "preuve": "aucun service non sain relevé"}
    # Compose abandonne dès qu'un service plante : les autres restent en
    # « starting ». Ce délai-là n'est pas vérifié (pas de réessai) et ne doit
    # pas masquer une cause établie.
    fermes = {s: c for s, c in causes.items() if not c.get("reessayable")}
    causes = fermes or causes
    service, retenue = min(causes.items(), key=lambda kv: (_RANG.get(kv[1]["imputation"], 3), kv[0]))
    return {"imputation": retenue["imputation"], "cause": retenue["cause"], "service": service,
            "preuve": retenue.get("preuve", "")}


def couper(texte: str, maxi: int) -> str:
    texte = (texte or "").strip()
    return texte if len(texte) <= maxi else f"[… {len(texte) - maxi} caractères coupés]\n" + texte[-maxi:]


def decrire_service(nom: str, attrs: dict, logs: str, cause: dict, derniers_essais: int = 3) -> str:
    """Le bloc lisible d'un service non sain : état, santé, healthcheck, logs.

    N'affiche jamais Config.Env (mots de passe Grafana et autres)."""
    state = attrs.get("State") or {}
    sante = _sante(attrs)
    lignes = [f"=== {nom} ===",
              f"état : {state.get('Status')}, ExitCode {state.get('ExitCode')}, "
              f"OOMKilled {state.get('OOMKilled')}, RestartCount {attrs.get('RestartCount') or 0}"]
    hc = definition_healthcheck((attrs.get("Config") or {}).get("Healthcheck"))
    if hc:
        lignes.append(f"healthcheck : test {masquer_secrets(str(hc['test']), attrs)}, interval {hc['interval_s']:g}s, "
                      f"timeout {hc['timeout_s']:g}s, retries {hc['retries']}, "
                      f"start_period {hc['start_period_s']:g}s")
    else:
        lignes.append("healthcheck : aucun")
    if sante:
        lignes.append(f"santé : {sante.get('Status')}, FailingStreak {sante.get('FailingStreak')}")
        essais = (sante.get("Log") or [])[-derniers_essais:]
        if not essais:
            lignes.append("  (historique vide : Docker le remet à zéro à chaque redémarrage)")
        for essai in essais:
            lignes.append(f"  essai code {essai.get('ExitCode')} : "
                          f"{couper(masquer_secrets(str(essai.get('Output') or ''), attrs), 400)}")
    lignes.append(masquer_secrets(
        f"cause : {cause['cause']} → imputation {cause['imputation']} ; {cause['preuve']}", attrs))
    lignes.append(f"--- logs (200 dernières lignes, fin) ---\n"
                  f"{couper(masquer_secrets(logs, attrs), 4000) or '(vides)'}")
    return "\n".join(lignes)


def delai_de_reessai(attrs: dict, plafond: float) -> float:
    """start_period + retries × (interval + timeout), plafonné."""
    hc = definition_healthcheck((attrs.get("Config") or {}).get("Healthcheck")) or {}
    attente = (hc.get("start_period_s", 0)
               + hc.get("retries", 3) * (hc.get("interval_s", 30) + hc.get("timeout_s", 30)))
    return max(0.0, min(attente, plafond))
