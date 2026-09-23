"""Une requête HTTP de sonde, qui suit les redirections sans les prendre
pour une panne.

`urllib.request.urlopen` suivait les 3xx en silence. Sur 457405 et 461091,
nginx répondait 301 vers `https://127.0.0.1/` (`$host` sans port, donc le
443 du conteneur, non publié sur l'hôte) : la sonde prenait « Connection
refused », et une redirection qui marche passait pour une panne
(scriptorium #320).

On suit donc les sauts un à un, en gardant la chaîne :
- la cible répond : `code` est le code final, comme avant (les barèmes
  ne perdent rien), et `redirection` garde le premier saut ;
- la cible est injoignable depuis le harnais (refus, reset, TLS, délai) ou
  la chaîne boucle : `code` est le 3xx, `redirection_active` est vrai, et
  `raison` dit pourquoi le suivi s'est arrêté. Jamais une panne.
"""

import ssl
import urllib.error
import urllib.parse
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


SAUTS_MAX = 5


def _requete(url, entetes, corps, methode, timeout, contexte, taille_extrait):
    """Un saut : rend (sonde, location) ou lève une erreur de transport."""
    requete = urllib.request.Request(url, data=corps, headers=entetes or {}, method=methode)
    try:
        reponse = _ouvrir(requete, timeout, contexte)
        return {"code": reponse.status,
                "extrait": reponse.read(taille_extrait).decode("utf-8", "replace")}, None
    except urllib.error.HTTPError as erreur:
        sonde = {"code": erreur.code,
                 "extrait": erreur.read(taille_extrait).decode("utf-8", "replace")}
        if "WWW-Authenticate" in erreur.headers:
            sonde["entetes"] = dict(erreur.headers)
        location = erreur.headers.get("Location") if 300 <= erreur.code < 400 else None
        return sonde, location


def sonder(url: str, *, entetes: dict | None = None, corps: bytes | None = None,
           methode: str | None = None, timeout: float = 10, contexte=None,
           taille_extrait: int = 300) -> dict:
    """Une requête, redirections suivies et consignées.

    Rend `code` (int, ou « non reçu »), `extrait`, et selon le cas
    `redirection` ({code, location} du premier saut), `chaine` (tous les
    sauts), `redirection_active` + `raison`, `entetes` (défi
    WWW-Authenticate) ou `erreur`.
    """
    try:
        sonde, location = _requete(url, entetes, corps, methode, timeout, contexte, taille_extrait)
    except Exception as erreur:
        return {"code": "non reçu", "erreur": str(erreur), "extrait": str(erreur)}
    if location is None:
        return sonde

    premier = sonde
    chaine = []
    courante = url
    while location is not None:
        chaine.append({"code": sonde["code"], "location": location})
        if len(chaine) >= SAUTS_MAX:
            raison = f"boucle de redirections : suivi arrêté après {SAUTS_MAX} sauts"
            break
        suivante = urllib.parse.urljoin(courante, location)
        code = sonde["code"]
        if code == 303 or (code in (301, 302) and (methode or "GET").upper() not in ("GET", "HEAD")):
            methode, corps = "GET", None
        if urllib.parse.urlsplit(suivante).hostname != urllib.parse.urlsplit(courante).hostname:
            entetes = None  # les identifiants ne quittent pas l'hôte sondé
        courante = suivante
        try:
            sonde, location = _requete(courante, entetes, corps, methode, timeout, contexte,
                                       taille_extrait)
        except Exception as erreur:
            raison = f"cible non joignable depuis le harnais : {erreur}"
            break
    else:
        sonde.update(redirection=dict(chaine[0]), chaine=chaine)
        return sonde

    premier.update(redirection=dict(chaine[0]), chaine=chaine,
                   redirection_active=True, raison=raison)
    return premier


def resume(sonde: dict) -> str:
    """Première ligne d'une étape de sonde : le code, et la chaîne de
    redirections quand il y en a une."""
    code = sonde.get("code", "non reçu")
    chaine = sonde.get("chaine")
    if not chaine:
        return f"code {code}"
    sauts = " puis ".join(f"{saut['code']} → {saut['location']}" for saut in chaine)
    if sonde.get("redirection_active"):
        return f"code {sauts} : redirection active ({sonde.get('raison', '')})"
    return f"code {code} : {sauts} puis {code}"
