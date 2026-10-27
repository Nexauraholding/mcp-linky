"""Client Conso API (https://conso.boris.sh). Lecture seule : uniquement des GET."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from . import __version__
from .fichier_acces import charger, attendu

BASE_PAR_DEFAUT = "https://conso.boris.sh/api"
USER_AGENT = f"NexAura-Linky-MCP/{__version__} (lecture seule)"

# Un appel = (url, en-têtes) -> (code HTTP, corps texte)
Fetch = Callable[[str, dict], "tuple[int, str]"]


try:  # SDK 2.x : seule une ToolError montre son message à l'agent (sinon : « Error executing tool »)
    from mcp.server.mcpserver.exceptions import ToolError as _Base
except ImportError:  # SDK 1.x ou SDK absent
    try:
        from mcp.server.fastmcp.exceptions import ToolError as _Base
    except ImportError:
        _Base = Exception


class LinkyError(_Base):
    """Erreur dont le message est écrit pour l'agent, en français simple."""


def fetch_http(url: str, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LinkyError("Impossible de joindre le service Enedis (Conso API). Réessaie plus tard.") from e


class ClientLinky:
    def __init__(self, token: str | None = None, prm: str | None = None,
                 fetch: Fetch = fetch_http, base: str | None = None,
                 cache_secondes: int = 6 * 3600, horloge=time.monotonic):
        env, self._secrets_file = charger(
            os.environ, "LINKY_SECRETS_FILE", {"LINKY_TOKEN", "LINKY_PRM"}, LinkyError)
        self.token = token if token is not None else env.get("LINKY_TOKEN", "")
        self.prm = prm if prm is not None else env.get("LINKY_PRM", "")
        self.base = (base or os.environ.get("LINKY_BASE_URL") or BASE_PAR_DEFAUT).rstrip("/")
        self._fetch = fetch
        self._ttl = cache_secondes
        self._horloge = horloge
        self._cache: dict[str, tuple[float, dict]] = {}

    @property
    def secrets(self) -> list[str]:
        return [self.token, self.prm]

    def lire(self, type_: str, debut: str, fin_exclue: str, **extra: str) -> dict:
        """GET /api/<type>. `fin_exclue` : la date de fin n'est pas incluse (règle de l'API)."""
        if not self.token or not self.prm:
            raise LinkyError(
                "Linky n'est pas encore branché : le jeton (LINKY_TOKEN) ou le numéro de "
                "compteur (LINKY_PRM) manque." + attendu(self._secrets_file)
            )
        params = {"pointId": self.prm, "dateDebut": debut, "dateFin": fin_exclue, **extra}
        url = f"{self.base}/{type_}?{urllib.parse.urlencode(params)}"
        # Pas de requêtes inutiles : mêmes données = même réponse (règle d'usage de Conso API).
        cle = f"{type_}|{debut}|{fin_exclue}|{sorted(extra.items())}"
        vu = self._cache.get(cle)
        if vu and self._horloge() - vu[0] < self._ttl:
            return vu[1]
        code, corps = self._fetch(url, {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        if code == 401:
            raise LinkyError("Le jeton Linky est refusé : il est invalide, expiré, ou ne donne pas "
                             "accès à ce compteur. Le client doit refaire l'autorisation sur conso.boris.sh.")
        if code == 400:
            raise LinkyError("Enedis a refusé les dates demandées (trop anciennes, trop longues ou "
                             "dans le futur). Change la période.")
        if code == 404:
            raise LinkyError("Enedis n'a pas encore ces données (elles arrivent en général le lendemain "
                             "vers 8 h). Réessaie plus tard.")
        if code == 429:
            raise LinkyError("Trop de demandes envoyées à Enedis. Attends un peu avant de réessayer.")
        if code >= 500:
            raise LinkyError("Enedis ou Conso API a un souci en ce moment. Réessaie plus tard.")
        if code != 200:
            raise LinkyError(f"Réponse inattendue du service Enedis (code {code}).")
        try:
            data = json.loads(corps)
        except json.JSONDecodeError as e:
            raise LinkyError("Réponse illisible du service Enedis.") from e
        self._cache[cle] = (self._horloge(), data)
        return data


def points(reponse: dict) -> list[dict]:
    """Les points {v, d, ...} de la première grandeur de la réponse (format Conso API v2)."""
    try:
        return list(reponse["grandeur"][0]["points"])
    except (KeyError, IndexError, TypeError):
        return []
