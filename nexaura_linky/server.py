"""Serveur MCP (stdio par défaut, http possible). Quatre outils, tous en lecture."""

from __future__ import annotations

import functools
import os
import time
from datetime import date, timedelta
from typing import Any, Callable

from mcp.types import ToolAnnotations

from . import autorisation, journal, outils
from .client import ClientLinky, LinkyError

try:  # SDK MCP 2.x
    from mcp.server.mcpserver import MCPServer as _Serveur
except ImportError:  # SDK MCP 1.x
    from mcp.server.fastmcp import FastMCP as _Serveur

INSTRUCTIONS = (
    "Linky : consommation électrique du client, lue chez Enedis. Lecture seule : rien n'est "
    "jamais modifié. Les données d'un jour arrivent le lendemain vers 8 h."
)


PORTEE = "linky:read"  # la seule portée : aucune portée d'écriture n'existe


def _controler_perimetre(mandat, nom: str, kw: dict) -> None:
    """Périmètre du mandat : `outils` (liste d'outils permis) et `depuis` (première date lisible)."""
    autorisation.controler_outil(mandat, nom)
    if mandat is None or "depuis" not in mandat.perimetre:
        return
    depuis = mandat.perimetre["depuis"]
    try:
        limite = date.fromisoformat(depuis)
    except (TypeError, ValueError):
        raise autorisation.HorsMandat("Le mandat a une date de départ illisible : appel refusé.") from None
    try:
        premier = date.fromisoformat(kw["debut"]) if kw.get("debut") else outils.aujourdhui() - timedelta(days=1)
    except (TypeError, ValueError):
        return  # date illisible : l'outil la refusera lui-même
    if premier < limite:
        raise autorisation.HorsMandat(f"Ce mandat ne permet de lire qu'à partir du {limite.isoformat()}.")


def creer_serveur(client: ClientLinky | None = None, chemin_journal=None,
                  cfg_auth: autorisation.ConfigAutorisation | None = None):
    """`cfg_auth` : mode http, MCP protégé par un mandat. Sans lui : stdio local, comme avant."""
    cl = client or ClientLinky()
    http = cfg_auth is not None
    options: dict[str, Any] = {}
    if http:
        options = {"token_verifier": autorisation.VerificateurMandat(
            cfg_auth, sur_refus=lambda raison: journal.ecrire_refus(raison, chemin_journal)),
            "auth": autorisation.reglages_mcp(cfg_auth)}
    serveur = _Serveur("linky", instructions=INSTRUCTIONS, **options)

    annotations = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    )

    def lecture(nom: str, fn: Callable[..., dict]):
        """Enveloppe : journalise chaque appel (sans secret) et traduit les erreurs."""
        @functools.wraps(fn)
        def enveloppe(**kw: Any) -> dict:
            t0 = time.monotonic()
            parametres = {k: v for k, v in kw.items() if v is not None}
            mandat = None
            try:
                mandat = autorisation.mandat_courant(PORTEE, exige=http)
                _controler_perimetre(mandat, nom, kw)
                res = fn(cl, **kw)
            except autorisation.HorsMandat as e:
                journal.ecrire(nom, parametres, int((time.monotonic() - t0) * 1000), "refuse",
                               str(e), cl.secrets, chemin_journal, mandat.pour_journal() if mandat else None)
                raise LinkyError(str(e)) from None
            except LinkyError as e:
                journal.ecrire(nom, parametres, int((time.monotonic() - t0) * 1000), "erreur",
                               str(e), cl.secrets, chemin_journal, mandat.pour_journal() if mandat else None)
                raise
            except Exception as e:  # noqa: BLE001
                journal.ecrire(nom, parametres, int((time.monotonic() - t0) * 1000), "erreur",
                               type(e).__name__, cl.secrets, chemin_journal,
                               mandat.pour_journal() if mandat else None)
                raise LinkyError("Erreur inattendue en lisant Linky.") from None
            journal.ecrire(nom, parametres, int((time.monotonic() - t0) * 1000), "ok",
                           None, cl.secrets, chemin_journal, mandat.pour_journal() if mandat else None)
            return res
        return enveloppe

    @serveur.tool(name="conso_veille", annotations=annotations)
    def conso_veille() -> dict:
        """Dit combien d'électricité le foyer a consommé hier, en kWh (kilowattheures).
        À utiliser pour « combien j'ai consommé hier ? ». Lecture seule."""
        return lecture("conso_veille", outils.conso_veille)()

    @serveur.tool(name="conso_periode", annotations=annotations)
    def conso_periode(debut: str, fin: str) -> dict:
        """Donne la consommation d'électricité jour par jour, en kWh, entre deux dates incluses,
        avec le total et la moyenne. Dates au format AAAA-MM-JJ (exemple 2026-05-01).
        Jusqu'à 366 jours, jusqu'à hier au plus tard. Lecture seule."""
        return lecture("conso_periode", outils.conso_periode)(debut=debut, fin=fin)

    @serveur.tool(name="courbe_charge", annotations=annotations)
    def courbe_charge(debut: str | None = None, fin: str | None = None) -> dict:
        """Donne la consommation toutes les 30 minutes (puissance moyenne en watts et énergie en kWh).
        Sans dates : hier. 7 jours au plus. Dates au format AAAA-MM-JJ, incluses.
        Marche seulement si le client a activé l'enregistrement de la courbe de charge chez Enedis.
        Lecture seule."""
        return lecture("courbe_charge", outils.courbe_charge)(debut=debut, fin=fin)

    @serveur.tool(name="puissance_max", annotations=annotations)
    def puissance_max(debut: str | None = None, fin: str | None = None) -> dict:
        """Donne la puissance maximale atteinte chaque jour (en VA) et le jour le plus fort de la
        période. Utile pour savoir si l'abonnement est trop petit ou trop grand. Sans dates : hier.
        Dates au format AAAA-MM-JJ, incluses. Lecture seule."""
        return lecture("puissance_max", outils.puissance_max)(debut=debut, fin=fin)

    serveur.outils_directs = {  # pour les tests : appeler un outil sans passer par le protocole
        "conso_veille": conso_veille, "conso_periode": conso_periode,
        "courbe_charge": courbe_charge, "puissance_max": puissance_max}
    return serveur


def main() -> None:
    transport = os.environ.get("LINKY_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        creer_serveur().run()  # branchement local, sans jeton : ce que fait Hermès aujourd'hui
        return
    # Mode http : ressource protégée OAuth 2.1. Jamais sans serveur d'autorisation.
    cfg = autorisation.ConfigAutorisation.depuis_env("LINKY_MCP", PORTEE)
    serveur = creer_serveur(cfg_auth=cfg)
    hote = os.environ.get("LINKY_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("LINKY_MCP_PORT", "8791"))
    hotes = [h.strip() for h in os.environ.get("LINKY_MCP_HOTES", "").split(",") if h.strip()]
    lancer_http(serveur, hote, port, hotes)


def lancer_http(serveur, hote: str, port: int, hotes_autorises: list[str]) -> None:
    """Lance le serveur http (SDK 2.x : arguments de run ; SDK 1.x : réglages du serveur)."""
    from mcp.server.transport_security import TransportSecuritySettings
    securite = TransportSecuritySettings(
        allowed_hosts=[f"{hote}:*", "localhost:*", "127.0.0.1:*", *hotes_autorises],
        allowed_origins=[]) if hotes_autorises else None
    try:
        serveur.run(transport="streamable-http", host=hote, port=port, transport_security=securite)
    except TypeError:  # SDK 1.x
        serveur.settings.host, serveur.settings.port = hote, port
        if securite is not None:
            serveur.settings.transport_security = securite
        serveur.run(transport="streamable-http")
