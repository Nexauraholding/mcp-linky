"""Autorisation du mode http : le MCP est une « ressource protégée » OAuth 2.1, et le jeton qu'il
accepte est un « mandat ».

Rien n'est inventé ici : la spécification MCP d'autorisation (RFC 9728 pour la publication, RFC 8707
pour l'audience, PKCE côté client) est tenue par le SDK MCP ; la signature des jetons par PyJWT. Ce
fichier ne fait que vérifier un jeton RFC 9068 (`at+jwt`) émis par un serveur d'autorisation libre selon
le flux standard (code + PKCE + `resource`), et lire le mandat qu'il porte. Pas d'échange de jetons
RFC 8693 : la spécification MCP de base ne l'emploie pas, et Hermès (`auth: oauth`) fait le flux standard.

Le même fichier sert à Linky et à Qonto : il ne connaît aucun nom de service.

Le mandat est le jeton court lui-même. Il doit porter :
  iss, aud (= l'adresse exacte de ce MCP), exp, iat, jti,
  sub (pour qui l'agent agit : l'humain qui a donné son accord), client_id (l'agent : le client OAuth),
  scope (ex. « linky:read »),
  mandat = {"id": "...", "jusqu_a": <date de fin, secondes Unix>, "perimetre": {...}}
    (le serveur d'autorisation l'ajoute au jeton à partir de l'accord de l'humain).
Il vit 15 minutes au plus ; Hermès en redemande un au serveur d'autorisation tant que l'accord vit.
Un mandat dont la fin est passée est refusé même si le jeton est encore valide.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

try:  # PyJWT : seulement pour le mode http (pip install ".[http]") ; le mode stdio n'en a pas besoin
    import jwt
    from jwt import PyJWKClient
except ImportError:  # pragma: no cover
    jwt = None  # type: ignore[assignment]

DUREE_JETON_MAX = 900  # secondes : un jeton court
DERIVE_HORLOGE = 10    # secondes de tolérance entre deux horloges
ALGOS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
TYPES_JETON = ("at+jwt", "application/at+jwt")  # RFC 9068
OBLIGATOIRES = ["exp", "iat", "iss", "aud", "sub", "jti"]


class RefusJeton(Exception):
    """Jeton refusé. `code` est une raison courte pour le journal (jamais le jeton)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class HorsMandat(Exception):
    """L'appel sort du périmètre du mandat. Le message est écrit pour l'agent."""


@dataclass(frozen=True)
class Mandat:
    id: str
    pour: str                # sub : l'humain
    agent: str               # client_id : l'agent (le client OAuth)
    portees: tuple[str, ...]
    jusqu_a: int             # fin du mandat (secondes Unix)
    perimetre: dict = field(default_factory=dict)

    def pour_journal(self) -> dict[str, str]:
        return {"id": self.id, "pour": self.pour, "agent": self.agent}

    @classmethod
    def depuis_claims(cls, c: dict[str, Any], portee: str, maintenant: float | None = None) -> "Mandat":
        """Lit un jeton DÉJÀ vérifié. Refuse si le mandat est absent, mal formé ou fini."""
        m = c.get("mandat")
        agent = c.get("client_id")
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"]:
            raise RefusJeton("mandat_absent")
        if not isinstance(agent, str) or not agent:
            raise RefusJeton("agent_absent")  # RFC 9068 : sans `client_id`, on ne sait pas qui agit
        fin = m.get("jusqu_a")
        if isinstance(fin, bool) or not isinstance(fin, (int, float)):
            raise RefusJeton("mandat_sans_fin")
        if (maintenant if maintenant is not None else time.time()) >= fin:
            raise RefusJeton("mandat_expire")
        per = m.get("perimetre", {})
        if not isinstance(per, dict):
            raise RefusJeton("perimetre_invalide")
        portees = tuple(s for s in str(c.get("scope", "")).split() if s == portee)
        return cls(id=m["id"], pour=str(c["sub"]), agent=agent, portees=portees,
                   jusqu_a=int(fin), perimetre=per)


@dataclass
class ConfigAutorisation:
    issuer: str
    ressource: str          # adresse exacte de ce MCP : c'est l'audience attendue (RFC 8707)
    portee: str             # la seule portée acceptée, en lecture : « linky:read »
    jwks_url: str | None = None

    @classmethod
    def depuis_env(cls, prefixe: str, portee: str, env: dict[str, str] | None = None) -> "ConfigAutorisation":
        e = os.environ if env is None else env
        issuer = (e.get(f"{prefixe}_ISSUER") or "").strip()
        ressource = (e.get(f"{prefixe}_RESOURCE") or "").strip()
        if not issuer or not ressource:
            raise ValueError(f"Mode http : {prefixe}_ISSUER (le serveur d'autorisation) et "
                             f"{prefixe}_RESOURCE (l'adresse publique de ce MCP) sont obligatoires.")
        return cls(issuer=issuer, ressource=ressource, portee=portee,
                   jwks_url=(e.get(f"{prefixe}_JWKS_URL") or "").strip() or None)

    def valider(self) -> None:
        for nom, url in (("issuer", self.issuer), ("ressource", self.ressource)):
            u = urlsplit(url)
            if u.scheme not in ("https", "http") or not u.hostname or u.fragment:
                raise ValueError(f"Adresse « {nom} » invalide (https://hote[/chemin], sans #).")
            if u.scheme == "http" and not _boucle_locale(u.hostname):
                raise ValueError(f"Adresse « {nom} » en http : seulement sur la machine locale.")


def _boucle_locale(hote: str) -> bool:
    if hote == "localhost" or hote.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hote).is_loopback
    except ValueError:
        return False


def trouver_jwks(issuer: str, delai: float = 5.0) -> str:
    """Découverte RFC 8414 puis OpenID : l'adresse des clés publiques du serveur d'autorisation."""
    base = issuer.rstrip("/")
    for chemin in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        try:
            with urllib.request.urlopen(base + chemin, timeout=delai) as r:  # noqa: S310 - adresse réglée par l'exploitant
                meta = json.loads(r.read(1_000_000))
        except (OSError, ValueError):
            continue
        if meta.get("issuer") == issuer and isinstance(meta.get("jwks_uri"), str):  # RFC 8414 §3.3
            return meta["jwks_uri"]
    raise ValueError("Impossible de trouver les clés du serveur d'autorisation (RFC 8414). "
                     "Donne leur adresse avec _JWKS_URL.")


class VerificateurMandat:
    """Implémente `TokenVerifier` du SDK MCP : un jeton en entrée, un `AccessToken` ou rien."""

    def __init__(self, cfg: ConfigAutorisation, sur_refus: Callable[[str], None] | None = None):
        if jwt is None:
            raise RuntimeError("Le mode http a besoin de PyJWT : pip install \".[http]\"")
        cfg.valider()
        self.cfg = cfg
        self.sur_refus = sur_refus
        self._cles = PyJWKClient(cfg.jwks_url or trouver_jwks(cfg.issuer), cache_keys=True,
                                 lifespan=300, timeout=5)

    def verifier(self, jeton: str) -> tuple[Mandat, dict[str, Any]]:
        """Vérifie signature, émetteur, audience, expiration, durée, mandat. Lève RefusJeton."""
        try:
            entete = jwt.get_unverified_header(jeton)
        except jwt.PyJWTError:
            raise RefusJeton("jeton_illisible") from None
        if entete.get("alg") not in ALGOS:
            raise RefusJeton("algorithme_refuse")
        if str(entete.get("typ", "")).lower() not in TYPES_JETON:
            raise RefusJeton("type_refuse")
        try:
            cle = self._cles.get_signing_key_from_jwt(jeton).key
        except (jwt.PyJWTError, OSError):
            raise RefusJeton("cle_introuvable") from None
        try:
            c = jwt.decode(jeton, cle, algorithms=ALGOS, audience=self.cfg.ressource,
                           issuer=self.cfg.issuer, leeway=DERIVE_HORLOGE,
                           options={"require": OBLIGATOIRES})
        except jwt.ExpiredSignatureError:
            raise RefusJeton("jeton_expire") from None
        except jwt.InvalidAudienceError:
            raise RefusJeton("mauvaise_audience") from None
        except jwt.InvalidIssuerError:
            raise RefusJeton("mauvais_emetteur") from None
        except jwt.PyJWTError:
            raise RefusJeton("jeton_invalide") from None
        if not all(isinstance(c[k], str) and c[k] for k in ("sub", "jti")):
            raise RefusJeton("jeton_invalide")
        if c["exp"] - c["iat"] > DUREE_JETON_MAX:
            raise RefusJeton("jeton_trop_long")
        return Mandat.depuis_claims(c, self.cfg.portee), c

    async def verify_token(self, token: str):  # nom imposé par le SDK
        from mcp.server.auth.provider import AccessToken
        try:
            mandat, c = await asyncio.to_thread(self.verifier, token)
        except RefusJeton as e:
            if self.sur_refus:
                self.sur_refus(e.code)
            return None
        return AccessToken(token=token, client_id=mandat.agent,
                           scopes=list(mandat.portees), expires_at=int(c["exp"]),
                           resource=self.cfg.ressource, subject=mandat.pour,
                           claims={"iss": c["iss"], "client_id": c["client_id"], "mandat": c["mandat"],
                                   "jti": c["jti"], "scope": c.get("scope", "")})


def reglages_mcp(cfg: ConfigAutorisation):
    """`AuthSettings` du SDK : publie les métadonnées de ressource protégée (RFC 9728) et exige la portée.

    Si le SDK sait aussi comparer l'audience (`validate_token_resource`), on l'active : c'est une seconde
    barrière derrière notre propre vérification."""
    from mcp.server.auth.settings import AuthSettings
    options: dict[str, Any] = {}
    if "validate_token_resource" in getattr(AuthSettings, "model_fields", {}):
        options["validate_token_resource"] = True
    return AuthSettings(issuer_url=cfg.issuer, resource_server_url=cfg.ressource,
                        required_scopes=[cfg.portee], **options)


def mandat_courant(portee: str, exige: bool = False) -> Mandat | None:
    """Le mandat de la requête en cours.

    Mode stdio (branchement local) : pas de jeton, donc None. Mode http (`exige=True`) : tout appel doit
    avoir un mandat vivant, sinon refus : on ne retombe jamais sur le mode local par erreur."""
    jeton = None
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        jeton = get_access_token()
    except Exception:  # noqa: BLE001
        jeton = None
    if jeton is None or not jeton.claims:
        if exige:
            raise HorsMandat("Aucun mandat pour cet appel : il est refusé.")
        return None
    try:
        return Mandat.depuis_claims({**jeton.claims, "sub": jeton.subject, "scope": " ".join(jeton.scopes)},
                                    portee)
    except RefusJeton as e:
        raise HorsMandat("Le mandat n'est plus valable (fini ou incomplet) : appel refusé.") from e


def controler_outil(mandat: Mandat | None, outil: str) -> None:
    """Périmètre « outils » : si le mandat en donne la liste, l'outil doit y figurer."""
    if mandat is None:
        return
    permis = mandat.perimetre.get("outils")
    if permis is None:
        return
    if not isinstance(permis, list) or outil not in permis:
        raise HorsMandat(f"Ce mandat ne couvre pas l'outil « {outil} ».")
