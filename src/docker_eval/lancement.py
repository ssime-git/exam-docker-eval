"""Le lancement de l'énoncé, joué tel que livré (politique du 16/10).

« On note ce qui a été livré, on mesure le reste avec la réparation. »
L'énoncé nginx fait lancer le projet par `make start-project` ; l'énoncé
prometheus-grafana par « une simple commande `make` ». Le moteur lançait
`docker compose up --build` lui-même : la commande de l'énoncé n'était jamais
jouée, et un `.env` que la copie aurait dû produire (469217 : `make env`) était
fabriqué sans le dire au barème.

Isolation. La recette tourne dans le conteneur jetable de `make test` (image
alpine outillée, copie par archive, sans montage de l'hôte), avec un `docker`
factice en tête du PATH. Jamais le socket Docker : un docker-socket-proxy
filtre par route, pas par corps de requête — dès que `POST /containers/create`
passe (indispensable à `compose up`), la copie peut demander `privileged` ou un
montage de `/` et devenir root sur l'hôte. Docker-in-Docker exige `privileged`
(ou des userns imbriqués) et repart d'un cache d'images vide.

Fidélité. `make` développe lui-même la recette (`$(COMPOSE)`, prérequis
`certs env`, `$(MAKE)`, `$(shell …)`) : les prérequis qui n'ont pas besoin de
Docker tournent pour de vrai et produisent ce qu'ils produisent. Le factice
enregistre chaque appel ; `docker compose up` rend la main (code sentinelle,
make s'arrête là) et le moteur joue ce `up` lui-même, avec ses fichiers `-f`,
ses profils et son `--env-file`, sous son nom de projet et ports relâchés. Les
étapes qui suivent le `up` dans la recette (attente, messages) ne sont pas
jouées et ne comptent pas.
"""
import io
import os
import re
import tarfile

# Sentinelles du docker factice. 97 : `compose up` atteint, le moteur prend la
# main. 98 : commande docker que le moteur ne sait pas rejouer — non mesuré.
CODE_UP = 97
CODE_NON_REPRODUIT = 98
FICHIER_APPELS = "/scriptorium/appels"
DELAI_LANCEMENT_S = 180
MARGE_LANCEMENT_S = 60
TAILLE_MAX_FICHIER = 5 * 1024 * 1024
TAILLE_MAX_TOTALE = 50 * 1024 * 1024
TAILLE_EXTRAIT = 1500
_IGNORES = {".git", ".venv", "venv", "node_modules", "__pycache__", "__MACOSX"}

SEP_ARG, FIN_APPEL = "\x1f", "\x1e"

# Le docker factice : enregistre l'appel (arguments séparés par US, appel
# terminé par RS), ne parle à aucun démon. Lecture seule ou sans effet hors de
# la stack (down, build, ps…) : 0. `up` : la main au moteur.
SHIM_DOCKER = f"""#!/bin/sh
# docker factice du harnais Scriptorium : enregistre l'appel, ne parle a aucun demon.
prog=${{0##*/}}
{{ printf '%s' "$prog"; for a in "$@"; do printf '\\037%s' "$a"; done; printf '\\036\\n'; }} >> {FICHIER_APPELS}
if [ "$prog" = docker-compose ]; then set -- compose "$@"; fi
case "$1" in
  --version|-v|version) echo "Docker version 27 (factice du harnais)"; exit 0;;
  info) exit 0;;
  compose) shift;;
  *) echo "docker $1 : commande non reproduite par le harnais" >&2; exit {CODE_NON_REPRODUIT};;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    -f|--file|-p|--project-name|--profile|--env-file|--ansi|--progress|--parallel|--project-directory) shift 2;;
    -*) shift;;
    *) break;;
  esac
done
case "$1" in
  up) echo "docker compose up : le harnais prend la main" >&2; exit {CODE_UP};;
  ""|build|pull|config|ps|down|stop|rm|kill|ls|images|logs|version|top) exit 0;;
  *) echo "docker compose $1 : commande non reproduite par le harnais" >&2; exit {CODE_NON_REPRODUIT};;
esac
"""
# Le PATH du conteneur n'a pas sudo : `sudo docker compose up` doit aboutir au
# factice, pas à « sudo: not found » imputé à la copie.
SHIM_SUDO = '#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift; done\nexec "$@"\n'


def commande_de_lancement(eval_dir: str) -> str | None:
    """La commande de lancement que l'énoncé impose, ou None (autre examen)."""
    from . import sondes_nginx, sondes_prometheus
    if sondes_nginx.fichier_nginx_conf(eval_dir):
        return "make start-project"  # nginx_fr/06_nginx_fr.md, « Reproductibilité »
    if sondes_prometheus.est_examen_prometheus(eval_dir):
        return "make"  # prometheus_grafana_mlops_fr/06, « une simple commande `make` »
    return None


def makefile_de(eval_dir: str) -> str | None:
    """Le Makefile que `make` lirait, dans son ordre de recherche."""
    return next((os.path.join(eval_dir, n) for n in ("GNUmakefile", "makefile", "Makefile")
                 if os.path.isfile(os.path.join(eval_dir, n))), None)


def archive_du_factice() -> bytes:
    """docker, docker-compose et sudo factices, et le dossier d'enregistrement."""
    tampon = io.BytesIO()
    with tarfile.open(fileobj=tampon, mode="w") as archive:
        dossier = tarfile.TarInfo("scriptorium")
        dossier.type, dossier.mode = tarfile.DIRTYPE, 0o755
        archive.addfile(dossier)
        appels = tarfile.TarInfo("scriptorium/appels")
        appels.mode = 0o666
        archive.addfile(appels, io.BytesIO(b""))
        for nom, contenu in (("docker", SHIM_DOCKER), ("docker-compose", SHIM_DOCKER), ("sudo", SHIM_SUDO)):
            donnees = contenu.encode()
            info = tarfile.TarInfo(f"usr/local/bin/{nom}")
            info.size, info.mode = len(donnees), 0o755
            archive.addfile(info, io.BytesIO(donnees))
    return tampon.getvalue()


def lire_appels(texte: str) -> list:
    """Les appels enregistrés par le factice, chacun en argv."""
    appels = []
    for bloc in texte.split(FIN_APPEL):
        bloc = bloc.strip("\n")
        if bloc:
            appels.append(bloc.split(SEP_ARG))
    return appels


_GLOBALES_A_VALEUR = {"-f", "--file", "-p", "--project-name", "--profile", "--env-file",
                      "--ansi", "--progress", "--parallel"}
_GLOBALES_SANS_VALEUR = {"--compatibility", "--verbose"}
# Options de `up` sans effet sur ce qui est lancé (le moteur détache, construit
# et attend de lui-même) ; `--scale`, `--no-start`, `--dry-run`… changent la
# stack : pas de traduction approximative.
_UP_SANS_VALEUR = {"-d", "--detach", "--build", "--force-recreate", "--remove-orphans", "--wait",
                   "--quiet-pull", "--always-recreate-deps", "--renew-anon-volumes", "-V",
                   "--no-recreate", "--abort-on-container-exit", "--no-color", "--no-log-prefix",
                   "--no-attach", "--timestamps", "-y", "--yes"}
_UP_A_VALEUR = {"--pull", "--wait-timeout", "-t", "--timeout"}


def _sous_commande(argv: list) -> tuple:
    """(sous-commande compose, index) ; (None, …) hors compose."""
    if not argv:
        return None, 0
    args = argv[1:]
    if os.path.basename(argv[0]) != "docker-compose":
        if not args or args[0] != "compose":
            return None, 0
        args = args[1:]
    decalage = len(argv) - len(args)
    i = 0
    while i < len(args):
        a = args[i]
        if a in _GLOBALES_A_VALEUR or a == "--project-directory":
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a, decalage + i
    return "", decalage + i


def est_up(argv: list) -> bool:
    return _sous_commande(argv)[0] == "up"


def est_non_reproduit(argv: list) -> bool:
    """Ce que le factice a refusé (code 98) : même règle que le script."""
    sous, _ = _sous_commande(argv)
    if sous is None:
        premier = argv[1] if len(argv) > 1 else ""
        return premier not in ("--version", "-v", "version", "info")
    return sous not in ("", "up", "build", "pull", "config", "ps", "down", "stop", "rm", "kill",
                        "ls", "images", "logs", "version", "top")


def traduire_up(argv: list) -> dict | None:
    """Le `compose up` de la recette en arguments du moteur, ou None si le
    moteur ne peut pas le rejouer fidèlement. `-p` est ignoré : le nom de
    projet du moteur porte le nettoyage."""
    sous, index = _sous_commande(argv)
    if sous != "up":
        return None
    depart = 1 if os.path.basename(argv[0]) == "docker-compose" else 2
    globales, options = argv[depart:index], argv[index + 1:]
    resultat = {"fichiers": [], "profils": [], "env_files": [], "services": []}
    cles = {"-f": "fichiers", "--file": "fichiers", "--profile": "profils", "--env-file": "env_files"}
    i = 0
    while i < len(globales):
        a = globales[i]
        nom, egal, valeur = a.partition("=")
        if egal and nom in _GLOBALES_A_VALEUR:
            a, valeur, pas = nom, valeur, 1
        elif a in _GLOBALES_A_VALEUR:
            if i + 1 >= len(globales):
                return None
            valeur, pas = globales[i + 1], 2
        elif a in _GLOBALES_SANS_VALEUR:
            i += 1
            continue
        else:
            return None  # --project-directory, --dry-run, option inconnue
        if a in cles:
            resultat[cles[a]].append(valeur)
        i += pas
    i = 0
    while i < len(options):
        a = options[i]
        nom = a.partition("=")[0]
        if a in _UP_SANS_VALEUR:
            i += 1
        elif nom in _UP_A_VALEUR:
            i += 1 if "=" in a else 2
        elif a.startswith("-"):
            return None
        else:
            resultat["services"].append(a)
            i += 1
    return resultat


_SECRET = re.compile(
    r"(?im)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*)(\s*[=:]\s*)(\S+)")


def masquer_secrets(texte: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", texte or "")


def _extrait(sortie: str) -> str:
    from .sondes_nginx import nettoyer_sortie
    return masquer_secrets(nettoyer_sortie(sortie or "", maxi=TAILLE_EXTRAIT))


def _tel(commande, code, cause, motif, extrait, up=None) -> dict:
    return {"commande": commande, "code": code, "cause": cause, "motif": motif,
            "extrait": extrait, "up": up}


def classer(commande: str, code, sortie: str, appels: list) -> dict:
    """Ce que dit le passage : {commande, code, cause, motif, extrait, up}.

    cause None : la recette atteint `compose up` (code 0, le moteur joue ce
    up) ; "copie" : la commande échoue telle que livrée ; "non_mesure" : notre
    outillage empêche la mesure (outil absent de l'image, commande docker non
    rejouable, délai) — jamais un zéro pour la copie."""
    extrait = _extrait(sortie)
    ups = [a for a in appels if est_up(a)]
    if ups:
        up = ups[0]
        return _tel(commande, 0, None, None,
                    (f"`{commande}` atteint `{' '.join(up)}` : le moteur joue ce up lui-même. Les étapes "
                     f"suivantes de la recette (attente, messages) ne sont pas jouées.\n{extrait}").strip(), up)
    if code == 124:
        return _tel(commande, None, "non_mesure", "delai",
                    f"délai de {DELAI_LANCEMENT_S} s dépassé dans le conteneur jetable\n{extrait}".strip())
    refuses = [a for a in appels if est_non_reproduit(a)]
    if refuses or re.search(rf"Error {CODE_NON_REPRODUIT}\b", sortie or ""):
        quoi = " ".join(refuses[0]) if refuses else "docker"
        return _tel(commande, None, "non_mesure", "commande_docker_non_reproduite",
                    f"`{quoi}` : commande docker que le harnais ne rejoue pas sans démon\n{extrait}".strip())
    if re.search(r"No rule to make target|No targets specified and no makefile found", sortie or ""):
        return _tel(commande, code, "copie", "cible_absente", extrait)
    if code in (126, 127) or re.search(r"Error 12[67]\b", sortie or ""):
        return _tel(commande, None, "non_mesure", "outil_absent",
                    f"commande introuvable dans le conteneur jetable (outil absent de notre image ?)\n{extrait}".strip())
    if code == 0:
        return _tel(commande, 0, "copie", "ne_lance_pas_la_stack",
                    f"`{commande}` se termine sans lancer `docker compose up`\n{extrait}".strip())
    return _tel(commande, code, "copie", "prerequis_en_echec", extrait)


def sans_makefile(commande: str) -> dict:
    return _tel(commande, 2, "copie", "makefile_absent",
                f"pas de Makefile dans le projet : `{commande}` répond « No targets specified and no makefile found » "
                "(constat statique, rien n'a été exécuté)")


def non_mesure(commande: str, motif: str, extrait: str) -> dict:
    return _tel(commande, None, "non_mesure", motif, masquer_secrets(extrait))


def _sous_le_projet(racine: str, chemin: str) -> bool:
    reel = os.path.realpath(chemin)
    return reel == racine or reel.startswith(racine + os.sep)


def rapatrier(donnees: bytes, dossier: str, prefixe: str = "travail/") -> tuple:
    """Les fichiers réguliers nouveaux ou modifiés par la recette, recopiés
    dans le projet. Ce tar vient d'un conteneur qui a exécuté du code
    d'apprenant : ni lien, ni `..`, ni chemin absolu, taille bornée.

    Rend (nouveaux, originaux) : chemins créés, et contenu d'origine des
    fichiers remplacés — le nettoyage remet le projet en l'état."""
    racine = os.path.realpath(dossier)
    nouveaux, originaux, total = [], {}, 0
    with tarfile.open(fileobj=io.BytesIO(donnees)) as archive:
        for membre in archive:
            if not membre.isfile() or not membre.name.startswith(prefixe):
                continue
            relatif = os.path.normpath(membre.name[len(prefixe):])
            if not relatif or relatif == "." or os.path.isabs(relatif) or relatif.split(os.sep)[0] == "..":
                continue
            if any(p in _IGNORES for p in relatif.split(os.sep)):
                continue
            if membre.size > TAILLE_MAX_FICHIER or total + membre.size > TAILLE_MAX_TOTALE:
                continue
            cible = os.path.join(racine, relatif)
            if os.path.islink(cible) or not _sous_le_projet(racine, os.path.dirname(cible)):
                continue
            lecteur = archive.extractfile(membre)
            if lecteur is None:
                continue
            contenu = lecteur.read()
            ancien = None
            if os.path.lexists(cible):
                if not os.path.isfile(cible):
                    continue
                with open(cible, "rb") as f:
                    ancien = f.read()
                if ancien == contenu:
                    continue
            os.makedirs(os.path.dirname(cible), exist_ok=True)
            if not _sous_le_projet(racine, os.path.dirname(cible)):
                continue
            with open(cible, "wb") as f:
                f.write(contenu)
            total += membre.size
            if ancien is None:
                nouveaux.append(cible)
            else:
                originaux[cible] = ancien
    return nouveaux, originaux


def contenu_tar(donnees: bytes, nom: str) -> str:
    """Le texte d'un membre d'un tar (get_archive d'un fichier seul)."""
    with tarfile.open(fileobj=io.BytesIO(donnees)) as archive:
        for membre in archive:
            if membre.isfile() and os.path.basename(membre.name) == nom:
                lecteur = archive.extractfile(membre)
                return lecteur.read().decode("utf-8", errors="replace") if lecteur else ""
    return ""


def variables_definies(chemin_env: str) -> set:
    """Les noms affectés à une valeur non vide dans un .env."""
    noms = set()
    try:
        with open(chemin_env, encoding="utf-8", errors="replace") as lecteur:
            for ligne in lecteur:
                m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", ligne)
                if m and m.group(2).strip() not in ("", '""', "''"):
                    noms.add(m.group(1))
    except OSError:
        pass
    return noms
