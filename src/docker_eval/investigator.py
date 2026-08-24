"""Phase d'investigation outillée, pendant que la stack tourne encore.

Quand une étape échoue sans annotation « attendu », un LLM (gateway
OpenAI-compatible, typiquement Liora) mène une investigation bornée en
lecture seule : re-sonder une URL, lire les logs d'un conteneur, exécuter
une commande de lecture, lire un fichier de la copie. Chaque action et son
observation deviennent des étapes du contrat — visibles au scratchpad,
citables par la revue. Après le teardown, plus rien n'est testable : c'est
ici que le vrai debug se joue (scriptorium #78).
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

ACTIONS_MAX = 6
TIMEOUT_TOTAL_SECONDES = 240
SORTIE_MAX = 2000

# ponytail: liste noire lexicale, pas un sandbox — le vrai garde-fou est
# l'exec sans écriture possible sur la copie (elle vit hors conteneur) et
# le budget d'actions. Durcir en whitelist si un exam l'exige.
_EXEC_INTERDITS = (">", ">>", "rm ", "mv ", "cp ", "chmod", "chown", "kill",
                   "shutdown", "reboot", "mkfs", "dd ", "wget", "curl -o", "tee")

PROMPT_SYSTEME = """Tu investigues l'échec d'une évaluation d'examen pendant que la stack Docker tourne encore.
Tu réponds UNIQUEMENT par un objet JSON, sans texte autour. Actions disponibles :
{"action":"sonde","url":"http(s)://127.0.0.1:<port>/<chemin>"} — refaire une requête, chemins et schémas libres
{"action":"logs","service":"<nom de conteneur>","lignes":50} — lire la fin des logs
{"action":"exec","service":"<nom de conteneur>","commande":"<commande de LECTURE>"} — ex. cat d'une config effective
{"action":"fichier","chemin":"<chemin relatif dans la copie>"} — lire un fichier rendu par l'apprenant
{"action":"verdict","cause":"<cause établie ou 'non établie'>","faute":"apprenant|harnais|indetermine","revelable":"<ce que le feedback peut dire : symptôme, où chercher>","non_revelable":"<ce qu'il ne faut pas donner : la solution>"}
Méthode : formule une hypothèse, teste-la par UNE action, lis l'observation, itère. Termine par "verdict"
dès que la cause est établie ou qu'aucune action ne peut plus trancher. C'est un examen : le verdict guide
sans jamais donner la correction. Budget strict : {actions_max} actions.
"""


class Investigator:
    """Boucle d'investigation adossée au runner (record_step, conteneurs)."""

    def __init__(self, runner, eval_dir: str, services: list):
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
        self.model = os.environ.get("PI_CORRECTOR_INVESTIGATE_MODEL", "gpt-4o-mini")

    def disponible(self) -> bool:
        return bool(self.base_url and self.api_key)

    # --- actions -----------------------------------------------------------

    def _sonde(self, url: str) -> str:
        if "127.0.0.1" not in url and "localhost" not in url:
            return "refusé : seules les URLs locales (127.0.0.1) sont sondables"
        import ssl
        contexte = ssl.create_default_context()
        contexte.check_hostname = False
        contexte.verify_mode = ssl.CERT_NONE
        try:
            reponse = urllib.request.urlopen(
                url, timeout=10, context=contexte if url.startswith("https") else None)
            return f"code {reponse.status}\n{reponse.read(400).decode('utf-8', 'replace')}"
        except urllib.error.HTTPError as erreur:
            return f"code {erreur.code}\n{erreur.read(400).decode('utf-8', 'replace')}"
        except Exception as erreur:
            return f"non reçu : {erreur}"

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
        if any(interdit in commande for interdit in _EXEC_INTERDITS):
            return "refusé : commande d'écriture ou de réseau — lecture seule"
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

    def _appeler_llm(self, messages: list) -> str:
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
                                        f"Conteneurs debout : {self._conteneurs()}\nCommence."},
        ]
        for numero in range(1, ACTIONS_MAX + 1):
            if time.time() - debut > TIMEOUT_TOTAL_SECONDES:
                self.runner.record_step("Investigation interrompue",
                                        output="budget temps épuisé", exit_code=1)
                return
            try:
                brut = self._appeler_llm(messages)
                demande = json.loads(brut.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
            except Exception as erreur:
                self.runner.record_step("Investigation interrompue",
                                        output=f"réponse LLM inexploitable : {erreur}", exit_code=1)
                return
            action = demande.get("action")
            if action == "verdict":
                self.runner.record_step(
                    "Investigation — verdict",
                    output=(f"cause : {demande.get('cause')}\n"
                            f"faute : {demande.get('faute')}\n"
                            f"révélable au feedback : {demande.get('revelable')}\n"
                            f"à ne pas révéler : {demande.get('non_revelable')}"),
                    exit_code=0, duration=time.time() - debut)
                return
            try:
                if action == "sonde":
                    observation = self._sonde(demande["url"])
                    commande = f"GET {demande['url']}"
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
            self.runner.record_step(f"Investigation {numero} : {action}",
                                    command=commande, output=observation[:SORTIE_MAX], exit_code=0)
            messages.append({"role": "assistant", "content": brut})
            messages.append({"role": "user", "content": f"Observation :\n{observation[:SORTIE_MAX]}"})
        self.runner.record_step("Investigation — verdict",
                                output="budget d'actions épuisé sans verdict : cause non établie",
                                exit_code=1, duration=time.time() - debut)

    def _conteneurs(self) -> str:
        return ", ".join(self.services) or "(aucun)"
