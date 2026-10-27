import json
import os
import socket

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Réponses fausses écrites à la main : aucune vraie donnée.
TOKEN = "jeton-de-test-aaaa.bbbb.cccc"
PRM = "00000000000001"


def reponse_quotidienne(points):
    return {"periode": {"dateDebut": "x", "dateFin": "y"}, "pas": "P1D",
            "grandeur": [{"grandeurMetier": "CONS", "grandeurPhysique": "EA", "unite": "Wh",
                          "points": [{"v": str(v), "d": d} for d, v in points], "calendrier": []}]}


def reponse_courbe(points):
    return {"grandeur": [{"grandeurMetier": "CONS", "grandeurPhysique": "PA", "unite": "W",
                          "points": [{"v": str(v), "d": d, "p": "PT30M", "n": "B", "iv": "0", "ec": "0"}
                                     for d, v in points]}]}


def reponse_pmax(points):
    return {"pas": "P1D", "grandeur": [{"grandeurPhysique": "PMA", "unite": "VA",
                                        "points": [{"v": str(v), "d": d} for d, v in points]}]}


class FauxReseau:
    """Remplace le réseau : garde les appels reçus, renvoie une réponse prévue."""
    def __init__(self, code=200, corps=None):
        self.code, self.corps, self.appels = code, corps or {}, []

    def __call__(self, url, headers):
        self.appels.append((url, dict(headers)))
        return self.code, json.dumps(self.corps)


@pytest.fixture(autouse=True)
def sans_reseau(request, monkeypatch):
    """MCP_TEST_SANS_RESEAU=1 : aucun socket, même pour les faux serveurs locaux."""
    if os.environ.get("MCP_TEST_SANS_RESEAU") != "1":
        return

    def interdit(*args, **kwargs):
        raise AssertionError("Réseau interdit pendant cette exécution")

    monkeypatch.setattr(socket, "create_connection", interdit)
    monkeypatch.setattr(socket.socket, "connect", interdit)
    monkeypatch.setattr(socket.socket, "connect_ex", interdit)
    monkeypatch.setattr(socket.socket, "bind", interdit)


def pytest_collection_modifyitems(items):
    if os.environ.get("MCP_TEST_SANS_RESEAU") == "1":
        for item in items:
            if {"fausse_api", "faux_as", "mcp_http"}.intersection(item.fixturenames):
                item.add_marker(pytest.mark.skip(
                    reason="MCP_TEST_SANS_RESEAU=1 : faux serveur HTTP local écarté"))
