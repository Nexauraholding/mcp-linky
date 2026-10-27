"""Journal des appels : une ligne JSON par appel, fichier local, jamais de secret."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAUT = "~/.nexaura/journal/linky.jsonl"
_verrou = threading.Lock()


def chemin_journal() -> Path:
    """Emplacement réglable avec la variable LINKY_MCP_JOURNAL."""
    return Path(os.environ.get("LINKY_MCP_JOURNAL") or DEFAUT).expanduser()


def masquer(texte: str, secrets: Iterable[str]) -> str:
    """Remplace tout secret connu (jeton, PRM) et toute suite de 14 chiffres."""
    for s in secrets:
        if s and len(s) >= 4:
            texte = texte.replace(s, "***")
    return re.sub(r"\d{14}", "***", texte)


def ecrire_refus(raison: str, chemin: Path | None = None) -> None:
    """Un jeton refusé à la porte (http) : on note la raison courte, jamais le jeton."""
    ecrire("(porte)", {}, 0, "refuse", raison, (), chemin)


def ecrire(
    outil: str,
    parametres: dict[str, Any],
    duree_ms: int,
    resultat: str,
    erreur: str | None = None,
    secrets: Iterable[str] = (),
    chemin: Path | None = None,
    mandat: dict[str, str] | None = None,
) -> None:
    """Ajoute une ligne. `mandat` = {id, pour, agent} en mode http, absent en stdio (branchement local). N'échoue jamais : un journal cassé ne doit pas casser la lecture."""
    ligne: dict[str, Any] = {
        "quand": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "service": "linky",
        "outil": outil,
        "parametres": parametres,
        "acces": "lecture",  # ce MCP n'écrit jamais
        "duree_ms": duree_ms,
        "resultat": resultat,
        "mandat": mandat["id"] if mandat else None,  # l'identifiant du mandat sous lequel l'agent a agi
    }
    if mandat:
        ligne["pour"], ligne["agent"] = mandat["pour"], mandat["agent"]
    if erreur:
        ligne["erreur"] = masquer(erreur, secrets)[:300]
    try:
        cible = chemin or chemin_journal()
        cible.parent.mkdir(parents=True, exist_ok=True)
        with _verrou, open(cible, "a", encoding="utf-8") as f:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
    except OSError:
        pass
