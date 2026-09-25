# exam-docker-eval

Le moteur d'évaluation en sandbox partagé par les skills correcteurs : `bentoml`, `nginx`, `linux-bash`, `prometheus-grafana`.

Il était auparavant **dupliqué dans chacun des quatre dépôts** — 3 157 lignes en quatre exemplaires. Un correctif devait donc être appliqué quatre fois, et les copies avaient déjà divergé.

## Ce qu'il fait

Il prend un rendu d'apprenant déjà extrait, l'exécute dans des conteneurs jetables, et rend un résultat structuré : ce qui a tourné, ce que ça a produit, à qui imputer un échec.

| module | rôle |
|---|---|
| `bentoml_runner` | image Docker livrée ou `.bento` à conteneuriser |
| `compose_runner` | `docker-compose.yml` fourni par l'apprenant |
| `bento_compiled_runner` | `.bento` compilé |
| `base_runner` | socle commun, dont la trace pas à pas |
| `utils` | nettoyage et vérification des ressources |
| `environnement` | faits d'environnement du harnais (Python des tests, version déclarée par la copie, images, plateforme, limites), consignés dans l'étape « Environnement du harnais » |
| `investigator` | investigation outillée pendant que la stack tourne : débogage par hypothèse, verdict mécanique ou scoré |
| `config` | délais, limites de ressources, codes de sortie |

## Ce qu'il garantit

- **Le nettoyage est garanti**, y compris sur plantage et signaux. Les diagnostics sont capturés *avant* le démontage, jamais en le différant.
- **Les ports hôte sont éphémères** et relus après coup. Tester la disponibilité d'un port ne fonctionne pas depuis un conteneur : le bind se fait sur l'hôte, pas sur le loopback du processus.
- **Une image d'une autre architecture est exécutée sous émulation** si `binfmt_misc` expose des gestionnaires QEMU. Un apprenant qui construit sur un Mac Apple Silicon n'est pas pénalisé.
- **Un échec est attribué** : `apprenant`, `systeme`, ou `indetermine`. Dans le doute on ne tranche pas — un REPASS envoyé sur une supposition est irréversible.
- **Le temps rapporté est le temps écoulé**, jamais le délai configuré.

## Investigation par hypothèse (scriptorium #336)

Quand une étape du runner compose échoue, un LLM enquête pendant que la stack tourne encore. Il conçoit des expériences et le harnais les juge. Le LLM ne décide jamais du résultat.

Une hypothèse porte un test et deux prédictions :

```json
{"action":"hypothese","id":"H1","enonce":"le DNS imposé ne résout pas l'hôte du dataset",
 "faute":"apprenant",
 "test":{"action":"dns","service":"bike-api","nom":"archive.ics.uci.edu"},
 "si_vraie":{"exit_code":2},"si_fausse":{"exit_code":0}}
```

- **Le test** est une action de lecture, bornée aux conteneurs de la copie : `sonde`, `logs`, `exec` (liste noire d'écriture), `fichier`, `dns` (`getent hosts` dans le conteneur, puis son `resolv.conf`), `ports` (`ss -ltn`, sinon `netstat`, sinon `/proc/net/tcp` décodé), `env` (`docker inspect`, valeurs des clés contenant KEY, TOKEN, SECRET ou PASS masquées).
- **Une prédiction** combine `code` (HTTP), `exit_code`, `contient` et `ne_contient_pas`. Les sous-chaînes sont cherchées dans la sortie complète, pas dans l'extrait affiché.
- **Hypothèse rejetée, non exécutée** : `faute` absente, test qui n'est pas une lecture, ou prédictions qui peuvent être vraies ensemble.
- **Verdict mécanique** : `établie` si seule `si_vraie` tient, `réfutée` si seule `si_fausse` tient, `non tranchée` sinon. Une donnée absente ou un refus du harnais donne toujours `non tranchée`.

Chaque expérience devient une étape « Hypothèse H1 — énoncé ». La première hypothèse établie donne la `cause` et la `faute` du verdict, sans passer par le scoreur de #307. Sinon le verdict est « cause : non établie », `faute: indetermine`, et le révélable se réduit au symptôme observé. Un verdict direct `{"action":"verdict",…}` reste accepté et passe par le scoreur comme avant.

La sortie de l'étape « Investigation — verdict » se termine toujours par un bloc ` ```json ` (scriptorium #338 et pi-corrector #337 le lisent) :

```json
{"version": 1,
 "statut": "cause_etablie | cause_scoree | cause_non_verifiee | cause_non_etablie",
 "cause": "… ou null", "piste": "cause d'un verdict direct déclassé, ou null",
 "faute": "apprenant | harnais | indetermine",
 "revelable": "…", "non_revelable": "…",
 "hypothese_etablie": "H1 ou null",
 "hypotheses_restantes": [{"id": "H2", "enonce": "…", "faute": "…",
                           "resultat": "non_tranchee | rejetee",
                           "raison_rejet": "si rejetee",
                           "experiences": [{"test": "…", "si_vraie": {}, "si_fausse": {},
                                            "resultat": {"code": 500, "exit_code": null, "extrait": "…"},
                                            "verdict": "non_tranchee"}]}],
 "hypotheses_refutees": ["même forme, resultat refutee"]}
```

Le consommateur prend le dernier bloc ` ```json ` de la sortie. Budget : `PI_CORRECTOR_INVESTIGATE_MAX_ACTIONS` (6) et `PI_CORRECTOR_INVESTIGATE_TIMEOUT_SECONDS` (240). Chaque test d'hypothèse compte pour une action, et chaque étape Hypothèse porte sa durée.

## Contrat de ligne de commande

Tous les scripts d'évaluation acceptent les mêmes arguments, qu'ils s'en servent ou non :

```
--exam-type --student --eval-dir --timeout --output-log --output-json --container-logs
```

Un argument inconnu est ignoré avec un avertissement : un contrat qui s'enrichit ne doit pas casser un skill existant.

## Contrôles

```bash
uvx --with testcontainers --with docker --with requests --with pyyaml python test_container_death_reporting.py
uvx --with testcontainers --with docker --with requests --with pyyaml python test_port_and_platform.py
uvx --with testcontainers --with docker --with requests --with pyyaml python test_fault_attribution.py
uvx --with pytest --with testcontainers --with docker --with requests --with pyyaml pytest -q
```

## ⚠️ Une modification locale ne prend effet qu'une fois poussée

Les skills déclarent ce dépôt en dépendance `uvx` :

```
pi_python_packages: git+https://github.com/ssime-git/exam-docker-eval,testcontainers,...
```

`uvx` l'installe **depuis GitHub**, et `find_spec("docker_eval")` trouve cette copie avant tout dépôt local. Modifier le code ici sans pousser ne change donc rien à ce qui s'exécute — et l'ancien comportement persiste sans le moindre message.

Pour travailler sur une modification non poussée, pointer explicitement le dépôt local :

```bash
EXAM_DOCKER_EVAL=/home/seb/project/exam-docker-eval/src python run_docker_eval.py ...
```

La variable est consultée avant les dépôts frères, mais **après** un paquet déjà importable. Retirer le git URL de `pi_python_packages` le temps du développement est la seule façon sûre.

## Comment les skills le trouvent

`pi-corrector` clone les dépôts déclarés dans son `skills.registry.json` côte à côte, puis place ce dépôt sur le `PYTHONPATH` du script d'évaluation. Un skill lancé à la main le cherche dans un dépôt frère.


## Où atterrit un correctif ? Les trois familles (scriptorium #49)

1. **Invariant du moteur** — vrai pour tous les examens, pour toujours (ports
   hôte éphémères, émulation QEMU, attribution de faute, qualification du
   bruit de sondes…) → code partagé ici, avec un test.
2. **Convention d'examen** — vraie pour un type d'examen → **frontmatter du
   SKILL.md**, jamais le moteur. Clés servies en environnement :
   `pi_default_service_port` → `EXAM_DEFAULT_SERVICE_PORT`,
   `pi_test_packages` → `EXAM_TEST_PACKAGES`,
   `pi_test_env` → `EXAM_TEST_ENV`,
   `pi_stack_mode` (serving|pipeline|auto) → `EXAM_STACK_MODE`.
   Clé absente = défaut historique du moteur.
3. **Cas unique d'une copie** — aucun code. C'est le travail de
   l'investigation outillée et de `revueRequise` : la revue humaine tranche.
