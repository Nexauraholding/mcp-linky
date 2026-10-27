"""Lecture des seuls accès autorisés, sans modifier l'environnement."""

import os
import shlex
import stat


def charger(env, variable, cles, erreur):
    valeurs = dict(env)
    chemin = os.path.expanduser(env.get(variable) or "")
    if not chemin:
        return valeurs, ""

    def refuser():
        return erreur(
            f"Fichier privé refusé : {chemin}. Utilise un fichier ordinaire, sans lien symbolique, "
            f"qui t'appartient, puis lance : chmod 600 {shlex.quote(chemin)}"
        )

    def verifier(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise refuser()

    try:
        info = os.stat(chemin, follow_symlinks=False)
    except FileNotFoundError:
        return valeurs, chemin
    except OSError:
        raise refuser() from None
    verifier(info)  # impérativement avant d'ouvrir ou de lire le contenu
    try:
        fd = os.open(chemin, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as fichier:
            ouvert = os.fstat(fichier.fileno())
            verifier(ouvert)
            if (info.st_dev, info.st_ino) != (ouvert.st_dev, ouvert.st_ino):
                raise refuser()
            for ligne in fichier:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#") or "=" not in ligne:
                    continue
                cle, valeur = ligne.split("=", 1)
                cle, valeur = cle.strip(), valeur.strip()
                if cle not in cles or cle in env:
                    continue
                if len(valeur) >= 2 and valeur[0] in ("'", '"') and valeur[-1] == valeur[0]:
                    valeur = valeur[1:-1]
                valeurs[cle] = valeur
    except (OSError, UnicodeError):
        # Jamais le texte brut de l'exception : il peut contenir des octets secrets.
        raise erreur(f"Impossible de lire le fichier privé : {chemin}. "
                     "Vérifie qu'il est lisible et enregistré en UTF-8.") from None
    return valeurs, chemin


def attendu(chemin):
    return f" Fichier privé attendu : {chemin}." if chemin else ""
