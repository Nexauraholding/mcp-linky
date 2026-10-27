"""Test du protocole MCP : on lance le vrai serveur en stdio, contre une fausse API locale."""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from conftest import PRM, TOKEN, reponse_quotidienne

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

RACINE = Path(__file__).resolve().parent.parent
VUES: list = []


class FausseApi(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        VUES.append((self.path, self.headers.get("Authorization")))
        corps = json.dumps(reponse_quotidienne([("2026-05-01", 6261), ("2026-05-02", 4882)])).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(corps)

    def log_message(self, *a):
        pass


@pytest.fixture()
def fausse_api():
    srv = HTTPServer(("127.0.0.1", 0), FausseApi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/api"
    srv.shutdown()


def test_liste_des_outils_et_un_appel(tmp_path, fausse_api):
    journal = tmp_path / "j.jsonl"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "nexaura_linky"], cwd=str(RACINE),
        env={"LINKY_TOKEN": TOKEN, "LINKY_PRM": PRM, "LINKY_BASE_URL": fausse_api,
             "LINKY_MCP_JOURNAL": str(journal), "PYTHONPATH": str(RACINE)})

    async def scenario():
        async with stdio_client(params) as (lire, ecrire):
            async with ClientSession(lire, ecrire) as s:
                await s.initialize()
                outils = (await s.list_tools()).tools
                res = await s.call_tool("conso_periode", {"debut": "2026-05-01", "fin": "2026-05-02"})
                return outils, res

    outils, res = asyncio.run(scenario())
    assert sorted(t.name for t in outils) == ["conso_periode", "conso_veille", "courbe_charge", "puissance_max"]
    for t in outils:
        assert t.description and "Lecture seule" in t.description  # description française, lecture seule
    assert not (getattr(res, "isError", None) or getattr(res, "is_error", None))
    texte = "".join(c.text for c in res.content if hasattr(c, "text"))
    assert '"total_kwh"' in texte and "11.143" in texte
    assert VUES and VUES[0][1] == f"Bearer {TOKEN}"
    ligne = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert ligne["outil"] == "conso_periode" and ligne["resultat"] == "ok"
    assert TOKEN not in journal.read_text(encoding="utf-8")
