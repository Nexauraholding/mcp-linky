"""Mode http : ressource protégée OAuth 2.1 + mandat. Faux serveur d'autorisation, fausse API Enedis."""
import asyncio
import base64
import datetime
import hashlib
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

import pytest

from conftest import PRM, TOKEN, FauxReseau, reponse_quotidienne
from faux_as import FauxAS

pytest.importorskip("mcp")
pytest.importorskip("jwt")
import uvicorn  # noqa: E402

from nexaura_linky import autorisation, outils  # noqa: E402
from nexaura_linky.client import ClientLinky, LinkyError  # noqa: E402
from nexaura_linky.server import PORTEE, creer_serveur  # noqa: E402


# ---------- outillage ----------

def _port_libre() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def faux_as():
    a = FauxAS()
    yield a
    a.arreter()


@pytest.fixture(scope="module")
def mcp_http(faux_as, tmp_path_factory):
    """Le vrai serveur MCP en http, sur la machine locale, contre une fausse API Enedis."""
    mp = pytest.MonkeyPatch()
    mp.setattr(outils, "aujourdhui", lambda: datetime.date(2026, 5, 10))
    port = _port_libre()
    ressource = f"http://127.0.0.1:{port}/mcp"
    journal = tmp_path_factory.mktemp("j") / "linky.jsonl"
    reseau = FauxReseau(corps=reponse_quotidienne([("2026-05-01", 6261), ("2026-05-02", 4882)]))
    cfg = autorisation.ConfigAutorisation(faux_as.issuer, ressource, PORTEE, faux_as.jwks_url)
    serveur = creer_serveur(ClientLinky(token=TOKEN, prm=PRM, fetch=reseau, base="http://faux/api"),
                            journal, cfg)
    u = uvicorn.Server(uvicorn.Config(serveur.streamable_http_app(), host="127.0.0.1", port=port,
                                      log_level="error"))
    threading.Thread(target=u.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield {"url": ressource, "base": f"http://127.0.0.1:{port}", "journal": journal, "serveur": serveur,
           "reseau": reseau}
    u.should_exit = True
    mp.undo()


@pytest.fixture(autouse=True)
def journal_vide(request):
    if "mcp_http" in request.fixturenames:
        j = request.getfixturevalue("mcp_http")["journal"]
        j.write_text("", encoding="utf-8")


def lignes(j):
    return [json.loads(x) for x in j.read_text(encoding="utf-8").splitlines()]


def poster(url: str, jeton: str | None):
    """Une requête brute vers le MCP : renvoie (code, en-têtes, corps)."""
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if jeton:
        h["Authorization"] = f"Bearer {jeton}"
    req = urllib.request.Request(url, data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@asynccontextmanager
async def client_mcp(url: str, jeton: str):
    """Un client MCP http, avec le jeton en en-tête (SDK 2.x, sinon 1.x)."""
    try:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        hc = httpx2.AsyncClient(headers={"Authorization": f"Bearer {jeton}"})
        async with Client(streamable_http_client(url, http_client=hc)) as c:
            yield c
    except ImportError:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {jeton}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s


def appeler(url: str, jeton: str, outil: str, args: dict | None = None):
    async def go():
        async with client_mcp(url, jeton) as c:
            return await c.call_tool(outil, args or {})
    return asyncio.run(go())


def erreur_de(res) -> bool:
    return bool(getattr(res, "is_error", None) or getattr(res, "isError", None))


def texte(res) -> str:
    return "".join(c.text for c in res.content if hasattr(c, "text"))


def post_as(a: FauxAS, champs: dict):
    corps = urllib.parse.urlencode(champs).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(a.issuer + "/token", data=corps)) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def obtenir_mandat(a: FauxAS, ressource: str, scope: str, humain="alice", agent="agent-demo"):
    """Le flux standard de la spécification MCP, tel que le mène Hermès (`auth: oauth`) :
    code d'autorisation + PKCE (S256) + paramètre `resource` (RFC 8707) aux deux étapes."""
    verif = secrets.token_urlsafe(48)
    defi = base64.urlsafe_b64encode(hashlib.sha256(verif.encode()).digest()).rstrip(b"=").decode()
    code = a.autoriser(humain, agent, ressource=ressource, scope=scope, code_challenge=defi)
    return post_as(a, {"grant_type": "authorization_code", "code": code, "code_verifier": verif,
                       "client_id": agent, "resource": ressource})


# ---------- publication RFC 9728 ----------

def test_metadonnees_de_ressource_protegee(mcp_http, faux_as):
    code, _, corps = (lambda r: (r.status, r.headers, r.read()))(
        urllib.request.urlopen(mcp_http["base"] + "/.well-known/oauth-protected-resource/mcp"))
    meta = json.loads(corps)
    assert code == 200
    assert meta["resource"] == mcp_http["url"]
    assert [a.rstrip("/") for a in meta["authorization_servers"]] == [faux_as.issuer]  # SDK 1.x ajoute « / »
    assert meta["scopes_supported"] == ["linky:read"]  # aucune portée d'écriture
    assert meta["bearer_methods_supported"] == ["header"]


def test_sans_jeton_401_qui_indique_les_metadonnees(mcp_http):
    code, h, _ = poster(mcp_http["url"], None)
    assert code == 401
    assert "resource_metadata=" in h["WWW-Authenticate"]
    assert "/.well-known/oauth-protected-resource" in h["WWW-Authenticate"]


# ---------- jetons refusés ----------

def _fautes(a: FauxAS, res: str):
    return {
        "mauvaise_audience": a.jeton(ressource="https://autre-mcp.example/mcp"),
        "jeton_expire": a.jeton(ressource=res, decalage=-3600),
        "mauvais_emetteur": a.jeton(ressource=res, iss="https://faux-emetteur.example"),
        "jeton_trop_long": a.jeton(ressource=res, duree=7200),
        "agent_absent": a.jeton(ressource=res, agent=None),
        "mandat_absent": a.jeton(ressource=res, mandat=None),
        "mandat_expire": a.jeton(ressource=res, mandat={"id": "mdt-fini", "jusqu_a": int(time.time()) - 5}),
        "mandat_sans_fin": a.jeton(ressource=res, mandat={"id": "mdt-sans-fin"}),
        "jeton_invalide": a.jeton(ressource=res, cle=a.autre_cle),  # signé par un autre : kid connu, signature fausse
        "type_refuse": a.jeton(ressource=res, typ="JWT"),
    }


@pytest.mark.parametrize("raison", ["mauvaise_audience", "jeton_expire", "mauvais_emetteur", "jeton_trop_long",
                                    "agent_absent", "mandat_absent", "mandat_expire", "mandat_sans_fin",
                                    "jeton_invalide", "type_refuse"])
def test_jeton_refuse_401_et_journal(mcp_http, faux_as, raison):
    jeton = _fautes(faux_as, mcp_http["url"])[raison]
    code, h, _ = poster(mcp_http["url"], jeton)
    assert code == 401
    assert 'error="invalid_token"' in h["WWW-Authenticate"]
    brut = mcp_http["journal"].read_text(encoding="utf-8")
    ligne = lignes(mcp_http["journal"])[-1]
    assert ligne["resultat"] == "refuse" and ligne["erreur"] == raison and ligne["mandat"] is None
    assert jeton not in brut and jeton.split(".")[1] not in brut  # jamais le jeton dans le journal


def test_algorithme_none_et_hmac_refuses(mcp_http, faux_as):
    import base64
    import jwt as pyjwt

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    c = {"iss": faux_as.issuer, "aud": mcp_http["url"], "sub": "alice", "iat": int(time.time()),
         "exp": int(time.time()) + 300, "jti": "x", "scope": "linky:read", "client_id": "agent-demo",
         "mandat": {"id": "m", "jusqu_a": int(time.time()) + 999}}
    sans = f'{b64({"alg": "none", "typ": "at+jwt"})}.{b64(c)}.'
    hmac = pyjwt.encode(c, "secret-de-test-assez-long-pour-hs256-0123456789", algorithm="HS256",
                        headers={"typ": "at+jwt", "kid": faux_as.kid})
    for jeton in (sans, hmac):
        assert poster(mcp_http["url"], jeton)[0] == 401


def test_mauvaise_portee_403(mcp_http, faux_as):
    for scope in ("linky:write", "qonto:read", "openid"):
        code, h, _ = poster(mcp_http["url"], faux_as.jeton(ressource=mcp_http["url"], scope=scope))
        assert code == 403, scope
        assert 'error="insufficient_scope"' in h["WWW-Authenticate"]
        assert "linky:read" in h["WWW-Authenticate"]  # la portée attendue est dite au client


def test_portee_d_ecriture_en_plus_n_ouvre_rien(mcp_http, faux_as):
    """Un jeton « linky:read linky:write » est accepté pour la lecture ; l'écriture n'existe pas."""
    jeton = faux_as.jeton(ressource=mcp_http["url"], scope="linky:read linky:write")
    res = appeler(mcp_http["url"], jeton, "conso_veille")
    assert not erreur_de(res)
    async def noms():
        async with client_mcp(mcp_http["url"], jeton) as c:
            return [t.name for t in (await c.list_tools()).tools]
    assert sorted(asyncio.run(noms())) == ["conso_periode", "conso_veille", "courbe_charge", "puissance_max"]


# ---------- le mandat : flux standard, puis appel, puis journal ----------

def test_flux_standard_puis_appel_journalise_le_mandat(mcp_http, faux_as):
    id_ = faux_as.consentir("alice", "agent-demo", portees=["linky:read"], id="mdt-linky-42")
    code, rep = obtenir_mandat(faux_as, mcp_http["url"], "linky:read")
    assert code == 200 and rep["expires_in"] <= 600  # un jeton court
    assert faux_as.demandes[-1]["resource"] == mcp_http["url"]  # RFC 8707 : le jeton vise ce MCP seul
    res = appeler(mcp_http["url"], rep["access_token"], "conso_periode", {"debut": "2026-05-01", "fin": "2026-05-02"})
    assert not erreur_de(res) and "11.143" in texte(res)
    ligne = lignes(mcp_http["journal"])[-1]
    assert ligne["outil"] == "conso_periode" and ligne["resultat"] == "ok" and ligne["acces"] == "lecture"
    assert ligne["mandat"] == id_ == "mdt-linky-42"
    assert ligne["pour"] == "alice" and ligne["agent"] == "agent-demo"
    brut = mcp_http["journal"].read_text(encoding="utf-8")
    assert rep["access_token"] not in brut and TOKEN not in brut and PRM not in brut


def test_chaque_appel_porte_le_mandat(mcp_http, faux_as):
    faux_as.consentir("alice", "agent-demo", portees=["linky:read"], id="mdt-a")
    jeton = obtenir_mandat(faux_as, mcp_http["url"], "linky:read")[1]["access_token"]
    appeler(mcp_http["url"], jeton, "conso_veille")
    appeler(mcp_http["url"], jeton, "puissance_max")
    appeler(mcp_http["url"], jeton, "conso_periode", {"debut": "9999", "fin": "x"})  # erreur de l'outil
    ls = lignes(mcp_http["journal"])
    assert [l["resultat"] for l in ls] == ["ok", "ok", "erreur"]
    assert {l["mandat"] for l in ls} == {"mdt-a"}


def test_le_serveur_d_autorisation_ne_donne_pas_une_portee_d_ecriture(mcp_http, faux_as):
    faux_as.consentir("alice", "agent-demo", portees=["linky:read"])
    with pytest.raises(PermissionError):
        obtenir_mandat(faux_as, mcp_http["url"], "linky:write")


def test_mandat_fini_plus_de_jeton_neuf(mcp_http, faux_as):
    """Quand l'accord est fini, le renouvellement est refusé : l'agent n'a plus de jeton."""
    faux_as.consentir("alice", "agent-demo", portees=["linky:read"], duree=1)
    rep = obtenir_mandat(faux_as, mcp_http["url"], "linky:read")[1]
    time.sleep(1.2)
    code, err = post_as(faux_as, {"grant_type": "refresh_token", "refresh_token": rep["refresh_token"]})
    assert code == 400 and err["error"] == "invalid_grant"


def test_mandat_fini_mais_jeton_encore_vivant_refuse(mcp_http, faux_as):
    """Le jeton court n'a pas encore expiré, mais le mandat est fini : refus à la porte."""
    jeton = faux_as.jeton(ressource=mcp_http["url"], duree=600,
                          mandat={"id": "mdt-fini", "jusqu_a": int(time.time()) + 2})
    assert not erreur_de(appeler(mcp_http["url"], jeton, "conso_veille"))
    time.sleep(2.2)
    assert poster(mcp_http["url"], jeton)[0] == 401
    assert lignes(mcp_http["journal"])[-1]["erreur"] == "mandat_expire"


# ---------- le périmètre du mandat ----------

def test_perimetre_outils(mcp_http, faux_as):
    jeton = faux_as.jeton(ressource=mcp_http["url"], mandat={
        "id": "mdt-p1", "jusqu_a": int(time.time()) + 3600, "perimetre": {"outils": ["conso_veille"]}})
    assert not erreur_de(appeler(mcp_http["url"], jeton, "conso_veille"))
    res = appeler(mcp_http["url"], jeton, "courbe_charge")
    assert erreur_de(res) and "ne couvre pas" in texte(res)
    dernier = lignes(mcp_http["journal"])[-1]
    assert dernier["resultat"] == "refuse" and dernier["mandat"] == "mdt-p1" and dernier["outil"] == "courbe_charge"


def test_perimetre_refuse_avant_tout_appel_a_enedis(mcp_http, faux_as):
    avant = len(mcp_http["reseau"].appels)
    jeton = faux_as.jeton(ressource=mcp_http["url"], mandat={
        "id": "mdt-p2", "jusqu_a": int(time.time()) + 3600, "perimetre": {"depuis": "2026-05-02"}})
    res = appeler(mcp_http["url"], jeton, "conso_periode", {"debut": "2026-05-01", "fin": "2026-05-02"})
    assert erreur_de(res) and "2026-05-02" in texte(res)
    assert len(mcp_http["reseau"].appels) == avant
    assert lignes(mcp_http["journal"])[-1]["resultat"] == "refuse"
    assert not erreur_de(appeler(mcp_http["url"], jeton, "conso_periode", {"debut": "2026-05-02", "fin": "2026-05-02"}))


# ---------- il n'y a toujours aucune écriture ----------

def test_aucune_ecriture_possible_en_http(mcp_http, faux_as):
    jeton = faux_as.jeton(ressource=mcp_http["url"])

    async def go():
        async with client_mcp(mcp_http["url"], jeton) as c:
            return (await c.list_tools()).tools
    outils_ = asyncio.run(go())
    assert len(outils_) == 4
    for t in outils_:
        assert "Lecture seule" in t.description
        assert not any(m in t.name for m in ("ecrire", "modifier", "supprimer", "write", "set", "update", "delete"))
    assert PORTEE == "linky:read" and autorisation.ConfigAutorisation("a", "b", PORTEE).portee == "linky:read"
    # Enedis n'est interrogé qu'en lecture : la fausse API n'a reçu que des GET (le client n'en sait pas plus)
    import inspect

    from nexaura_linky import client
    src = inspect.getsource(client)
    assert "POST" not in src and "PUT" not in src and "DELETE" not in src and "PATCH" not in src


# ---------- stdio reste possible ----------

def test_stdio_local_sans_mandat(tmp_path):
    j = tmp_path / "j.jsonl"
    r = FauxReseau(corps=reponse_quotidienne([("2026-05-01", 1000)]))
    srv = creer_serveur(ClientLinky(token=TOKEN, prm=PRM, fetch=r, base="http://faux/api"), j)
    srv.outils_directs["conso_veille"]()
    ligne = lignes(j)[-1]
    assert ligne["resultat"] in ("ok", "erreur") and ligne["mandat"] is None and "pour" not in ligne


def test_http_sans_contexte_de_jeton_refuse(mcp_http):
    """Si le jeton manquait par accident (appel direct), l'outil ne retombe pas sur le mode local."""
    with pytest.raises(LinkyError, match="Aucun mandat"):
        mcp_http["serveur"].outils_directs["conso_veille"]()
    assert lignes(mcp_http["journal"])[-1]["resultat"] == "refuse"


# ---------- réglages ----------

def test_decouverte_des_cles(faux_as):
    assert autorisation.trouver_jwks(faux_as.issuer) == faux_as.jwks_url
    with pytest.raises(ValueError):
        autorisation.trouver_jwks("http://127.0.0.1:9", delai=0.5)


def test_reglages_obligatoires_et_http_seulement_en_local():
    with pytest.raises(ValueError, match="obligatoires"):
        autorisation.ConfigAutorisation.depuis_env("LINKY_MCP", PORTEE, {})
    cfg = autorisation.ConfigAutorisation.depuis_env(
        "LINKY_MCP", PORTEE, {"LINKY_MCP_ISSUER": "https://auth.example", "LINKY_MCP_RESOURCE": "http://mcp.example/mcp"})
    with pytest.raises(ValueError, match="http"):
        cfg.valider()
    autorisation.ConfigAutorisation("https://auth.example", "https://mcp.example/mcp", PORTEE).valider()
