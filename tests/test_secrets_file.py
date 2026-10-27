import os
import traceback
from unittest.mock import patch

import pytest

from nexaura_linky.client import ClientLinky, LinkyError


@pytest.fixture
def prive(tmp_path, monkeypatch):
    for cle in ("LINKY_TOKEN", "LINKY_PRM", "LINKY_SECRETS_FILE", "INCONNUE"):
        monkeypatch.delenv(cle, raising=False)
    fichier = tmp_path / "acces"
    fichier.write_text('  # commentaire\n\n LINKY_TOKEN = "jeton-fictif=abc" \n'
                       " LINKY_PRM = '00000000000001' \nINCONNUE=ignore-moi\n")
    fichier.chmod(0o600)
    monkeypatch.setenv("LINKY_SECRETS_FILE", str(fichier))
    return fichier


def test_lecture_et_environnement_intact(prive):
    avant = dict(os.environ)
    client = ClientLinky()
    assert client.secrets == ["jeton-fictif=abc", "00000000000001"]
    assert dict(os.environ) == avant
    assert "INCONNUE" not in os.environ


def test_env_prioritaire(prive, monkeypatch):
    monkeypatch.setenv("LINKY_TOKEN", "env-fictif")
    assert ClientLinky().token == "env-fictif"
    monkeypatch.setenv("LINKY_TOKEN", "")
    assert ClientLinky().token == ""


def test_tilde(prive, monkeypatch):
    monkeypatch.setenv("HOME", str(prive.parent))
    monkeypatch.setenv("LINKY_SECRETS_FILE", "~/acces")
    assert ClientLinky().token == "jeton-fictif=abc"


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_droits_refuses_avant_lecture(prive, mode):
    prive.chmod(mode)
    with patch("nexaura_linky.fichier_acces.os.open", side_effect=AssertionError("lecture interdite")):
        with pytest.raises(LinkyError) as exc:
            ClientLinky()
    texte = str(exc.value)
    assert f"chmod 600 {prive}" in texte
    assert "jeton-fictif" not in texte
    assert "00000000000001" not in texte


def test_lien_refuse(prive, monkeypatch):
    lien = prive.parent / "lien"
    lien.symlink_to(prive)
    monkeypatch.setenv("LINKY_SECRETS_FILE", str(lien))
    with pytest.raises(LinkyError, match="sans lien symbolique"):
        ClientLinky()


def test_autre_proprietaire_refuse(prive):
    with patch("nexaura_linky.fichier_acces.os.getuid", return_value=os.getuid() + 1):
        with pytest.raises(LinkyError, match="qui t'appartient"):
            ClientLinky()


@pytest.mark.parametrize("contenu", [None, "", "LINKY_TOKEN=jeton-fictif\n"])
def test_absent_vide_ou_incomplet(prive, contenu):
    if contenu is None:
        prive.unlink()
    else:
        prive.write_text(contenu)
    with pytest.raises(LinkyError) as exc:
        ClientLinky().lire("daily_consumption", "2026-01-01", "2026-01-02")
    assert "compteur (LINKY_PRM) manque." in str(exc.value)
    assert str(prive) in str(exc.value)
    assert "jeton-fictif" not in str(exc.value)


def test_encodage_invalide_sans_fuite(prive):
    prive.write_bytes(b"LINKY_TOKEN=jeton-fictif\xff")
    with pytest.raises(LinkyError) as exc:
        ClientLinky()
    assert "jeton-fictif" not in "".join(traceback.format_exception(exc.value))
