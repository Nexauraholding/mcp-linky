"""Les deux modes publient les annotations MCP, sans ouvrir de connexion."""
import asyncio
import json

import pytest

from conftest import PRM, TOKEN, FauxReseau
from nexaura_linky.autorisation import ConfigAutorisation
from nexaura_linky.client import ClientLinky
from nexaura_linky.server import PORTEE, creer_serveur


@pytest.mark.parametrize("mode", ["stdio", "http"])
def test_annotations_lecture_seule(mode, tmp_path):
    reseau = FauxReseau()
    client = ClientLinky(token=TOKEN, prm=PRM, fetch=reseau)
    cfg = ConfigAutorisation(
        "https://auth.example", "https://linky.example/mcp", PORTEE,
        "https://auth.example/jwks",
    ) if mode == "http" else None
    serveur = creer_serveur(client, tmp_path / "journal.jsonl", cfg_auth=cfg)
    outils = asyncio.run(serveur.list_tools())
    assert {outil.name for outil in outils} == {
        "conso_veille", "conso_periode", "courbe_charge", "puissance_max",
    }
    for outil in outils:
        # Vérifie aussi la présence des annotations dans la représentation MCP sérialisée.
        annotations = json.loads(outil.model_dump_json(by_alias=True, exclude_none=True))["annotations"]
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["idempotentHint"] is True
        assert annotations["openWorldHint"] is True
    assert reseau.appels == []
