"""Phase d'investigation outillée, pendant que la stack tourne encore.

Quand une étape échoue sans annotation « attendu », un LLM (gateway
OpenAI-compatible, typiquement Liora) mène une investigation bornée en
lecture seule : re-sonder une URL, lire les logs d'un conteneur, exécuter
une commande de lecture, lire un fichier de la copie, résoudre un nom
(`dns`), lister les ports en écoute (`ports`), lire l'environnement
(`env`, secrets masqués). Chaque action et son observation deviennent des
étapes du contrat, visibles au scratchpad et citables par la revue. Après
le teardown, plus rien n'est testable : c'est ici que le vrai debug se joue
(scriptorium #78).

Débogage par hypothèse (scriptorium #336). Le LLM conçoit l'expérience, le
harnais la juge. Il propose une ou plusieurs hypothèses (un objet, ou une
liste d'objets) :

    {"action":"hypothese","id":"H1","enonce":"…","faute":"apprenant|harnais|indetermine",
     "test":{<action de lecture : sonde, logs, exec, fichier, dns, ports, env>},
     "si_vraie":{<prédiction>},"si_fausse":{<prédiction>},
     "revelable":"…(optionnel)","non_revelable":"…(optionnel)"}

Une prédiction est une conjonction de clés vérifiables mécaniquement :
`code` (code HTTP, entier ou liste), `exit_code` (entier ou liste),
`contient` / `ne_contient_pas` (sous-chaîne ou liste, cherchée dans la
sortie complète de l'action, pas dans l'extrait affiché). Le harnais
rejette sans l'exécuter une hypothèse sans `faute` valide, dont le test
n'est pas une action de lecture, ou dont les deux prédictions peuvent être
vraies ensemble. Seuls trois cas prouvent la disjonction : des ensembles de
`code` disjoints, des ensembles d'`exit_code` disjoints, ou `contient X`
face à `ne_contient_pas Y` avec Y sous-chaîne de X. Sinon il exécute le
test (une action du budget) et compare :

- établie : si_vraie tient et si_fausse ne tient pas ;
- réfutée : si_fausse tient et si_vraie ne tient pas ;
- non tranchée : tout le reste, y compris une donnée absente (pas de code
  HTTP sur un « non reçu »), un refus du harnais (« refusé : … », « échec
  de l'action ») et une erreur du démon docker (conteneur arrêté ou absent :
  son code de retour n'est pas celui de la commande), qui ne sont jamais des
  observations. Sur une lecture tronquée à la source (fichier au-delà de
  LECTURE_FICHIER_MAX, corps HTTP au-delà de LECTURE_SONDE_MAX, logs au
  plafond de --tail), une présence se prouve encore, une absence non.

Chaque expérience devient une étape (contrat lu par scriptorium#340) :
titre « Hypothèse H<n> — <énoncé> », `command` = le test exécuté, une
sortie avec les prédictions, une ligne « résultat : <code=… exit_code=…> »,
les lignes qui contiennent les sous-chaînes prédites, l'extrait brut, et
une dernière ligne « verdict : établie | réfutée | non tranchée ». La clé
`verdict` de l'étape porte la même valeur. Une hypothèse rejetée a aussi
son étape, avec le verdict « non tranchée ». La première hypothèse
établie clôt l'investigation. Son énoncé devient la `cause`, et sa `faute`
celle du verdict. Le scoreur de #307 n'est pas sollicité, et l'étape
« Investigation — vérification du verdict » le consigne.

Verdict direct (compatibilité). {"action":"verdict",…} reste accepté. Un
verdict qui impute une faute (`faute` ≠ `indetermine`) n'est pas pris sur
la parole du LLM (scriptorium #307) : sa `cause` est scorée par un
vérificateur indépendant contre les seules preuves de l'investigation
(sorties des étapes en échec, observations et tests d'hypothèse). Le
vérificateur est un scoring de logprobs sur quatre verdicts, même prompt
que `score_claim` de scriptorium/tools/verification.py ; le banc #304 a
retenu Qwen3.5-4B Q4_K_M derrière llama-server.

- P(soutenu) ≥ seuil : verdict accepté, la distribution est consignée.
- Sous le seuil, avec des actions et du temps restants et moins de
  MAX_ITERATIONS challenges : le LLM reçoit « la cause n'est pas établie par
  tes observations » et doit agir. Un verdict rendu sans observation nouvelle
  depuis le challenge ne compte pas (#306) : il est déclassé.
- Sinon, et sur toute sortie de boucle avec un verdict contesté en attente
  (budget, réponse inexploitable, vérificateur en erreur) : verdict déclassé,
  cause préfixée « hypothèse : », `faute: indetermine`, `revelable` réduit au
  symptôme observé.

Un verdict `faute: indetermine`, un budget épuisé ou une investigation
interrompue après des hypothèses donnent « cause : non établie »,
`faute: indetermine` et un `revelable` réduit au symptôme observé.

Sortie de l'étape « Investigation — verdict ». Sans cause établie, elle
commence par la ligne « cause non établie ». Ensuite viennent les lignes lisibles
(`cause : …`, `faute : …`, `révélable au feedback : …`, `à ne pas
révéler : …`, puis la liste des hypothèses et de leurs expériences). Elle
se termine TOUJOURS par un bloc JSON pour les consommateurs (scriptorium
#338, pi-corrector #337), le dernier bloc ```json de la sortie :

    {"version": 1,
     "statut": "cause_etablie" | "cause_scoree" | "cause_non_verifiee" | "cause_non_etablie",
     "cause": str | null,              # null si statut = cause_non_etablie
     "piste": str | null,              # cause d'un verdict direct déclassé
     "faute": "apprenant" | "harnais" | "indetermine",
     "revelable": str, "non_revelable": str,
     "hypothese_etablie": "H1" | null,
     "hypotheses_restantes": [<hypothèse>],   # non tranchées et rejetées
     "hypotheses_refutees": [<hypothèse>]}

    <hypothèse> = {"id": str, "enonce": str, "faute": str,
                   "resultat": "non_tranchee" | "rejetee" | "refutee" | "etablie",
                   "raison_rejet": str (si rejetee),
                   "experiences": [<expérience>]}   # [] : piste non testée
    <expérience> = {"test": str,
                    "prediction": {"si_vraie": {…}, "si_fausse": {…}},
                    "resultat": "code=405 exit_code=-",   # résultat brut court
                    "verdict": "etablie" | "refutee" | "non_tranchee",
                    "mesures": {"code": int|null, "exit_code": int|null,
                                "tronquee": bool, "fiable": bool,
                                "correspondances": [str], "extrait": str}}

Le bloc tient sur une ligne entre ```json et ```. L'étape porte aussi les
clés `statut` et `hypotheses_restantes`, qui ont les mêmes valeurs que dans le bloc.

`cause_etablie` : une hypothèse établie mécaniquement. `cause_scoree` :
verdict direct accepté par le scoreur. `cause_non_verifiee` : verdict
direct accepté sans scoreur configuré. Une hypothèse retestée sous le même
id accumule ses expériences, et son `resultat` est celui de la dernière.

Environnement. Investigation : PI_CORRECTOR_INVESTIGATE_MAX_ACTIONS
(ACTIONS_MAX, 6) et PI_CORRECTOR_INVESTIGATE_TIMEOUT_SECONDS
(TIMEOUT_TOTAL_SECONDES, 240). Vérification (contrat de scriptorium #311) :
PI_CORRECTOR_VERIFY_BASE_URL (base OpenAI-compatible, `/v1` compris ;
absente = vérification désactivée, et l'étape « Vérification du verdict non
configurée » le dit), PI_CORRECTOR_VERIFY_API_KEY (Bearer, optionnel),
PI_CORRECTOR_VERIFY_MODEL (optionnel, ignoré par llama-server),
PI_CORRECTOR_VERIFY_THRESHOLD (0.55), PI_CORRECTOR_VERIFY_MAX_ITERATIONS (2).

Budget. Le maximum d'actions borne les actions, et chaque test d'hypothèse
compte pour une. Une hypothèse rejetée n'est pas exécutée et ne consomme
pas d'action. Le nombre de tours de LLM reste borné. Les tours de verdict
ne consomment pas d'action (au plus 1 + MAX_ITERATIONS). Le timeout borne
les actions et les ré-investigations, pas le scoring : un verdict direct
final est toujours scoré. Budget additionnel assumé : au plus
1 + MAX_ITERATIONS appels au vérificateur, chacun borné par
VERIFICATION_TIMEOUT_SECONDES (le banc mesure 15 à 26 s par appel sur CPU).
Chaque étape Hypothèse porte sa durée, pour mesurer le temps sur les rejeux.

Trou connu, non comblé ici : l'investigateur ne tourne que dans le runner
compose (ComposeRunner, services persistants), et seulement quand des étapes
sont en échec. Une extraction ratée de la copie, ou les autres runners
(conteneur unique, image, bentoml…), ne sont jamais investigués : leurs
causes runtime restent sans verdict, donc sans vérification.
"""

import functools
import ipaddress
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

ACTIONS_MAX = 6
TIMEOUT_TOTAL_SECONDES = 240
SORTIE_MAX = 2000
# Extrait du résultat brut gardé par expérience dans le bloc JSON.
EXTRAIT_MAX = 300
# Lectures intégrales comparées aux prédictions (l'affichage reste à
# SORTIE_MAX) : au-delà, la lecture est tronquée et une absence ne se prouve
# plus.
LECTURE_FICHIER_MAX = 200_000
LECTURE_SONDE_MAX = 65_536
# Erreurs du CLI docker (conteneur arrêté, absent, binaire manquant) : leur
# code de retour n'est pas celui de la commande dans le conteneur.
_ERREURS_DOCKER = ("Error response from daemon", "No such container", "OCI runtime exec failed")

# Vérificateur du verdict (#307). Prompt et verdicts recopiés de
# scriptorium/tools/verification.py : le seuil vient d'un banc mesuré avec eux.
VERDICTS = {
    "A": ("soutenu", "une preuve établit directement l'affirmation, aucune ne la contredit"),
    "B": ("non_soutenu", "une preuve contredit l'affirmation, aucune ne la soutient"),
    "C": ("conflit_de_preuves", "une preuve la soutient ET une autre la contredit"),
    "D": ("inverifiable", "fait que les preuves ne couvrent pas"),
}
SEUIL_VERIFICATION = 0.55
ITERATIONS_VERIFICATION = 2
VERIFICATION_TIMEOUT_SECONDES = 120
# Le contexte du banc : la fin des preuves, 6000 caractères.
PREUVES_MAX = 6000
PREFIXE_HYPOTHESE = "hypothèse : "

# ponytail: liste noire lexicale, pas un sandbox — le vrai garde-fou est
# l'exec sans écriture possible sur la copie (elle vit hors conteneur) et
# le budget d'actions. Durcir en whitelist si un exam l'exige. Elle ne
# s'applique qu'aux commandes écrites par le LLM : dns, ports et env lancent
# des commandes fixes du harnais.
_EXEC_INTERDITS = (">", ">>", "rm ", "mv ", "cp ", "chmod", "chown", "kill",
                   "shutdown", "reboot", "mkfs", "dd ", "wget", "curl -o", "tee")

ACTIONS_LECTURE = ("sonde", "logs", "exec", "fichier", "dns", "ports", "env")
FAUTES = ("apprenant", "harnais", "indetermine")
CLES_PREDICTION = ("code", "exit_code", "contient", "ne_contient_pas")
# Ce que le harnais rend quand il n'a rien observé : jamais une observation.
_NON_OBSERVATIONS = ("refusé", "échec de l'action", "action inconnue")
# Masquage de `env` : toute clé qui contient un de ces mots.
_CLES_SECRETES = ("KEY", "TOKEN", "SECRET", "PASS")
_NOM_HOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_MARQUEUR_PROC = "__PROC_NET_TCP__"
# Le nom passe en argument positionnel ($1), jamais concaténé au script.
_SCRIPT_DNS = ('getent hosts "$1"; rc=$?; echo "--- /etc/resolv.conf"; '
               'cat /etc/resolv.conf 2>/dev/null; exit $rc')
_SCRIPT_PORTS = ("ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null || "
                 "{ echo " + _MARQUEUR_PROC + "; cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; }")

PROMPT_SYSTEME = """Tu investigues l'échec d'une évaluation d'examen pendant que la stack Docker tourne encore.
Tu réponds UNIQUEMENT par du JSON (un objet, ou une liste d'hypothèses), sans texte autour.

Méthode attendue : le débogage par hypothèse. Tu conçois l'expérience ; le harnais l'exécute et la juge.
{"action":"hypothese","id":"H1","enonce":"<cause supposée>","faute":"apprenant|harnais|indetermine","test":{<une action de lecture ci-dessous>},"si_vraie":{<prédiction>},"si_fausse":{<prédiction>},"revelable":"<ce que le feedback pourra dire si elle est établie : symptôme, où chercher>","non_revelable":"<la solution, à ne jamais donner>"}
Une prédiction combine (ET) : "code" (code HTTP, entier ou liste), "exit_code" (entier ou liste),
"contient" / "ne_contient_pas" (sous-chaîne ou liste). Les deux prédictions doivent s'exclure : codes
différents, exit_code différents, ou "contient":"X" face à "ne_contient_pas":"Y" avec Y inclus dans X.
Sinon l'hypothèse est rejetée sans être exécutée. "faute" est obligatoire : celle qui vaut si l'hypothèse
est établie. "revelable" et "non_revelable" servent au verdict si elle est établie : l'énoncé seul
peut contenir la solution. Le harnais répond établie, réfutée ou non tranchée. La première hypothèse établie devient la
cause du verdict : tu n'as rien d'autre à faire. Réfutée ou non tranchée : propose une autre expérience.
Tu peux envoyer plusieurs hypothèses d'un coup dans une liste JSON ; chaque test coûte une action.

Actions de lecture (utilisables seules ou comme "test" d'une hypothèse) :
{"action":"sonde","url":"http(s)://127.0.0.1:<port>/<chemin>","identifiants":"user:pass"} — refaire une requête, chemins et schémas libres ; "identifiants" (optionnel) ajoute une authentification Basic
{"action":"logs","service":"<nom de conteneur>","lignes":50} — lire la fin des logs
{"action":"exec","service":"<nom de conteneur>","commande":"<commande de LECTURE>"} — ex. cat d'une config effective
{"action":"fichier","chemin":"<chemin relatif dans la copie>"} — lire un fichier rendu par l'apprenant
{"action":"dns","service":"<nom de conteneur>","nom":"<hôte>"} — getent hosts DANS le conteneur, puis son /etc/resolv.conf ; exit_code 0 si le nom se résout
{"action":"ports","service":"<nom de conteneur>"} — ports TCP en écoute dans le conteneur
{"action":"env","service":"<nom de conteneur>"} — variables d'environnement du conteneur (secrets masqués)

Comble les trous de la soumission avant de conclure : une route qui répond 401 se traite en CHERCHANT les
identifiants que l'apprenant a fournis (ses tests, son README, ses scripts — action "fichier") puis en
re-sondant avec ("sonde" + "identifiants"). Ne conclus jamais « identifiants absents ou refusés » sans avoir
lu les fichiers où ils pourraient être.

Si aucune expérience ne peut plus trancher, termine par
{"action":"verdict","cause":"non établie","faute":"indetermine","revelable":"<symptôme observé>","non_revelable":"<la solution>"}
Un verdict direct qui impute une faute sans hypothèse établie reste possible, mais il est scoré par un
vérificateur indépendant et déclassé s'il n'est pas soutenu par tes observations. C'est un examen : le
verdict guide sans jamais donner la correction. Budget strict : {actions_max} actions.
"""


def scorer_affirmation(preuves: str, affirmation: str, *, base_url: str,
                       api_key: str = "", model: str = "",
                       timeout: float = VERIFICATION_TIMEOUT_SECONDES) -> dict:
    """Distribution sur les quatre verdicts, lue dans les logprobs du premier
    token de réponse (softmax restreint aux lettres présentes). Aucune lettre
    dans le top 20 : distribution vide, donc P(soutenu) nulle."""
    options = "\n".join(f"{lettre}. {nom} : {description}"
                        for lettre, (nom, description) in VERDICTS.items())
    charge = {
        "messages": [{"role": "user", "content":
            f"Preuves d'exécution d'une correction d'examen :\n\n{preuves}\n\n"
            f"Affirmation du feedback à vérifier : {affirmation}\n\n"
            f"Choisis le verdict :\n{options}\n\n"
            "Réponds par une seule lettre."}],
        "max_tokens": 1,
        "temperature": 0,
        "logprobs": True,
        "top_logprobs": 20,
        # Sans quoi un modèle à raisonnement ouvre par « Thinking » et les
        # logprobs ne portent plus sur le verdict.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if model:
        charge["model"] = model
    entetes = {"Content-Type": "application/json"}
    if api_key:
        entetes["Authorization"] = f"Bearer {api_key}"
    requete = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(charge).encode(), headers=entetes)
    with urllib.request.urlopen(requete, timeout=timeout) as reponse:
        contenu = json.load(reponse)
    tops = contenu["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    lp = {}
    for t in tops:
        lettre = t["token"].strip()
        if lettre in VERDICTS and lettre not in lp:
            lp[lettre] = t["logprob"]
    if not lp:
        return {}
    m = max(lp.values())
    z = sum(math.exp(v - m) for v in lp.values())
    return {VERDICTS[l][0]: math.exp(v - m) / z for l, v in lp.items()}


def _nombre_env(nom: str, defaut, conversion):
    try:
        return conversion(os.environ.get(nom, "").strip() or defaut)
    except ValueError:
        return defaut




class Observation(str):
    """Sortie affichée d'une action, avec ce que le harnais peut comparer.

    `code` (HTTP) et `exit_code` valent None quand l'action ne les produit
    pas. `integrale` est la sortie avant troncature d'affichage : les
    prédictions portent sur elle. `fiable` est faux quand le harnais n'a rien
    observé (refus, erreur du démon docker). `tronquee` est vrai quand la
    lecture elle-même s'est arrêtée avant la fin (fichier plus long, corps
    HTTP plus long, logs au plafond de --tail) : une absence n'y prouve rien.
    Une `str` nue rendue par une action se lit comme une Observation sans
    code ni code de sortie."""

    def __new__(cls, texte, code=None, exit_code=None, integrale=None, fiable=True,
                tronquee=False):
        obs = super().__new__(cls, texte)
        obs.code = code
        obs.exit_code = exit_code
        obs.integrale = str(texte) if integrale is None else integrale
        obs.fiable = fiable
        obs.tronquee = tronquee
        return obs


def _lire(obs) -> dict:
    texte = str(obs)
    return {"code": getattr(obs, "code", None),
            "exit_code": getattr(obs, "exit_code", None),
            "integrale": getattr(obs, "integrale", texte),
            "fiable": getattr(obs, "fiable", not texte.startswith(_NON_OBSERVATIONS)),
            "tronquee": getattr(obs, "tronquee", False)}


def _liste(valeur) -> list:
    return list(valeur) if isinstance(valeur, (list, tuple)) else [valeur]


def _valider_prediction(prediction) -> str:
    if not isinstance(prediction, dict) or not prediction:
        return "prédiction vide ou absente : toujours vraie, donc prédictions non disjointes"
    for cle, valeur in prediction.items():
        if cle not in CLES_PREDICTION:
            return f"clé de prédiction inconnue : {cle} (attendu : {', '.join(CLES_PREDICTION)})"
        valeurs = _liste(valeur)
        if not valeurs:
            return f"{cle} vide"
        if cle in ("code", "exit_code"):
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in valeurs):
                return f"{cle} doit être un entier ou une liste d'entiers"
        elif not all(isinstance(v, str) and v for v in valeurs):
            return f"{cle} doit être une chaîne non vide ou une liste de chaînes"
    return ""


def predictions_disjointes(a: dict, b: dict) -> bool:
    """Vrai seulement si les deux prédictions ne peuvent pas tenir ensemble.
    Par défaut, non : on ne rejette que ce qu'on sait prouver disjoint."""
    for cle in ("code", "exit_code"):
        if cle in a and cle in b and not set(_liste(a[cle])) & set(_liste(b[cle])):
            return True
    for p, q in ((a, b), (b, a)):
        for present in _liste(p.get("contient", [])):
            for absent in _liste(q.get("ne_contient_pas", [])):
                if absent in present:
                    return True
    return False


def evaluer_prediction(prediction: dict, lu: dict):
    """True, False, ou None quand une donnée comparée manque. Sur une lecture
    tronquée, une présence se prouve encore, une absence non."""
    inconnu_si_tronque = None if lu.get("tronquee") else False
    resultats = []
    for cle, attendu in prediction.items():
        if cle in ("code", "exit_code"):
            obtenu = lu[cle]
            resultats.append(None if obtenu is None else obtenu in _liste(attendu))
        elif cle == "contient":
            presents = all(s in lu["integrale"] for s in _liste(attendu))
            resultats.append(True if presents else inconnu_si_tronque)
        else:
            present = any(s in lu["integrale"] for s in _liste(attendu))
            resultats.append(False if present else (None if lu.get("tronquee") else True))
    if False in resultats:
        return False
    if None in resultats:
        return None
    return True


def verdict_mecanique(si_vraie: dict, si_fausse: dict, lu: dict) -> str:
    if not lu["fiable"]:
        return "non_tranchee"
    vraie = evaluer_prediction(si_vraie, lu)
    fausse = evaluer_prediction(si_fausse, lu)
    if vraie is True and fausse is False:
        return "etablie"
    if fausse is True and vraie is False:
        return "refutee"
    return "non_tranchee"


_LIBELLES = {"etablie": "établie", "refutee": "réfutée",
             "non_tranchee": "non tranchée", "rejetee": "rejetée"}


def correspondances(predictions: list, integrale: str, par_motif: int = 3) -> list:
    """Les lignes qui contiennent les sous-chaînes prédites : la preuve d'un
    « contient » reste citable même quand l'affichage garde la fin."""
    motifs = []
    for prediction in predictions:
        for cle in ("contient", "ne_contient_pas"):
            motifs += [m for m in _liste(prediction.get(cle, [])) if m not in motifs]
    lignes = []
    for motif in motifs:
        trouvees = [ligne.strip()[:200] for ligne in integrale.splitlines() if motif in ligne]
        lignes += [ligne for ligne in trouvees[:par_motif] if ligne not in lignes]
    return lignes


def masquer_env(lignes: list) -> list:
    """Masque la valeur des clés qui ressemblent à un secret."""
    masquees = []
    for ligne in lignes:
        cle, egal, _valeur = str(ligne).partition("=")
        if egal and any(mot in cle.upper() for mot in _CLES_SECRETES):
            masquees.append(f"{cle}=***")
        else:
            masquees.append(str(ligne))
    return masquees


def _adresse_proc(hexa: str) -> str:
    adresse, _, port = hexa.partition(":")
    brut = bytes.fromhex(adresse)
    if len(brut) == 4:
        ip = ipaddress.IPv4Address(brut[::-1])
    else:
        ip = ipaddress.IPv6Address(b"".join(brut[i:i + 4][::-1] for i in range(0, 16, 4)))
    hote = f"[{ip}]" if ip.version == 6 else str(ip)
    return f"{hote}:{int(port, 16)}"


def ecoutes_proc_net_tcp(texte: str) -> list:
    """Les sockets en écoute (état 0A) d'un /proc/net/tcp{,6}."""
    ecoutes = []
    for ligne in texte.splitlines():
        champs = ligne.split()
        if len(champs) > 3 and champs[0].endswith(":") and champs[3] == "0A":
            try:
                ecoutes.append(f"LISTEN {_adresse_proc(champs[1])}")
            except ValueError:
                continue
    return ecoutes


class Investigator:
    """Boucle d'investigation adossée au runner (record_step, conteneurs).

    `scoreur(preuves, affirmation) -> {verdict: probabilité}` s'injecte pour
    les contrôles ; par défaut, `scorer_affirmation` sur
    PI_CORRECTOR_VERIFY_BASE_URL, ou aucun si la variable est absente."""

    def __init__(self, runner, eval_dir: str, services: list, scoreur=None):
        self.runner = runner
        self.eval_dir = os.path.realpath(eval_dir)
        # Seuls les conteneurs de la copie évaluée sont accessibles : sans ce
        # périmètre, le LLM verrait et exécuterait dans les autres stacks de
        # la machine.
        self.services = list(services)
        self.base_url = (os.environ.get("PI_CORRECTOR_INVESTIGATE_BASE_URL")
                         or os.environ.get("LIORA_GATEWAY_URL", "")).rstrip("/")
        self.api_key = (os.environ.get("PI_CORRECTOR_INVESTIGATE_API_KEY")
                        or os.environ.get("LIORA_API_KEY", ""))
        # Jamais de modèle en dur : la variable d'env prime, sinon la gateway
        # elle-même dit ce qu'elle sert (hot-swap côté gateway sans redéploiement).
        self.model = os.environ.get("PI_CORRECTOR_INVESTIGATE_MODEL", "")
        base_verification = os.environ.get("PI_CORRECTOR_VERIFY_BASE_URL", "").strip()
        if scoreur is None and base_verification:
            scoreur = functools.partial(
                scorer_affirmation, base_url=base_verification,
                api_key=os.environ.get("PI_CORRECTOR_VERIFY_API_KEY", "").strip(),
                model=os.environ.get("PI_CORRECTOR_VERIFY_MODEL", "").strip())
        self.scoreur = scoreur
        self.seuil = _nombre_env("PI_CORRECTOR_VERIFY_THRESHOLD", SEUIL_VERIFICATION, float)
        self.iterations_max = _nombre_env("PI_CORRECTOR_VERIFY_MAX_ITERATIONS",
                                          ITERATIONS_VERIFICATION, int)
        self.actions_max = _nombre_env("PI_CORRECTOR_INVESTIGATE_MAX_ACTIONS", ACTIONS_MAX, int)
        self.timeout_total = _nombre_env("PI_CORRECTOR_INVESTIGATE_TIMEOUT_SECONDS",
                                         TIMEOUT_TOTAL_SECONDES, float)
        self._observations = []   # (titre, commande, sortie) : les preuves de #307
        self._hypotheses = {}     # id -> hypothèse et ses expériences, dans l'ordre

    def disponible(self) -> bool:
        return bool(self.base_url and self.api_key)

    # --- actions -----------------------------------------------------------

    def _sonde(self, url: str, identifiants: str = "") -> str:
        if "127.0.0.1" not in url and "localhost" not in url:
            return Observation("refusé : seules les URLs locales (127.0.0.1) sont sondables",
                               fiable=False)
        from .sonde_http import resume, sonder
        entetes = {}
        if identifiants:
            import base64
            entetes["Authorization"] = "Basic " + base64.b64encode(identifiants.encode()).decode()
        # Même règle que les sondes du runner : la chaîne de redirections est
        # consignée, une cible injoignable n'est pas une panne (#320).
        sonde = sonder(url, entetes=entetes, taille_extrait=LECTURE_SONDE_MAX)
        if not isinstance(sonde["code"], int):
            return Observation(f"non reçu : {sonde['erreur']}")
        corps = sonde["extrait"]
        return Observation(f"{resume(sonde)}\n{corps[:400]}", code=sonde["code"],
                           integrale=f"{resume(sonde)}\n{corps}",
                           tronquee=len(corps.encode()) >= LECTURE_SONDE_MAX)

    def _hors_perimetre(self, service: str):
        if service not in self.services:
            return Observation("refusé : conteneur hors du périmètre de la copie", fiable=False)
        return None

    @staticmethod
    def _sortie(resultat, vide: str, tronquee: bool = False) -> Observation:
        integrale = (resultat.stdout or "") + (resultat.stderr or "")
        # Conteneur arrêté ou absent : le code est celui du CLI docker, pas
        # de la commande. Ce n'est pas une observation de la copie.
        erreur_docker = any(e in (resultat.stderr or "") for e in _ERREURS_DOCKER)
        return Observation(integrale[-SORTIE_MAX:] or vide,
                           exit_code=None if erreur_docker else resultat.returncode,
                           integrale=integrale, fiable=not erreur_docker, tronquee=tronquee)

    def _logs(self, service: str, lignes: int) -> str:
        refus = self._hors_perimetre(service)
        if refus is not None:
            return refus
        plafond = min(int(lignes or 50), 200)
        resultat = subprocess.run(
            ["docker", "logs", "--tail", str(plafond), service],
            capture_output=True, text=True, timeout=20)
        # Au plafond de --tail, le début des logs manque peut-être.
        lues = ((resultat.stdout or "") + (resultat.stderr or "")).count("\n")
        return self._sortie(resultat, "(logs vides)", tronquee=lues >= plafond)

    def _exec(self, service: str, commande: str) -> str:
        refus = self._hors_perimetre(service)
        if refus is not None:
            return refus
        if any(interdit in commande for interdit in _EXEC_INTERDITS):
            return Observation("refusé : commande d'écriture ou de réseau — lecture seule",
                               fiable=False)
        resultat = subprocess.run(
            ["docker", "exec", service, "sh", "-c", commande],
            capture_output=True, text=True, timeout=20)
        return self._sortie(resultat, f"(vide, rc={resultat.returncode})")

    def _fichier(self, chemin: str) -> str:
        cible = os.path.realpath(os.path.join(self.eval_dir, chemin))
        if not cible.startswith(self.eval_dir + os.sep):
            return Observation("refusé : chemin hors de la copie", fiable=False)
        if not os.path.isfile(cible):
            return Observation("fichier introuvable", exit_code=1)
        with open(cible, encoding="utf-8", errors="replace") as lecteur:
            contenu = lecteur.read(LECTURE_FICHIER_MAX)
            reste = lecteur.read(1)
        return Observation(contenu[:SORTIE_MAX], exit_code=0, integrale=contenu,
                           tronquee=bool(reste))

    def _dns(self, service: str, nom: str) -> str:
        """getent hosts dans le conteneur, puis son resolv.conf (un `dns:`
        imposé par le compose s'y lit : 461451)."""
        refus = self._hors_perimetre(service)
        if refus is not None:
            return refus
        if not _NOM_HOTE.match(str(nom or "")):
            return Observation("refusé : nom d'hôte invalide", fiable=False)
        resultat = subprocess.run(
            ["docker", "exec", service, "sh", "-c", _SCRIPT_DNS, "sh", nom],
            capture_output=True, text=True, timeout=20)
        return self._sortie(resultat, f"(vide, rc={resultat.returncode})")

    def _ports(self, service: str) -> str:
        """ss, sinon netstat, sinon /proc/net/tcp décodé : les images minces
        n'ont souvent ni l'un ni l'autre."""
        refus = self._hors_perimetre(service)
        if refus is not None:
            return refus
        resultat = subprocess.run(
            ["docker", "exec", service, "sh", "-c", _SCRIPT_PORTS],
            capture_output=True, text=True, timeout=20)
        sortie = resultat.stdout or ""
        if sortie.lstrip().startswith(_MARQUEUR_PROC):
            ecoutes = ecoutes_proc_net_tcp(sortie)
            texte = ("ports en écoute (lus dans /proc/net/tcp, ni ss ni netstat) :\n"
                     + ("\n".join(ecoutes) or "(aucun)"))
            return Observation(texte[-SORTIE_MAX:], exit_code=resultat.returncode, integrale=texte)
        return self._sortie(resultat, f"(vide, rc={resultat.returncode})")

    def _env(self, service: str) -> str:
        """Config.Env du conteneur, lue par docker inspect (sans shell dans
        l'image), valeurs secrètes masquées avant que quiconque les lise."""
        refus = self._hors_perimetre(service)
        if refus is not None:
            return refus
        resultat = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", service],
            capture_output=True, text=True, timeout=20)
        if resultat.returncode != 0:
            return self._sortie(resultat, f"(vide, rc={resultat.returncode})")
        try:
            variables = json.loads(resultat.stdout or "null") or []
        except ValueError:
            return Observation("échec de l'action : sortie de docker inspect illisible", fiable=False)
        texte = "\n".join(masquer_env(variables)) or "(aucune variable)"
        return Observation(texte[-SORTIE_MAX:], exit_code=0, integrale=texte)

    def _executer(self, demande: dict):
        """(commande lisible, Observation) d'une action de lecture."""
        action = demande.get("action")
        try:
            if action == "sonde":
                identifiants = demande.get("identifiants", "")
                return (f"GET {demande['url']}" + (" avec Authorization: Basic" if identifiants else ""),
                        self._sonde(demande["url"], identifiants))
            if action == "logs":
                return (f"docker logs --tail {demande.get('lignes', 50)} {demande['service']}",
                        self._logs(demande["service"], demande.get("lignes", 50)))
            if action == "exec":
                return (f"docker exec {demande['service']} sh -c {demande['commande']!r}",
                        self._exec(demande["service"], demande["commande"]))
            if action == "fichier":
                return (f"lecture de {demande['chemin']} dans la copie",
                        self._fichier(demande["chemin"]))
            if action == "dns":
                return (f"docker exec {demande['service']} getent hosts {demande['nom']} "
                        "; cat /etc/resolv.conf",
                        self._dns(demande["service"], demande["nom"]))
            if action == "ports":
                return (f"docker exec {demande['service']} ss -ltn (repli netstat, /proc/net/tcp)",
                        self._ports(demande["service"]))
            if action == "env":
                return (f"docker inspect --format '{{{{json .Config.Env}}}}' {demande['service']} "
                        "(secrets masqués)", self._env(demande["service"]))
            return str(demande)[:200], Observation(f"action inconnue : {action}", fiable=False)
        except Exception as erreur:
            return str(demande)[:200], Observation(f"échec de l'action : {erreur}", fiable=False)

    # --- hypothèses ----------------------------------------------------------

    @staticmethod
    def _rejet(demande: dict) -> str:
        if not str(demande.get("enonce", "")).strip():
            return "énoncé absent"
        if str(demande.get("faute", "")).strip().lower() not in FAUTES:
            return "champ faute obligatoire : apprenant, harnais ou indetermine"
        test = demande.get("test")
        if not isinstance(test, dict) or test.get("action") not in ACTIONS_LECTURE:
            return f"le test doit être une action de lecture ({', '.join(ACTIONS_LECTURE)})"
        for nom in ("si_vraie", "si_fausse"):
            erreur = _valider_prediction(demande.get(nom))
            if erreur:
                return f"{nom} : {erreur}"
        if not predictions_disjointes(demande["si_vraie"], demande["si_fausse"]):
            return ("prédictions non disjointes : si_vraie et si_fausse peuvent être vraies "
                    "ensemble, le résultat ne trancherait pas")
        return ""

    def _fiche(self, demande: dict) -> dict:
        ident = str(demande.get("id") or f"H{len(self._hypotheses) + 1}").strip()[:20]
        fiche = self._hypotheses.setdefault(ident, {"id": ident, "experiences": []})
        fiche.update({"enonce": str(demande.get("enonce", "")).strip()[:300],
                      "faute": str(demande.get("faute", "")).strip().lower(),
                      "revelable": demande.get("revelable"),
                      "non_revelable": demande.get("non_revelable")})
        fiche.pop("raison_rejet", None)
        return fiche

    def _tester_hypothese(self, demande: dict):
        """Rend (fiche, message pour le LLM). Une hypothèse rejetée n'est pas
        exécutée ; une hypothèse testée compte comme une action.

        Contrat de l'étape (lu par scriptorium#340) : titre « Hypothèse H<n> —
        <énoncé> », `command` = le test, une ligne « résultat : … » et une
        ligne « verdict : établie | réfutée | non tranchée », et la clé
        `verdict` de même valeur. Une hypothèse rejetée, jamais exécutée, a le
        verdict « non tranchée »."""
        fiche = self._fiche(demande)
        titre = f"Hypothèse {fiche['id']} — {fiche['enonce'] or '(sans énoncé)'}"[:200]
        rejet = self._rejet(demande)
        if rejet:
            if not fiche["experiences"]:
                fiche["resultat"] = "rejetee"
                fiche["raison_rejet"] = rejet
            self.runner.record_step(
                titre, output=(f"hypothèse rejetée, non exécutée : {rejet}\n"
                               "résultat : non exécutée\n"
                               f"verdict : {_LIBELLES['non_tranchee']}"),
                exit_code=0, verdict=_LIBELLES["non_tranchee"])
            return fiche, f"{fiche['id']} : rejetée, non exécutée — {rejet}"
        debut = time.time()
        commande, observation = self._executer(demande["test"])
        lu = _lire(observation)
        verdict = verdict_mecanique(demande["si_vraie"], demande["si_fausse"], lu)
        affiche = str(observation)[:SORTIE_MAX]
        lignes = correspondances([demande["si_vraie"], demande["si_fausse"]], lu["integrale"])
        brut = (f"code={self._val(lu['code'])} exit_code={self._val(lu['exit_code'])}"
                + (" (lecture tronquée : une absence n'y prouve rien)" if lu["tronquee"] else "")
                + ("" if lu["fiable"] else " (le harnais n'a rien observé)"))
        fiche["resultat"] = verdict
        fiche["experiences"].append({
            "test": commande,
            "prediction": {"si_vraie": demande["si_vraie"], "si_fausse": demande["si_fausse"]},
            "resultat": brut,
            "verdict": verdict,
            "mesures": {"code": lu["code"], "exit_code": lu["exit_code"],
                        "tronquee": lu["tronquee"], "fiable": lu["fiable"],
                        "correspondances": lignes, "extrait": affiche[-EXTRAIT_MAX:]}})
        preuve = ("lignes correspondantes :\n" + "\n".join(lignes) + "\n") if lignes else ""
        self.runner.record_step(
            titre, command=commande,
            output=(f"faute si établie : {fiche['faute']}\n"
                    f"test : {commande}\n"
                    f"si vraie : {json.dumps(demande['si_vraie'], ensure_ascii=False)}\n"
                    f"si fausse : {json.dumps(demande['si_fausse'], ensure_ascii=False)}\n"
                    f"résultat : {brut}\n"
                    f"{preuve}"
                    f"sortie :\n{affiche}\n"
                    f"verdict : {_LIBELLES[verdict]}"),
            exit_code=0, duration=time.time() - debut, verdict=_LIBELLES[verdict])
        self._observations.append((titre, commande, preuve + affiche))
        return fiche, (f"{fiche['id']} : {_LIBELLES[verdict]} ({brut})\n{preuve}"
                       f"sortie :\n{affiche}")

    @staticmethod
    def _val(valeur) -> str:
        return "-" if valeur is None else str(valeur)

    # --- boucle ------------------------------------------------------------

    def _resoudre_modele(self) -> str:
        if not self.model:
            requete = urllib.request.Request(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"})
            with urllib.request.urlopen(requete, timeout=20) as reponse:
                modeles = json.load(reponse).get("data", [])
            chats = [m["id"] for m in modeles if m.get("mode", "chat") == "chat"]
            if not chats:
                raise RuntimeError("la gateway n'expose aucun modèle chat")
            self.model = chats[0]
        return self.model

    def _appeler_llm(self, messages: list) -> str:
        self._resoudre_modele()
        charge = json.dumps({"model": self.model, "messages": messages,
                             "temperature": 0}).encode()
        requete = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=charge,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(requete, timeout=60) as reponse:
            corps = json.load(reponse)
        return corps["choices"][0]["message"]["content"]

    @staticmethod
    def _demandes(brut: str) -> list:
        """Un objet, une liste d'objets, ou {"hypotheses": [...]}."""
        lu = json.loads(brut.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
        if isinstance(lu, dict) and isinstance(lu.get("hypotheses"), list) and "action" not in lu:
            lu = lu["hypotheses"]
        demandes = lu if isinstance(lu, list) else [lu]
        if not demandes or not all(isinstance(d, dict) for d in demandes):
            raise ValueError("JSON attendu : un objet ou une liste d'objets")
        return demandes

    def _temps_ecoule(self, debut: float) -> bool:
        return time.time() - debut > self.timeout_total

    def investiguer(self, echecs: list) -> None:
        debut = time.time()
        self._observations, self._hypotheses = [], {}
        observations = self._observations
        resume_echecs = "\n".join(
            f"- {e.get('title') or e.get('titre')}: {str(e.get('output') or e.get('sortie'))[:200]}"
            for e in echecs)
        messages = [
            {"role": "system", "content": PROMPT_SYSTEME.replace("{actions_max}", str(self.actions_max))},
            {"role": "user", "content": f"Étapes en échec (non attendues) :\n{resume_echecs}\n"
                                        f"Conteneurs debout : {self._conteneurs()}\n"
                                        f"Fichiers de la copie :\n{self._inventaire()}\nCommence."},
        ]
        iterations = 0          # challenges du vérificateur déjà renvoyés au LLM
        en_attente = None       # dernier verdict contesté : jamais perdu en sortie
        observations_au_challenge = 0
        # Chaque tour est une salve d'actions, une salve d'hypothèses ou un
        # verdict. Les hypothèses rejetées ne consomment pas d'action : la
        # borne des tours garde la boucle finie.
        for _tour in range(2 * self.actions_max + 1 + self.iterations_max):
            if self._temps_ecoule(debut):
                self._interrompre("budget temps épuisé", en_attente, echecs, debut)
                return
            try:
                brut = self._appeler_llm(messages)
                demandes = self._demandes(brut)
            except Exception as erreur:
                self._interrompre(f"réponse LLM inexploitable : {erreur}", en_attente, echecs, debut)
                return
            if len(demandes) == 1 and demandes[0].get("action") == "verdict":
                demande = demandes[0]
                if self.scoreur is None:
                    self.runner.record_step(
                        "Vérification du verdict non configurée",
                        output="PI_CORRECTOR_VERIFY_BASE_URL absente : verdict accepté "
                               "sans vérification indépendante de la cause",
                        exit_code=0)
                    if str(demande.get("faute", "")).strip().lower() == "indetermine":
                        self._non_etablie(echecs, debut, non_revelable=demande.get("non_revelable"))
                    else:
                        self._consigner_verdict(demande, debut, "cause_non_verifiee")
                    return
                if str(demande.get("faute", "")).strip().lower() == "indetermine":
                    self.runner.record_step(
                        "Investigation — vérification du verdict",
                        output="faute indetermine : cause non scorée, le verdict est déjà prudent",
                        exit_code=0)
                    self._non_etablie(echecs, debut, non_revelable=demande.get("non_revelable"))
                    return
                if en_attente is not None and len(observations) == observations_au_challenge:
                    self._declasser(demande, echecs, debut,
                                    "verdict rendu sans observation nouvelle depuis le "
                                    "challenge : reformuler avec la même preuve ne compte pas")
                    return
                try:
                    distribution = self.scoreur(self._preuves(echecs, observations),
                                                str(demande.get("cause", "")))
                except Exception as erreur:
                    self._declasser(demande, echecs, debut, f"vérificateur en erreur : {erreur}")
                    return
                p = float(distribution.get("soutenu", 0.0))
                lecture = (f"affirmation : {demande.get('cause')}\n"
                           f"P(soutenu)={p:.2f}, seuil {self.seuil:.2f}\n"
                           f"distribution : {self._distribution(distribution)}")
                if p >= self.seuil:
                    self.runner.record_step("Investigation — vérification du verdict",
                                            output=f"{lecture}\ncause établie : verdict accepté",
                                            exit_code=0)
                    self._consigner_verdict(demande, debut, "cause_scoree")
                    return
                if (iterations < self.iterations_max and len(observations) < self.actions_max
                        and not self._temps_ecoule(debut)):
                    iterations += 1
                    en_attente, observations_au_challenge = demande, len(observations)
                    self.runner.record_step(
                        "Investigation — vérification du verdict",
                        output=f"{lecture}\ncause non établie : réinvestigation "
                               f"{iterations}/{self.iterations_max}",
                        exit_code=0)
                    messages.append({"role": "assistant", "content": brut})
                    messages.append({"role": "user", "content":
                        f"La cause n'est pas établie par tes observations (P={p:.2f}). "
                        "Fais une action qui la prouve ou la réfute, puis rends un nouveau "
                        "verdict. Reformuler la même cause sans observation nouvelle ne compte pas."})
                    continue
                self._declasser(demande, echecs, debut,
                                f"{lecture}\ncause non établie, budget de vérification épuisé")
                return
            retours = []
            for demande in demandes:
                if len(observations) >= self.actions_max or self._temps_ecoule(debut):
                    break
                action = demande.get("action")
                if action == "verdict":
                    retours.append("verdict ignoré : un verdict se rend seul, après les résultats")
                    continue
                if action == "hypothese":
                    fiche, retour = self._tester_hypothese(demande)
                    retours.append(retour)
                    if fiche.get("resultat") == "etablie":
                        self._conclure_etablie(fiche, debut)
                        return
                    continue
                commande, observation = self._executer(demande)
                titre = f"Investigation {len(observations) + 1} : {action}"
                self.runner.record_step(titre, command=commande,
                                        output=observation[:SORTIE_MAX], exit_code=0)
                observations.append((titre, commande, observation[:SORTIE_MAX]))
                retours.append(f"Observation :\n{observation[:SORTIE_MAX]}")
            messages.append({"role": "assistant", "content": brut})
            messages.append({"role": "user", "content":
                "\n\n".join(retours) or "Aucune action exécutée : budget épuisé."})
            if self._temps_ecoule(debut):
                self._interrompre("budget temps épuisé", en_attente, echecs, debut)
                return
            if len(observations) >= self.actions_max:
                break
        if en_attente is not None:
            self._declasser(en_attente, echecs, debut,
                            "budget d'actions épuisé avant un nouveau verdict")
            return
        self._non_etablie(echecs, debut, entete="budget d'actions épuisé sans verdict : "
                                                "cause non établie", exit_code=1)

    # --- verdict -------------------------------------------------------------

    def _conclure_etablie(self, fiche: dict, debut: float) -> None:
        experience = fiche["experiences"][-1]
        self.runner.record_step(
            "Investigation — vérification du verdict",
            output=(f"cause établie mécaniquement par l'hypothèse {fiche['id']} "
                    f"(test : {experience['test']}) : scoreur non sollicité"),
            exit_code=0)
        self._consigner_verdict(
            {"cause": fiche["enonce"], "faute": fiche["faute"],
             "revelable": fiche.get("revelable") or fiche["enonce"],
             "non_revelable": fiche.get("non_revelable") or ""},
            debut, "cause_etablie", hypothese_etablie=fiche["id"])

    def _non_etablie(self, echecs: list, debut: float, entete: str = "",
                     exit_code: int = 0, non_revelable=None) -> None:
        self._consigner_verdict(
            {"cause": None, "faute": "indetermine", "revelable": self._symptome(echecs),
             "non_revelable": non_revelable or ""},
            debut, "cause_non_etablie", entete=entete, exit_code=exit_code)

    def _consigner_verdict(self, verdict: dict, debut: float, statut: str, *,
                           hypothese_etablie=None, piste=None, entete: str = "",
                           exit_code: int = 0) -> None:
        cause = verdict.get("cause")
        lignes = [entete] if entete else []
        if statut == "cause_non_etablie" and "cause non établie" not in entete:
            lignes.append("cause non établie")
        lignes += [f"cause : {'non établie' if cause is None else cause}",
                   f"faute : {verdict.get('faute')}",
                   f"révélable au feedback : {verdict.get('revelable')}",
                   f"à ne pas révéler : {verdict.get('non_revelable')}"]
        fiches = list(self._hypotheses.values())
        if fiches:
            lignes.append("hypothèses :")
            for fiche in fiches:
                lignes.append(f"- {fiche['id']} — {fiche.get('enonce')} : "
                              f"{_LIBELLES.get(fiche.get('resultat'), fiche.get('resultat'))}"
                              + (f" ({fiche['raison_rejet']})" if fiche.get("raison_rejet") else ""))
                for experience in fiche["experiences"]:
                    lignes.append(f"    · {experience['test']} → {experience['resultat']} : "
                                  f"{_LIBELLES[experience['verdict']]}")

        def publique(fiche):
            garde = {"id": fiche["id"], "enonce": fiche.get("enonce"), "faute": fiche.get("faute"),
                     "resultat": fiche.get("resultat"), "experiences": fiche["experiences"]}
            if fiche.get("raison_rejet"):
                garde["raison_rejet"] = fiche["raison_rejet"]
            return garde

        bloc = {"version": 1, "statut": statut,
                "cause": None if statut == "cause_non_etablie" else cause, "piste": piste,
                "faute": verdict.get("faute"), "revelable": verdict.get("revelable"),
                "non_revelable": verdict.get("non_revelable"),
                "hypothese_etablie": hypothese_etablie,
                "hypotheses_restantes": [publique(f) for f in fiches
                                         if f.get("resultat") in ("non_tranchee", "rejetee")],
                "hypotheses_refutees": [publique(f) for f in fiches
                                        if f.get("resultat") == "refutee"]}
        # Une seule ligne entre les délimiteurs : moins fragile si un rendu
        # aval coupe ou replie la sortie.
        lignes.append("bilan des hypothèses (JSON) :\n```json\n"
                      + json.dumps(bloc, ensure_ascii=False) + "\n```")
        self.runner.record_step("Investigation — verdict", output="\n".join(lignes),
                                exit_code=exit_code, duration=time.time() - debut,
                                statut=statut,
                                hypotheses_restantes=bloc["hypotheses_restantes"])

    def _declasser(self, verdict: dict, echecs: list, debut: float, raison: str) -> None:
        """Une cause que le vérificateur n'a pas vue établie devient une
        hypothèse, et le feedback n'en dit que le symptôme."""
        cause = str(verdict.get("cause", ""))
        piste = cause.removeprefix(PREFIXE_HYPOTHESE)
        self.runner.record_step("Investigation — vérification du verdict",
                                output=f"{raison}\nverdict déclassé en hypothèse, faute indetermine",
                                exit_code=0)
        self._consigner_verdict({"cause": PREFIXE_HYPOTHESE + piste, "faute": "indetermine",
                                 "revelable": self._symptome(echecs),
                                 "non_revelable": verdict.get("non_revelable")},
                                debut, "cause_non_etablie", piste=piste)

    def _interrompre(self, raison: str, en_attente, echecs: list, debut: float) -> None:
        self.runner.record_step("Investigation interrompue", output=raison, exit_code=1)
        if en_attente is not None:
            self._declasser(en_attente, echecs, debut, f"investigation interrompue : {raison}")
        elif self._hypotheses:
            # Les expériences déjà faites partent en revue humaine.
            self._non_etablie(echecs, debut, entete=f"investigation interrompue : {raison}",
                              exit_code=1)

    @staticmethod
    def _symptome(echecs: list) -> str:
        lignes = []
        for e in echecs[:3]:
            sortie = str(e.get("output") or e.get("sortie") or "").strip()
            premiere = sortie.splitlines()[0][:160] if sortie else "(sans sortie)"
            lignes.append(f"{e.get('title') or e.get('titre')} → {premiere}")
        return "symptôme observé : " + " ; ".join(lignes)

    @staticmethod
    def _preuves(echecs: list, observations: list) -> str:
        """Contexte étroit du vérificateur : les échecs, puis ce que
        l'investigation a observé. Au-delà de PREUVES_MAX, on garde la tête
        des échecs et la fin des observations, les plus récentes."""
        def tronquer_fin(texte, n):
            return texte if len(texte) <= n else "[…]" + texte[-n:]

        partie_echecs = "\n\n".join(
            f"### {e.get('title') or e.get('titre')} (exit {e.get('exit_code')})\n"
            f"{tronquer_fin(str(e.get('output') or e.get('sortie') or ''), 1000)}"
            for e in echecs)[:PREUVES_MAX // 3]
        partie_observations = "\n\n".join(
            f"### {titre}\n$ {commande}\n{sortie}" for titre, commande, sortie in observations)
        return (partie_echecs + "\n\n"
                + tronquer_fin(partie_observations, PREUVES_MAX - len(partie_echecs))).strip()

    @staticmethod
    def _distribution(distribution: dict) -> str:
        if not distribution:
            return "(aucune lettre de verdict dans les logprobs)"
        return " · ".join(f"{nom} {p:.2f}" for nom, p in
                          sorted(distribution.items(), key=lambda item: -item[1]))

    def _inventaire(self) -> str:
        """Les chemins réels de la copie : l'action « fichier » ne devine pas."""
        chemins = []
        for racine, dossiers, fichiers in os.walk(self.eval_dir):
            dossiers[:] = [d for d in dossiers if d not in (".git", ".venv", "__pycache__", "node_modules")]
            for nom in fichiers:
                chemins.append(os.path.relpath(os.path.join(racine, nom), self.eval_dir))
                if len(chemins) >= 60:
                    return "\n".join(chemins) + "\n(tronqué)"
        return "\n".join(chemins)

    def _conteneurs(self) -> str:
        return ", ".join(self.services) or "(aucun)"
