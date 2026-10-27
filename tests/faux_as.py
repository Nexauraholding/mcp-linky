"""Faux serveur d'autorisation pour les tests : aucune vraie clé, aucun vrai compte.

Il fait ce que ferait un vrai (Keycloak, oidc-provider...) juste assez pour les tests, selon le flux
standard de la spécification MCP : code d'autorisation + PKCE (S256) + paramètre `resource` (RFC 8707).
  - il publie ses clés (JWKS) et ses métadonnées (RFC 8414) ;
  - l'accord de l'humain (qui, pour quel agent, quelles portées, jusqu'à quand, quel périmètre) y est
    enregistré à l'avance, comme le ferait l'écran de consentement ;
  - le jeton émis est un « mandat » court : `sub` = l'humain, `client_id` = l'agent, `aud` = le MCP visé.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm



class FauxAS:
    def __init__(self):
        self.cle = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.autre_cle = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # un faux émetteur
        self.kid = "cle-de-test"
        self.accords: dict[tuple[str, str], dict] = {}  # (humain, client_id) -> accord enregistré
        self.codes: dict[str, dict] = {}
        self.demandes: list[dict] = []  # ce que le point /token a reçu
        srv = HTTPServer(("127.0.0.1", 0), self._gestionnaire())
        self._srv = srv
        self.issuer = f"http://127.0.0.1:{srv.server_port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def arreter(self):
        self._srv.shutdown()

    # ----- ce que publie un vrai serveur d'autorisation
    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/jwks"

    def _jwks(self) -> dict:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.cle.public_key()))
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [jwk]}

    # ----- fabrication directe d'un jeton (pour fabriquer aussi des jetons fautifs)
    def jeton(self, *, ressource: str, scope: str = "linky:read", sub: str = "alice",
              agent: str | None = "agent-demo", mandat: dict | None = ..., duree: int = 600,
              iss: str | None = None, decalage: int = 0, cle=None, typ: str = "at+jwt",
              extra: dict | None = None) -> str:
        maintenant = int(time.time()) + decalage
        c = {"iss": iss or self.issuer, "aud": ressource, "sub": sub, "iat": maintenant,
             "exp": maintenant + duree, "jti": uuid.uuid4().hex, "scope": scope}
        if agent:
            c["client_id"] = agent
        if mandat is ...:
            mandat = {"id": "mdt-essai-1", "jusqu_a": maintenant + 86400, "perimetre": {}}
        if mandat is not None:
            c["mandat"] = mandat
        c.update(extra or {})
        return jwt.encode(c, cle or self.cle, algorithm="RS256", headers={"kid": self.kid, "typ": typ})

    def consentir(self, humain: str, agent: str, *, portees: list[str], duree: int = 86400,
                  perimetre: dict | None = None, id: str | None = None) -> str:
        """L'accord de l'humain : ce que l'humain signe avant de donner un mandat."""
        m = {"id": id or f"mdt-{uuid.uuid4().hex[:8]}", "portees": portees,
             "jusqu_a": int(time.time()) + duree, "perimetre": perimetre or {}}
        self.accords[(humain, agent)] = m
        return m["id"]

    def autoriser(self, humain: str, agent: str, *, ressource: str, scope: str, code_challenge: str) -> str:
        """Le navigateur : l'humain approuve, le serveur rend un code (flux code + PKCE)."""
        m = self.accords.get((humain, agent))
        demandees = scope.split()
        if not m or time.time() >= m["jusqu_a"] or not demandees or not set(demandees) <= set(m["portees"]):
            raise PermissionError("access_denied")
        code = uuid.uuid4().hex
        self.codes[code] = {"humain": humain, "agent": agent, "ressource": ressource, "scope": scope,
                            "challenge": code_challenge}
        return code

    def _emettre(self, humain, agent, ressource, scope) -> dict:
        m = self.accords[(humain, agent)]
        duree = min(600, int(m["jusqu_a"] - time.time()))
        jeton = self.jeton(ressource=ressource, scope=scope, sub=humain, agent=agent, duree=duree,
                           mandat={"id": m["id"], "jusqu_a": m["jusqu_a"], "perimetre": m["perimetre"]})
        return {"access_token": jeton, "token_type": "Bearer", "expires_in": duree, "scope": scope}

    def _jeton(self, f: dict[str, str]) -> tuple[int, dict]:
        self.demandes.append(f)
        g = f.get("grant_type")
        if g == "authorization_code":
            c = self.codes.pop(f.get("code", ""), None)
            if not c or c["agent"] != f.get("client_id"):
                return 400, {"error": "invalid_grant"}
            verif = base64.urlsafe_b64encode(
                hashlib.sha256(f.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
            if verif != c["challenge"]:
                return 400, {"error": "invalid_grant", "error_description": "PKCE"}
            if f.get("resource") != c["ressource"]:  # RFC 8707 : la même ressource aux deux étapes
                return 400, {"error": "invalid_target"}
            rep = self._emettre(c["humain"], c["agent"], c["ressource"], c["scope"])
            rep["refresh_token"] = f"rt-{c['humain']}|{c['agent']}|{c['ressource']}|{c['scope']}"
            return 200, rep
        if g == "refresh_token":
            try:
                humain, agent, ressource, scope = f["refresh_token"][3:].split("|")
            except (KeyError, ValueError):
                return 400, {"error": "invalid_grant"}
            m = self.accords.get((humain, agent))
            if not m or time.time() >= m["jusqu_a"]:  # l'accord est fini : plus de jeton neuf
                return 400, {"error": "invalid_grant", "error_description": "accord fini"}
            return 200, self._emettre(humain, agent, ressource, scope)
        return 400, {"error": "unsupported_grant_type"}

    def _gestionnaire(self):
        racine = self

        class H(BaseHTTPRequestHandler):
            def _rep(self, code, corps):
                b = json.dumps(corps).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):  # noqa: N802
                if self.path == "/jwks":
                    return self._rep(200, racine._jwks())
                if self.path == "/.well-known/oauth-authorization-server":
                    return self._rep(200, {"issuer": racine.issuer, "jwks_uri": racine.jwks_url,
                                           "token_endpoint": racine.issuer + "/token",
                                           "grant_types_supported": ["authorization_code", "refresh_token"],
                                           "code_challenge_methods_supported": ["S256"]})
                self._rep(404, {})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                f = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                if self.path == "/token":
                    return self._rep(*racine._jeton(f))
                self._rep(404, {})

            def log_message(self, *a):
                pass
        return H
