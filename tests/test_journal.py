import json

import pytest

from conftest import PRM, TOKEN, FauxReseau, reponse_quotidienne
from nexaura_linky import outils
from nexaura_linky.client import ClientLinky, LinkyError
from nexaura_linky.journal import chemin_journal, masquer
from nexaura_linky.server import creer_serveur


def lignes(p):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_chemin_reglable(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKY_MCP_JOURNAL", str(tmp_path / "x.jsonl"))
    assert chemin_journal() == tmp_path / "x.jsonl"


def test_masquer_jeton_prm_et_14_chiffres():
    t = masquer(f"erreur {TOKEN} pour {PRM} et 12345678901234", [TOKEN, PRM])
    assert TOKEN not in t and PRM not in t and "12345678901234" not in t


def test_ligne_de_journal_ok_et_erreur(tmp_path, monkeypatch):
    monkeypatch.setattr(outils, "aujourdhui", lambda: __import__("datetime").date(2026, 5, 10))
    j = tmp_path / "j.jsonl"
    r = FauxReseau(corps=reponse_quotidienne([("2026-05-01", 1000)]))
    c = ClientLinky(token=TOKEN, prm=PRM, fetch=r, base="http://faux/api")
    s = creer_serveur(c, chemin_journal=j)
    fn = lambda nom: s.outils_directs[nom]  # noqa: E731
    fn("conso_periode")(debut="2026-05-01", fin="2026-05-01")
    with pytest.raises(LinkyError):
        fn("conso_periode")(debut="pas une date", fin="2026-05-01")
    ok, ko = lignes(j)
    assert ok["outil"] == "conso_periode" and ok["acces"] == "lecture" and ok["resultat"] == "ok"
    assert ok["parametres"] == {"debut": "2026-05-01", "fin": "2026-05-01"}
    assert isinstance(ok["duree_ms"], int) and ok["quand"].endswith("+00:00")
    assert ko["resultat"] == "erreur" and "AAAA-MM-JJ" in ko["erreur"]
    brut = j.read_text(encoding="utf-8")
    assert TOKEN not in brut and PRM not in brut
