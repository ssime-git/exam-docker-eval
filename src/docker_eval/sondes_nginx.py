"""Ce que les sondes de comportement nginx lisent et concluent.

La grille nginx du 15/10 (469145) donne 35 points à des exigences prouvées en
marche : routage A/B, limitation de débit qui se déclenche, répliques v1 qui
reçoivent du trafic, 401 sur mauvais identifiants, exporter et cible
Prometheus UP. Une directive présente dans le nginx.conf ne prouve rien de
tout ça : on lit la configuration seulement pour savoir quoi envoyer (en-tête
du map, upstream par défaut), puis on regarde ce que la stack répond.

Ce module garde les lectures et les conclusions pures ; les requêtes passent
par le runner (`ComposeRunner._sonder_comportement_nginx`). Les deux sondes
TLS ouvrent elles-mêmes leur socket : `sonder` ne sait pas fixer la version
du protocole ni rendre le certificat.
"""

import os
import re
import shutil
import socket
import ssl
import subprocess
import time

_DOSSIERS_IGNORES = (".git", ".venv", "__pycache__", "node_modules")


def fichier_nginx_conf(eval_dir: str) -> str | None:
    """Le premier nginx.conf de la copie, s'il y en a un."""
    for racine, dossiers, fichiers in os.walk(eval_dir):
        dossiers[:] = [d for d in dossiers if d not in _DOSSIERS_IGNORES]
        for nom in fichiers:
            if nom.lower() == "nginx.conf":
                return os.path.join(racine, nom)
    return None


def lire_nginx_conf(eval_dir: str) -> str | None:
    chemin = fichier_nginx_conf(eval_dir)
    if not chemin:
        return None
    try:
        with open(chemin, encoding="utf-8", errors="replace") as lecteur:
            return lecteur.read()
    except OSError:
        return None


def _sans_commentaires(texte: str) -> str:
    return re.sub(r"#[^\n]*", "", texte)


def routage_ab_declare(texte: str) -> dict | None:
    """L'en-tête et la valeur qui font basculer le routage A/B.

    `map $http_x_experiment_group $api_backend { default apis-v1; debug
    apis-v2; }` (469145) : l'en-tête est X-Experiment-Group, la variante
    `debug`. nginx expose l'en-tête HTTP `X-Experiment-Group` en
    `$http_x_experiment_group` : on refait le chemin inverse. Les clés
    regex (`~…`) ne se rejouent pas sans deviner une chaîne : ignorées.
    """
    for bloc in re.finditer(r"\bmap\s+\$http_(\w+)\s+\$(\w+)\s*\{([^}]*)\}", _sans_commentaires(texte)):
        defaut, variante = None, None
        for cle, valeur in re.findall(r"^\s*(\S+)\s+([^;\s]+)\s*;", bloc.group(3), re.MULTILINE):
            cle, valeur = cle.strip("'\""), valeur.strip("'\"")
            if cle == "default":
                defaut = valeur
            elif cle not in ("hostnames", "volatile", "include") and not cle.startswith("~") \
                    and variante is None:
                variante = (cle, valeur)
        if variante:
            return {
                "entete": "-".join(p.capitalize() for p in bloc.group(1).split("_")),
                "valeur": variante[0],
                "upstream_defaut": defaut,
                "upstream_variante": variante[1],
            }
    return None


def serveurs_upstream(texte: str, nom: str | None) -> list:
    """Les hôtes d'un bloc `upstream <nom>` : les services Compose visés."""
    if not nom:
        return []
    bloc = re.search(r"\bupstream\s+" + re.escape(nom) + r"\s*\{([^}]*)\}", _sans_commentaires(texte))
    if not bloc:
        return []
    return [h for h in re.findall(r"\bserver\s+([^\s:;]+)", bloc.group(1))]


# Les répliques v1 par leur nom de service quand l'upstream ne les nomme pas :
# api-v1, api_v1, apis-v1, apiv1 — jamais api-v2 ni api-v10.
_MOTIF_V1 = re.compile(r"apis?[-_]?v1(?!\d)", re.IGNORECASE)


def est_replique_v1(service: str, services_v1: list) -> bool:
    if services_v1:
        return service in services_v1
    return bool(_MOTIF_V1.search(service))


def compter_requetes(logs: bytes, chemin: str) -> int:
    """Les lignes d'accès d'un conteneur API qui citent le chemin sondé."""
    texte = logs.decode("utf-8", "replace") if isinstance(logs, bytes) else str(logs)
    return sum(1 for ligne in texte.splitlines() if chemin in ligne)


def redirection_vers_https(sondes_racine: list):
    """La sonde racine du port 80 de nginx a-t-elle été renvoyée vers https ?

    Sur 469145, nginx répond 301 vers `https://127.0.0.1/` : le 443 du
    conteneur, publié ailleurs sur l'hôte, donc injoignable tel quel. La
    sonde s'arrête en « redirection active » : le premier saut suffit à
    prouver la redirection, la cible injoignable n'est pas une faute."""
    du_port_80 = [
        s for s in sondes_racine
        if str(s.get("port", "")).split("/")[0] == "80"
        and s.get("url", "").startswith("http:")
        and "nginx" in str(s.get("service", "")).lower()
        and "exporter" not in str(s.get("service", "")).lower()
    ]
    if not du_port_80:
        return None
    for sonde in du_port_80:
        chaine = sonde.get("chaine") or []
        if chaine and str(chaine[0].get("location", "")).startswith("https://"):
            return True
    if any(isinstance(s.get("code"), int) for s in du_port_80):
        return False
    return None


# Refus côté client : l'OpenSSL du harnais n'offre pas TLS 1.1, la sonde ne
# prouve rien sur le serveur.
_REFUS_LOCAUX = ("NO_PROTOCOLS_AVAILABLE", "NO_CIPHERS_AVAILABLE", "NO_SUITABLE",
                 "no protocols available", "no ciphers available")


def issue_tls_ancien(erreur) -> bool | None:
    """Ce que prouve une poignée de main plafonnée à TLS 1.1.

    Aboutie : le serveur accepte un TLS ancien (False). Interrompue par le
    serveur (alerte protocol_version, connexion coupée) : refus (True). Le
    client local qui n'offre pas TLS 1.1, ou un serveur injoignable : on ne
    sait pas (None) — ne pas deviner."""
    if erreur is None:
        return False
    if isinstance(erreur, ssl.SSLError):
        texte = str(erreur)
        if any(marque in texte for marque in _REFUS_LOCAUX):
            return None
        return True
    if isinstance(erreur, (ConnectionResetError, BrokenPipeError)):
        return True
    return None


def tls_ancien_refuse(hote: str, port: int, timeout: float = 10) -> bool | None:
    """Tenter une poignée de main en TLS 1.0/1.1 seulement.

    OpenSSL 3 n'offre TLS 1.0/1.1 qu'au niveau de sécurité 0 : sans
    `@SECLEVEL=0`, c'est le client qui refuse, et le serveur passerait pour
    conforme sans avoir été interrogé."""
    try:
        contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        contexte.check_hostname = False
        contexte.verify_mode = ssl.CERT_NONE
        contexte.minimum_version = ssl.TLSVersion.TLSv1
        contexte.maximum_version = ssl.TLSVersion.TLSv1_1
        contexte.set_ciphers("DEFAULT:@SECLEVEL=0")
    except (ValueError, ssl.SSLError, AttributeError):
        return None
    try:
        brut = socket.create_connection((hote, port), timeout=timeout)
    except OSError:
        return None
    try:
        with contexte.wrap_socket(brut, server_hostname=hote):
            pass
    except Exception as erreur:
        return issue_tls_ancien(erreur)
    finally:
        brut.close()
    return issue_tls_ancien(None)


def certificat_pem_valide(pem: str) -> bool | None:
    """Le certificat est-il dans sa période de validité ?

    Auto-signé, c'est attendu à l'examen : on ne juge que les dates.
    `cryptography` n'est pas une dépendance du moteur ; `openssl x509` lit
    les dates. Sans openssl ou sur un PEM illisible : None."""
    if not shutil.which("openssl"):
        return None
    try:
        sortie = subprocess.run(["openssl", "x509", "-noout", "-dates"], input=pem,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if sortie.returncode != 0:
        return None
    dates = dict(re.findall(r"^(notBefore|notAfter)=(.+)$", sortie.stdout, re.MULTILINE))
    try:
        debut = ssl.cert_time_to_seconds(dates["notBefore"].strip())
        fin = ssl.cert_time_to_seconds(dates["notAfter"].strip())
    except (KeyError, ValueError):
        return None
    return debut <= time.time() <= fin


def certificat_valide(hote: str, port: int, timeout: float = 10) -> bool | None:
    try:
        pem = ssl.get_server_certificate((hote, port), timeout=timeout)
    except Exception:
        return None
    return certificat_pem_valide(pem)


def _oui_non(valeur, oui="oui", non="non", inconnu="non mesuré") -> str:
    return inconnu if valeur is None else (oui if valeur else non)


def resume_comportement(c: dict) -> str:
    """L'étape citable : chaque champ de `nginx_comportement` en clair."""
    lignes = []
    ab = c.get("ab")
    if ab is None:
        lignes.append("routage A/B : non mesuré (pas de `map $http_…` dans nginx.conf, ou pas de base TLS)")
    else:
        lignes.append(
            f"routage A/B : POST sans en-tête → {ab['code_defaut']}, avec {ab['entete']}: {ab['valeur']} → "
            f"{ab['code_variante']} ; corps différents : {_oui_non(ab['corps_differents'])}")
    rafale = c.get("rafale")
    if rafale is None:
        lignes.append("rafale : non mesurée")
    else:
        codes = ", ".join(f"{n}×{code}" for code, n in sorted(rafale["codes"].items()))
        lignes.append(
            f"rafale : {rafale['envoyees']} POST authentifiés simultanés → {codes} ; limitation déclenchée "
            f"(429/503) : {_oui_non(rafale['limite_declenchee'])} ; 2xx après une pause : "
            f"{_oui_non(rafale['retour_2xx_apres_pause'])}")
    mauvais = c.get("mauvais_identifiants")
    lignes.append(f"mauvais identifiants : {'non mesuré' if mauvais is None else mauvais}")
    repartition = c.get("repartition")
    if repartition is None:
        lignes.append("répliques API v1 : non mesuré (aucun conteneur v1 identifié ou logs illisibles)")
    else:
        detail = ", ".join(f"{nom} : {n} requête(s)" for nom, n in repartition["conteneurs"].items())
        lignes.append(f"répliques API v1 : {repartition['recoivent']} sur {len(repartition['conteneurs'])} "
                      f"reçoivent du trafic ({detail})")
    m = c.get("monitoring") or {}
    lignes.append(f"exporter nginx_up = 1 : {_oui_non(m.get('exporter_nginx_up'))}")
    lignes.append("Prometheus cibles : "
                  + ("non mesuré" if m.get("prometheus_cibles_up") is None else
                     f"{m['prometheus_cibles_up']} up, {m['prometheus_cibles_down']} down"))
    lignes.append("Grafana provisionné : "
                  + ("non mesuré" if m.get("grafana_datasources") is None else
                     f"{m['grafana_datasources']} source(s) de données")
                  + ("" if m.get("grafana_dashboards") is None else f", {m['grafana_dashboards']} tableau(x) de bord"))
    lignes.append(f"redirection 80 → 443 : {_oui_non(c.get('redirection_80_443'))}")
    tls = c.get("tls") or {}
    lignes.append(f"TLS 1.1 refusé : {_oui_non(tls.get('tls_ancien_refuse'))} ; "
                  f"certificat dans sa période de validité : {_oui_non(tls.get('certificat_valide'))}")
    return "\n".join(lignes)
