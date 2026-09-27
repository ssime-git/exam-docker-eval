"""Phase d'investigation outillée, pendant que la stack tourne encore.

Quand une étape échoue sans annotation « attendu », un LLM (gateway
OpenAI-compatible, typiquement Liora) mène une investigation bornée en
lecture seule : re-sonder une URL, lire les logs d'un conteneur, exécuter
une commande de lecture, lire un fichier de la copie. Chaque action et son
observation deviennent des étapes du contrat — visibles au scratchpad,
citables par la revue. Après le teardown, plus rien n'est testable : c'est
ici que le vrai debug se joue (scriptorium #78).

Verdict vérifié (scriptorium #307). Un verdict qui impute une faute
(`faute` ≠ `indetermine`) n'est pas accepté sur la parole du LLM : sa `cause`
est scorée par un vérificateur indépendant contre les seules preuves de
l'investigation (sorties des étapes en échec et observations « Investigation
N »). Le vérificateur est un scoring de logprobs sur quatre verdicts, même
prompt que `score_claim` de scriptorium/tools/verification.py ; le banc #304
a retenu Qwen3.5-4B Q4_K_M derrière llama-server.

- P(soutenu) ≥ seuil : verdict accepté, la distribution est consignée.
- Sous le seuil, avec des actions et du temps restants et moins de
  MAX_ITERATIONS challenges : le LLM reçoit « la cause n'est pas établie par
  tes observations » et doit agir. Un verdict rendu sans observation nouvelle
  depuis le challenge ne compte pas (#306) : il est déclassé.
- Sinon, et sur toute sortie de boucle avec un verdict contesté en attente
  (budget, réponse inexploitable, vérificateur en erreur) : verdict déclassé,
  cause préfixée « hypothèse : », `faute: indetermine`, `revelable` réduit au
  symptôme observé.

Environnement (contrat de scriptorium #311) : PI_CORRECTOR_VERIFY_BASE_URL
(base OpenAI-compatible, `/v1` compris ; absente = vérification désactivée,
et l'étape « Vérification du verdict non configurée » le dit),
PI_CORRECTOR_VERIFY_API_KEY (Bearer, optionnel), PI_CORRECTOR_VERIFY_MODEL
(optionnel, ignoré par llama-server), PI_CORRECTOR_VERIFY_THRESHOLD (0.55),
PI_CORRECTOR_VERIFY_MAX_ITERATIONS (2).

Budget. ACTIONS_MAX borne les actions ; les tours de verdict ne le
consomment plus (au plus 1 + MAX_ITERATIONS). TIMEOUT_TOTAL_SECONDES borne
les actions et les ré-investigations, pas le scoring : le verdict final est
toujours scoré. Budget additionnel assumé : au plus 1 + MAX_ITERATIONS appels
au vérificateur, chacun borné par VERIFICATION_TIMEOUT_SECONDES (le banc
mesure 15 à 26 s par appel sur CPU).

Trou connu, non comblé ici : l'investigateur ne tourne que dans le runner
compose (ComposeRunner, services persistants), et seulement quand des étapes
sont en échec. Une extraction ratée de la copie, ou les autres runners
(conteneur unique, image, bentoml…), ne sont jamais investigués : leurs
causes runtime restent sans verdict, donc sans vérification.
"""

import functools
import json
import math
import os
import subprocess
import time
import urllib.error
import urllib.request

ACTIONS_MAX = 6
TIMEOUT_TOTAL_SECONDES = 240
SORTIE_MAX = 2000

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

# Liste blanche : l'action `exec` ne lance que des lectures. Rejeu du 25/09
# (461451) : un test proposé par le LLM a exécuté `apt-get install` dans le
# conteneur de l'apprenant, que l'ancienne liste noire lexicale laissait passer
# (apt-get, pip, sed -i, python -c…). Une commande est un ou plusieurs
# segments reliés par `|`, chacun ouvert par un programme de lecture, sans
# enchaînement, redirection ni substitution.
_LECTURES = {
    "cat", "head", "tail", "ls", "grep", "egrep", "wc", "sort", "uniq", "cut",
    "ps", "env", "printenv", "getent", "nslookup", "dig", "ss", "netstat",
    "id", "whoami", "df", "du", "uname", "hostname", "date", "stat", "which",
    "find", "curl", "nginx", "python", "python3", "pip", "pip3",
}
_METACARACTERES = (";", "&&", "||", ">", "<", "`", "$(", "\n", "&")
_OPTIONS_INTERDITES = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"},
    "curl": {"-o", "-O", "--output", "--remote-name", "-X", "--request", "-d", "--data",
             "--data-raw", "--data-binary", "--data-urlencode", "-T", "--upload-file",
             "-F", "--form", "-K", "--config"},
}
_ARGUMENTS_AUTORISES = {
    # Pour ces programmes, seuls ces premiers arguments sont des lectures.
    "nginx": {"-T", "-t", "-v", "-V"},
    "python": {"--version", "-V"},
    "python3": {"--version", "-V"},
    "pip": {"list", "show", "freeze", "--version", "-V"},
    "pip3": {"list", "show", "freeze", "--version", "-V"},
}


def _arguments_dangereux(programme: str, args: list[str]) -> str | None:
    """Programmes de lecture qui écrivent ou exécutent selon leurs arguments."""
    import re
    if programme == "env" and args:
        # `env PROG` exécute PROG : seul `env` nu est une lecture.
        return "env : sans argument seulement (env PROG exécute PROG)"
    if programme == "printenv" and any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", a) for a in args):
        return "printenv : noms de variables seulement"
    if programme == "sort" and any(a == "-o" or a.startswith("--output") or (a.startswith("-") and not a.startswith("--") and "o" in a[1:]) for a in args):
        return "sort -o écrit un fichier"
    if programme == "uniq" and len([a for a in args if not a.startswith("-")]) > 1:
        return "uniq ENTRÉE SORTIE écrit un fichier"
    if programme == "date" and any(not a.startswith("+") for a in args):
        return "date : format d'affichage (+…) seulement"
    if programme == "hostname" and any(a not in {"-f", "-i", "-I", "-s", "-d"} for a in args):
        return "hostname : lecture seulement"
    if programme == "ss" and any(a in {"-K", "--kill"} or (a.startswith("-") and not a.startswith("--") and "K" in a[1:]) for a in args):
        return "ss -K coupe des connexions"
    return None


def commande_de_lecture(commande: str) -> str | None:
    """None si la commande n'est qu'une lecture, sinon la raison du refus."""
    import shlex
    if not commande or not commande.strip():
        return "commande vide"
    if "\n" in commande:
        return "une seule ligne de commande"
    for meta in _METACARACTERES:
        if meta in commande:
            return f"« {meta} » interdit : ni enchaînement, ni redirection, ni substitution"
    for segment in commande.split("|"):
        try:
            mots = shlex.split(segment)
        except ValueError:
            return "commande mal formée"
        if not mots:
            return "segment vide"
        programme = mots[0].rsplit("/", 1)[-1]
        if programme not in _LECTURES:
            return f"« {programme} » n'est pas un programme de lecture"
        interdites = _OPTIONS_INTERDITES.get(programme, set())
        for mot in mots[1:]:
            option = mot.split("=", 1)[0]
            if option in interdites or (programme == "curl" and option.startswith("-") and not option.startswith("--")
                                        and any(c in option[1:] for c in "oOXdTFK")):
                return f"option « {mot} » de {programme} : écriture ou envoi"
        refus = _arguments_dangereux(programme, mots[1:])
        if refus:
            return refus
        autorises = _ARGUMENTS_AUTORISES.get(programme)
        if autorises is not None and (len(mots) < 2 or mots[1] not in autorises):
            return f"{programme} : seules les lectures {sorted(autorises)} sont permises"
    return None

PROMPT_SYSTEME = """Tu investigues l'échec d'une évaluation d'examen pendant que la stack Docker tourne encore.
Tu réponds UNIQUEMENT par un objet JSON, sans texte autour. Actions disponibles :
{"action":"sonde","url":"http(s)://127.0.0.1:<port>/<chemin>","identifiants":"user:pass"} — refaire une requête, chemins et schémas libres ; "identifiants" (optionnel) ajoute une authentification Basic
{"action":"logs","service":"<nom de conteneur>","lignes":50} — lire la fin des logs
{"action":"exec","service":"<nom de conteneur>","commande":"<commande de LECTURE>"} — ex. cat d'une config effective
{"action":"fichier","chemin":"<chemin relatif dans la copie>"} — lire un fichier rendu par l'apprenant
{"action":"verdict","cause":"<cause établie ou 'non établie'>","faute":"apprenant|harnais|indetermine","revelable":"<ce que le feedback peut dire : symptôme, où chercher>","non_revelable":"<ce qu'il ne faut pas donner : la solution>"}
Méthode : formule une hypothèse, teste-la par UNE action, lis l'observation, itère. Comble les trous
de la soumission avant de conclure : une route qui répond 401 se traite en CHERCHANT les identifiants
que l'apprenant a fournis (ses tests, son README, ses scripts — action "fichier") puis en re-sondant
avec ("sonde" + "identifiants"). Ne conclus jamais « identifiants absents ou refusés » sans avoir lu
les fichiers où ils pourraient être. Termine par "verdict"
dès que la cause est établie ou qu'aucune action ne peut plus trancher. C'est un examen : le verdict guide
sans jamais donner la correction. Cohérence exigée : "faute" ne peut valoir "apprenant" ou "harnais" QUE si
"cause" est établie par une observation ; une cause non établie impose "faute":"indetermine" et un
"revelable" qui dit seulement le symptôme observé. Budget strict : {actions_max} actions.
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

    def disponible(self) -> bool:
        return bool(self.base_url and self.api_key)

    # --- actions -----------------------------------------------------------

    def _sonde(self, url: str, identifiants: str = "") -> str:
        if "127.0.0.1" not in url and "localhost" not in url:
            return "refusé : seules les URLs locales (127.0.0.1) sont sondables"
        from .sonde_http import resume, sonder
        entetes = {}
        if identifiants:
            import base64
            entetes["Authorization"] = "Basic " + base64.b64encode(identifiants.encode()).decode()
        # Même règle que les sondes du runner : la chaîne de redirections est
        # consignée, une cible injoignable n'est pas une panne (#320).
        sonde = sonder(url, entetes=entetes, taille_extrait=400)
        if not isinstance(sonde["code"], int):
            return f"non reçu : {sonde['erreur']}"
        return f"{resume(sonde)}\n{sonde['extrait']}"

    def _logs(self, service: str, lignes: int) -> str:
        if service not in self.services:
            return "refusé : conteneur hors du périmètre de la copie"
        resultat = subprocess.run(
            ["docker", "logs", "--tail", str(min(int(lignes or 50), 200)), service],
            capture_output=True, text=True, timeout=20)
        return (resultat.stdout + resultat.stderr)[-SORTIE_MAX:] or "(logs vides)"

    def _exec(self, service: str, commande: str) -> str:
        if service not in self.services:
            return "refusé : conteneur hors du périmètre de la copie"
        refus = commande_de_lecture(commande)
        if refus:
            return f"refusé : lecture seule — {refus}"
        resultat = subprocess.run(
            ["docker", "exec", service, "sh", "-c", commande],
            capture_output=True, text=True, timeout=20)
        return (resultat.stdout + resultat.stderr)[-SORTIE_MAX:] or f"(vide, rc={resultat.returncode})"

    def _fichier(self, chemin: str) -> str:
        cible = os.path.realpath(os.path.join(self.eval_dir, chemin))
        if not cible.startswith(self.eval_dir + os.sep):
            return "refusé : chemin hors de la copie"
        if not os.path.isfile(cible):
            return "fichier introuvable"
        with open(cible, encoding="utf-8", errors="replace") as lecteur:
            return lecteur.read(SORTIE_MAX)

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

    def investiguer(self, echecs: list) -> None:
        debut = time.time()
        resume_echecs = "\n".join(
            f"- {e.get('title') or e.get('titre')}: {str(e.get('output') or e.get('sortie'))[:200]}"
            for e in echecs)
        messages = [
            {"role": "system", "content": PROMPT_SYSTEME.replace("{actions_max}", str(ACTIONS_MAX))},
            {"role": "user", "content": f"Étapes en échec (non attendues) :\n{resume_echecs}\n"
                                        f"Conteneurs debout : {self._conteneurs()}\n"
                                        f"Fichiers de la copie :\n{self._inventaire()}\nCommence."},
        ]
        observations = []       # (titre, commande, sortie) des actions menées
        iterations = 0          # challenges du vérificateur déjà renvoyés au LLM
        en_attente = None       # dernier verdict contesté : jamais perdu en sortie
        observations_au_challenge = 0
        # Chaque tour est une action ou un verdict ; les verdicts sont au plus
        # 1 + iterations_max, les actions au plus ACTIONS_MAX.
        for _tour in range(ACTIONS_MAX + 1 + self.iterations_max):
            if time.time() - debut > TIMEOUT_TOTAL_SECONDES:
                self._interrompre("budget temps épuisé", en_attente, echecs, debut)
                return
            try:
                brut = self._appeler_llm(messages)
                demande = json.loads(brut.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
            except Exception as erreur:
                self._interrompre(f"réponse LLM inexploitable : {erreur}", en_attente, echecs, debut)
                return
            action = demande.get("action")
            if action == "verdict":
                if self.scoreur is None:
                    self.runner.record_step(
                        "Vérification du verdict non configurée",
                        output="PI_CORRECTOR_VERIFY_BASE_URL absente : verdict accepté "
                               "sans vérification indépendante de la cause",
                        exit_code=0)
                    self._consigner_verdict(demande, debut)
                    return
                if str(demande.get("faute", "")).strip().lower() == "indetermine":
                    self.runner.record_step(
                        "Investigation — vérification du verdict",
                        output="faute indetermine : cause non scorée, le verdict est déjà prudent",
                        exit_code=0)
                    self._consigner_verdict(demande, debut)
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
                    self._consigner_verdict(demande, debut)
                    return
                if (iterations < self.iterations_max and len(observations) < ACTIONS_MAX
                        and time.time() - debut <= TIMEOUT_TOTAL_SECONDES):
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
            try:
                if action == "sonde":
                    observation = self._sonde(demande["url"], demande.get("identifiants", ""))
                    commande = f"GET {demande['url']}" + (" avec Authorization: Basic" if demande.get("identifiants") else "")
                elif action == "logs":
                    observation = self._logs(demande["service"], demande.get("lignes", 50))
                    commande = f"docker logs --tail {demande.get('lignes', 50)} {demande['service']}"
                elif action == "exec":
                    observation = self._exec(demande["service"], demande["commande"])
                    commande = f"docker exec {demande['service']} sh -c {demande['commande']!r}"
                elif action == "fichier":
                    observation = self._fichier(demande["chemin"])
                    commande = f"lecture de {demande['chemin']} dans la copie"
                else:
                    observation = f"action inconnue : {action}"
                    commande = str(demande)[:200]
            except Exception as erreur:
                observation, commande = f"échec de l'action : {erreur}", str(demande)[:200]
            titre = f"Investigation {len(observations) + 1} : {action}"
            self.runner.record_step(titre, command=commande,
                                    output=observation[:SORTIE_MAX], exit_code=0)
            observations.append((titre, commande, observation[:SORTIE_MAX]))
            messages.append({"role": "assistant", "content": brut})
            messages.append({"role": "user", "content": f"Observation :\n{observation[:SORTIE_MAX]}"})
            if len(observations) >= ACTIONS_MAX:
                break
        if en_attente is not None:
            self._declasser(en_attente, echecs, debut,
                            "budget d'actions épuisé avant un nouveau verdict")
            return
        self.runner.record_step("Investigation — verdict",
                                output="budget d'actions épuisé sans verdict : cause non établie",
                                exit_code=1, duration=time.time() - debut)

    # --- verdict -------------------------------------------------------------

    def _consigner_verdict(self, verdict: dict, debut: float) -> None:
        self.runner.record_step(
            "Investigation — verdict",
            output=(f"cause : {verdict.get('cause')}\n"
                    f"faute : {verdict.get('faute')}\n"
                    f"révélable au feedback : {verdict.get('revelable')}\n"
                    f"à ne pas révéler : {verdict.get('non_revelable')}"),
            exit_code=0, duration=time.time() - debut)

    def _declasser(self, verdict: dict, echecs: list, debut: float, raison: str) -> None:
        """Une cause que le vérificateur n'a pas vue établie devient une
        hypothèse, et le feedback n'en dit que le symptôme."""
        cause = str(verdict.get("cause", ""))
        if not cause.startswith(PREFIXE_HYPOTHESE):
            cause = PREFIXE_HYPOTHESE + cause
        self.runner.record_step("Investigation — vérification du verdict",
                                output=f"{raison}\nverdict déclassé en hypothèse, faute indetermine",
                                exit_code=0)
        self._consigner_verdict({"cause": cause, "faute": "indetermine",
                                 "revelable": self._symptome(echecs),
                                 "non_revelable": verdict.get("non_revelable")}, debut)

    def _interrompre(self, raison: str, en_attente, echecs: list, debut: float) -> None:
        self.runner.record_step("Investigation interrompue", output=raison, exit_code=1)
        if en_attente is not None:
            self._declasser(en_attente, echecs, debut, f"investigation interrompue : {raison}")

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
