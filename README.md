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
| `config` | délais, limites de ressources, codes de sortie |

## Ce qu'il garantit

- **Le nettoyage est garanti**, y compris sur plantage et signaux. Les diagnostics sont capturés *avant* le démontage, jamais en le différant.
- **Les ports hôte sont éphémères** et relus après coup. Tester la disponibilité d'un port ne fonctionne pas depuis un conteneur : le bind se fait sur l'hôte, pas sur le loopback du processus.
- **Une image d'une autre architecture est exécutée sous émulation** si `binfmt_misc` expose des gestionnaires QEMU. Un apprenant qui construit sur un Mac Apple Silicon n'est pas pénalisé.
- **Un échec est attribué** : `apprenant`, `systeme`, ou `indetermine`. Dans le doute on ne tranche pas — un REPASS envoyé sur une supposition est irréversible.
- **Le temps rapporté est le temps écoulé**, jamais le délai configuré.

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
