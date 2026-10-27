"""Les quatre outils, tous en lecture. Fonctions pures : le client est passé en argument."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .client import ClientLinky, LinkyError, points

PARIS = ZoneInfo("Europe/Paris")
MAX_JOURS_PERIODE = 366
MAX_JOURS_COURBE = 7  # limite d'Enedis pour la courbe de charge


def aujourdhui() -> date:
    return datetime.now(PARIS).date()


def _date(texte: str, nom: str) -> date:
    try:
        return date.fromisoformat(texte.strip())
    except (ValueError, AttributeError):
        raise LinkyError(f"La date « {nom} » doit être écrite AAAA-MM-JJ, par exemple 2026-05-01.")


def _periode(debut: str | None, fin: str | None, max_jours: int, aujourd=None) -> tuple[date, date]:
    """Dates incluses des deux côtés. Par défaut : hier. Jamais dans le futur ni aujourd'hui."""
    hier = (aujourd or aujourdhui()) - timedelta(days=1)
    d = _date(debut, "debut") if debut else hier
    f = _date(fin, "fin") if fin else (d if debut else hier)
    if f > hier:
        f = hier  # les données du jour ne sont pas encore là
    if d > f:
        raise LinkyError("Cette période n'a pas encore de données : Enedis ne donne que les jours "
                         "déjà terminés, jusqu'à hier.")
    if (f - d).days + 1 > max_jours:
        raise LinkyError(f"Période trop longue : {max_jours} jours au plus par demande.")
    return d, f


def _kwh(wh: str | int | float) -> float:
    return round(float(wh) / 1000, 3)


def conso_veille(c: ClientLinky) -> dict:
    d, f = _periode(None, None, 1)
    rep = c.lire("consommation_quotidienne", d.isoformat(), (f + timedelta(days=1)).isoformat())
    pts = points(rep)
    if not pts:
        return {"date": d.isoformat(), "disponible": False,
                "message": "Les données d'hier ne sont pas encore arrivées. Elles sont en général "
                           "disponibles vers 8 h, parfois 10 h. Réessaie plus tard."}
    return {"date": d.isoformat(), "disponible": True, "consommation_kwh": _kwh(pts[0]["v"]),
            "unite": "kWh"}


def conso_periode(c: ClientLinky, debut: str, fin: str) -> dict:
    d, f = _periode(debut, fin, MAX_JOURS_PERIODE)
    rep = c.lire("consommation_quotidienne", d.isoformat(), (f + timedelta(days=1)).isoformat())
    jours = [{"date": p["d"], "kwh": _kwh(p["v"])} for p in points(rep)]
    total = round(sum(j["kwh"] for j in jours), 3)
    out: dict = {"debut": d.isoformat(), "fin": f.isoformat(), "unite": "kWh",
                 "jours": jours, "total_kwh": total, "nombre_de_jours": len(jours)}
    if jours:
        out["moyenne_par_jour_kwh"] = round(total / len(jours), 3)
    manque = (f - d).days + 1 - len(jours)
    if manque > 0:
        out["message"] = f"{manque} jour(s) sans donnée (compteur muet ou données pas encore arrivées)."
    return out


def courbe_charge(c: ClientLinky, debut: str | None = None, fin: str | None = None) -> dict:
    d, f = _periode(debut, fin, MAX_JOURS_COURBE)
    rep = c.lire("courbe_de_charge_consommation", d.isoformat(), (f + timedelta(days=1)).isoformat())
    pts = []
    for p in points(rep):
        w = float(p["v"])
        # La courbe donne une puissance moyenne (W) sur 30 min : énergie = W x 0,5 h.
        pts.append({"heure": p["d"], "puissance_moyenne_w": round(w), "energie_kwh": round(w * 0.5 / 1000, 4)})
    out: dict = {"debut": d.isoformat(), "fin": f.isoformat(), "pas": "30 minutes", "points": pts,
                 "energie_totale_kwh": round(sum(p["energie_kwh"] for p in pts), 3)}
    if not pts:
        out["message"] = ("Aucun point. Le client doit avoir activé « enregistrement de la courbe de "
                          "charge » dans son espace Enedis, et les données arrivent le lendemain.")
    return out


def puissance_max(c: ClientLinky, debut: str | None = None, fin: str | None = None) -> dict:
    d, f = _periode(debut, fin, MAX_JOURS_PERIODE)
    rep = c.lire("puissance_conso_max_quotidienne", d.isoformat(),
                 (f + timedelta(days=1)).isoformat(), mesuresPas="P1D", grandeurPhysique="PMA")
    jours = [{"moment": p["d"], "puissance_max_va": round(float(p["v"]))} for p in points(rep)]
    out: dict = {"debut": d.isoformat(), "fin": f.isoformat(), "unite": "VA", "jours": jours}
    if jours:
        top = max(jours, key=lambda j: j["puissance_max_va"])
        out["maximum"] = top
    else:
        out["message"] = "Aucune donnée de puissance pour cette période."
    return out
