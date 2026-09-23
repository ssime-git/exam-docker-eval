"""Une requête HTTP de sonde, qui ne suit jamais les redirections.

`urllib.request.urlopen` suit les 3xx. Sur 457405 et 461091, nginx
répondait 301 vers `https://127.0.0.1/` (`$host` sans port, donc le 443 du
conteneur, non publié sur l'hôte) : la sonde suivait, prenait « Connection
refused », et une redirection qui marche passait pour une panne
(scriptorium #320). Une 3xx est une réponse du service ; on la consigne
avec son `Location` et on s'arrête là.
"""

import ssl
import urllib.error
import urllib.request


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    """Remplace le gestionnaire par défaut : la 3xx remonte en HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def contexte_tls_permissif() -> ssl.SSLContext:
    """Les certificats des copies sont auto-signés : on sonde, on ne valide pas."""
    contexte = ssl.create_default_context()
    contexte.check_hostname = False
    contexte.verify_mode = ssl.CERT_NONE
    return contexte


def _ouvrir(requete: urllib.request.Request, timeout: float, contexte):
    ouvreur = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=contexte or contexte_tls_permissif()),
        _SansRedirection(),
    )
    return ouvreur.open(requete, timeout=timeout)


def sonder(url: str, *, entetes: dict | None = None, corps: bytes | None = None,
           methode: str | None = None, timeout: float = 10, contexte=None,
           taille_extrait: int = 300) -> dict:
    """Une requête, sans redirection suivie.

    Rend `code` (int, ou « non reçu »), `extrait`, et selon le cas
    `location` (3xx), `entetes` (défi WWW-Authenticate) ou `erreur`.
    """
    requete = urllib.request.Request(url, data=corps, headers=entetes or {}, method=methode)
    try:
        reponse = _ouvrir(requete, timeout, contexte)
        return {"code": reponse.status,
                "extrait": reponse.read(taille_extrait).decode("utf-8", "replace")}
    except urllib.error.HTTPError as erreur:
        sonde = {"code": erreur.code,
                 "extrait": erreur.read(taille_extrait).decode("utf-8", "replace")}
        if 300 <= erreur.code < 400 and erreur.headers.get("Location"):
            sonde["location"] = erreur.headers["Location"]
        if "WWW-Authenticate" in erreur.headers:
            sonde["entetes"] = dict(erreur.headers)
        return sonde
    except Exception as erreur:
        return {"code": "non reçu", "erreur": str(erreur), "extrait": str(erreur)}


def resume(sonde: dict) -> str:
    """Première ligne d'une étape de sonde : le code, et où mène une 3xx."""
    ligne = f"code {sonde.get('code', 'non reçu')}"
    if sonde.get("location"):
        ligne += f" → {sonde['location']} : redirection active"
    return ligne
