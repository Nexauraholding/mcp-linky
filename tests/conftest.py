import json
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
