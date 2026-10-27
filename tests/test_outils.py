from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import PRM, TOKEN, FauxReseau, reponse_courbe, reponse_pmax, reponse_quotidienne
from nexaura_linky import outils
from nexaura_linky.client import ClientLinky, LinkyError


@pytest.fixture(autouse=True)
def date_fixe(monkeypatch):
    monkeypatch.setattr(outils, "aujourdhui", lambda: date(2026, 5, 10))


def client(reseau):
    return ClientLinky(token=TOKEN, prm=PRM, fetch=reseau, base="http://faux/api")


def test_conso_veille_convertit_wh_en_kwh():
    r = FauxReseau(corps=reponse_quotidienne([("2026-05-09", 6261)]))
    out = outils.conso_veille(client(r))
    assert out == {"date": "2026-05-09", "disponible": True, "consommation_kwh": 6.261, "unite": "kWh"}
    url, h = r.appels[0]
    q = parse_qs(urlparse(url).query)
    assert urlparse(url).path.endswith("/consommation_quotidienne")
    assert q["dateDebut"] == ["2026-05-09"] and q["dateFin"] == ["2026-05-10"]  # fin exclue
    assert h["Authorization"] == f"Bearer {TOKEN}"


def test_conso_veille_pas_encore_arrivee():
    out = outils.conso_veille(client(FauxReseau(corps=reponse_quotidienne([]))))
    assert out["disponible"] is False and "8 h" in out["message"]


def test_conso_periode_total_et_moyenne():
    r = FauxReseau(corps=reponse_quotidienne([("2026-05-01", 6000), ("2026-05-02", 4000), ("2026-05-03", 5000)]))
    out = outils.conso_periode(client(r), "2026-05-01", "2026-05-03")
    assert out["total_kwh"] == 15.0 and out["moyenne_par_jour_kwh"] == 5.0 and out["nombre_de_jours"] == 3
    assert "message" not in out
    assert parse_qs(urlparse(r.appels[0][0]).query)["dateFin"] == ["2026-05-04"]


def test_conso_periode_signale_les_jours_manquants():
    r = FauxReseau(corps=reponse_quotidienne([("2026-05-01", 6000)]))
    assert "2 jour(s)" in outils.conso_periode(client(r), "2026-05-01", "2026-05-03")["message"]


def test_fin_dans_le_futur_est_ramenee_a_hier():
    r = FauxReseau(corps=reponse_quotidienne([]))
    out = outils.conso_periode(client(r), "2026-05-08", "2026-06-30")
    assert out["fin"] == "2026-05-09"


def test_periode_entierement_future_refusee():
    with pytest.raises(LinkyError, match="pas encore de données"):
        outils.conso_periode(client(FauxReseau()), "2026-06-01", "2026-06-02")


def test_date_mal_ecrite():
    with pytest.raises(LinkyError, match="AAAA-MM-JJ"):
        outils.conso_periode(client(FauxReseau()), "1er mai", "2026-05-03")


def test_periode_trop_longue():
    with pytest.raises(LinkyError, match="366"):
        outils.conso_periode(client(FauxReseau()), "2024-01-01", "2026-05-03")


def test_courbe_charge_watts_vers_kwh():
    r = FauxReseau(corps=reponse_courbe([("2026-05-09 00:30:00", 1000), ("2026-05-09 01:00:00", 500)]))
    out = outils.courbe_charge(client(r))
    assert out["points"][0] == {"heure": "2026-05-09 00:30:00", "puissance_moyenne_w": 1000, "energie_kwh": 0.5}
    assert out["energie_totale_kwh"] == 0.75


def test_courbe_charge_limite_7_jours():
    with pytest.raises(LinkyError, match="7 jours"):
        outils.courbe_charge(client(FauxReseau()), "2026-05-01", "2026-05-09")


def test_courbe_vide_explique_l_activation():
    out = outils.courbe_charge(client(FauxReseau(corps=reponse_courbe([]))))
    assert "courbe de charge" in out["message"]


def test_puissance_max():
    r = FauxReseau(corps=reponse_pmax([("2026-05-08 04:40:46", 2475), ("2026-05-09 22:10:33", 3325)]))
    out = outils.puissance_max(client(r), "2026-05-08", "2026-05-09")
    assert out["maximum"] == {"moment": "2026-05-09 22:10:33", "puissance_max_va": 3325}
    q = parse_qs(urlparse(r.appels[0][0]).query)
    assert q["grandeurPhysique"] == ["PMA"] and q["mesuresPas"] == ["P1D"]


@pytest.mark.parametrize("code,mot", [(401, "refusé"), (400, "dates"), (404, "pas encore"),
                                      (429, "Trop de demandes"), (500, "souci")])
def test_erreurs_http_en_francais(code, mot):
    with pytest.raises(LinkyError, match=mot):
        outils.conso_veille(client(FauxReseau(code=code)))


def test_jeton_ou_prm_manquant():
    c = ClientLinky(token="", prm="", fetch=FauxReseau())
    with pytest.raises(LinkyError, match="pas encore branché"):
        outils.conso_veille(c)


def test_cache_evite_les_requetes_inutiles():
    r = FauxReseau(corps=reponse_quotidienne([("2026-05-09", 1000)]))
    c = client(r)
    outils.conso_veille(c)
    outils.conso_veille(c)
    assert len(r.appels) == 1


def test_aucune_ecriture_http():
    # Le client n'expose que des GET : pas de méthode d'écriture dans le code.
    import inspect
    from nexaura_linky import client as m
    src = inspect.getsource(m)
    assert 'method="GET"' in src
    for interdit in ("POST", "PUT", "DELETE", "PATCH"):
        assert interdit not in src
