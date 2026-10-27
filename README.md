# Linky — MCP NexAura

Ton agent lit la consommation électrique de la maison (ou du local) du client, chez Enedis,
grâce au compteur Linky. **Lecture seule, rien d'autre.**

Fabriqué par NexAura, publié sous licence MIT.

## Ce qu'il fait

Quatre outils, que l'agent choisit tout seul selon la question :

| Outil | Il répond à… |
|---|---|
| `conso_veille` | « Combien j'ai consommé hier ? » (en kWh) |
| `conso_periode` | « Et la semaine dernière, jour par jour ? » (entre deux dates, avec total et moyenne) |
| `courbe_charge` | « À quelle heure ça consomme ? » (toutes les 30 minutes, 7 jours au plus) |
| `puissance_max` | « Mon abonnement est-il trop grand ? » (puissance la plus forte de chaque jour, en VA) |

## Ce qu'il lit

La consommation du compteur du client, via [Conso API](https://conso.boris.sh) (un service gratuit qui
passe par l'accès officiel d'Enedis). Les données d'un jour arrivent le lendemain vers 8 h : l'agent ne
peut donc pas voir aujourd'hui, seulement jusqu'à hier. La courbe toutes les 30 minutes ne marche que si
le client a activé « enregistrement de la courbe de charge » dans son espace Enedis.

## Ce qu'il ne fait jamais

- Il n'écrit rien, ne modifie rien, ne commande rien (pas de réglage du compteur, pas de changement de contrat).
- Il n'envoie aucune donnée ailleurs que chez Conso API.
- Il ne montre ni le jeton ni le numéro du compteur, ni à l'agent, ni dans le journal.
- Il ne redemande pas deux fois les mêmes données (mémoire de 6 h), pour respecter les règles de Conso API
  (5 demandes par seconde, 10 000 par heure, partagées entre tous les utilisateurs).

## Ce qu'il faut au client

1. Un compteur Linky avec la collecte activée dans son espace Enedis.
2. Un **jeton Conso API**, obtenu sur <https://conso.boris.sh> après avoir donné son accord chez Enedis
   (le jeton est personnel ; l'accord se retire à tout moment chez Enedis).
3. Le **numéro du compteur (PRM)**, 14 chiffres, visible dans l'espace Enedis ou sur une facture.

## Installer

Il faut Python 3.10 ou plus.

```bash
git clone https://github.com/Nexauraholding/mcp-linky.git
cd mcp-linky
python3 -m venv .venv
.venv/bin/pip install .
```

## Brancher dans Hermès

Dans `config.yaml` de l'agent :

```yaml
mcp_servers:
  linky:
    command: /chemin/vers/mcp-linky/.venv/bin/python
    args: ["-m", "nexaura_linky"]
    env:
      LINKY_TOKEN: "le-jeton-conso-api"
      LINKY_PRM: "12345678901234"
      LINKY_MCP_JOURNAL: "/chemin/vers/journal/linky.jsonl"   # facultatif
    tools:
      include: [conso_veille, conso_periode, courbe_charge, puissance_max]
```

`tools.include` est facultatif (les quatre outils sont déjà les seuls) ; il sert à retirer un outil,
par exemple `courbe_charge`, sans toucher au code.

## Brancher en http, avec un mandat (OAuth 2.1)

Le mode stdio ci-dessus reste le branchement local, sans jeton : c'est ce qu'Hermès fait aujourd'hui.
Quand le MCP tourne sur un serveur, il devient une **ressource protégée OAuth 2.1**, selon la
[spécification d'autorisation MCP](https://modelcontextprotocol.io/specification/draft/basic/authorization)
(version courante 2026-07-28) :

- il publie ses métadonnées de ressource protégée (RFC 9728) : `/.well-known/oauth-protected-resource/mcp`
  pour une ressource en `/mcp` ; une réponse 401 y renvoie le client ;
- il n'accepte qu'un jeton émis pour lui (audience = son adresse exacte, RFC 8707), signé, non expiré ;
- la seule portée est `linky:read` : **aucune portée d'écriture n'existe**. Un jeton qui n'a que
  `linky:write` ou `qonto:read` reçoit 403 ;
- le client mène le flux standard (code d'autorisation + PKCE + paramètre `resource`) : c'est le travail du
  client MCP, pas du nôtre.

### Le mandat

Le **mandat** est ce jeton court (15 minutes au plus), émis par le serveur d'autorisation selon le flux
standard, à partir de l'accord que l'humain a donné. Il dit :

| Dans le jeton | Sens |
|---|---|
| `sub` | pour qui l'agent agit (l'humain qui a donné son accord) |
| `client_id` | quel agent agit (le client OAuth) |
| `scope` | sur quoi : `linky:read` |
| `aud` | quel MCP : le sien, et aucun autre |
| `mandat.perimetre` | le périmètre : `outils` (liste d'outils permis), `depuis` (première date lisible, AAAA-MM-JJ) |
| `mandat.jusqu_a` | jusqu'à quand (secondes Unix) : le mandat fini est refusé même si le jeton vit encore |
| `mandat.id` | l'identifiant du mandat, écrit dans **chaque ligne du journal** |

Pas d'échange de jetons (RFC 8693) : la spécification MCP de base ne l'emploie pas, et Hermès sait déjà
faire le flux standard (voir ci-dessous). *(Changement du 27 oct. : la première version prévoyait cet échange.)*

Le jeton est refusé (401) s'il est expiré, mal signé, d'un autre émetteur, pour une autre audience, de type
autre que `at+jwt`, plus long que 15 minutes, ou sans mandat, sans agent, sans date de fin ; le mandat fini est
refusé de même. Chaque refus est noté au journal (raison courte, jamais le jeton).

### Lancer

```bash
.venv/bin/pip install ".[http]"
LINKY_MCP_TRANSPORT=http \
LINKY_MCP_ISSUER=https://auth.exemple.fr/realms/nexaura \
LINKY_MCP_RESOURCE=https://linky.exemple.fr/mcp \
LINKY_TOKEN=... LINKY_PRM=... \
.venv/bin/python -m nexaura_linky
```

Le serveur écoute en local (`127.0.0.1:8791`) : mets devant lui un proxy en https, et donne l'adresse publique
dans `LINKY_MCP_HOTES`. Un `LINKY_MCP_RESOURCE` en http n'est accepté que sur la machine locale.

### Côté Hermès : rien de maison

Hermès sait déjà faire OAuth 2.1 avec PKCE vers un MCP en http (`auth: oauth`, voir la
[documentation MCP d'Hermès](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)) : découverte, identification du client (CIMD, sinon enregistrement
dynamique), jeton, renouvellement.

```yaml
mcp_servers:
  linky:
    url: "https://linky.exemple.fr/mcp"
    auth: oauth
    oauth:
      scope: "linky:read"
```

### Quel serveur d'autorisation ?

Aucun n'est écrit ici : on prend une brique libre. Trois ont été regardés (sources officielles, 27 oct.) :

| | Keycloak (26.2 et plus) | oidc-provider (panva, Node) | Authlib (Python) |
|---|---|---|---|
| Nature | produit complet, écran d'accord, comptes, journal | bibliothèque certifiée OpenID | boîte à outils |
| Code + PKCE, jeton JWT (RFC 9068) | oui | oui | oui (9068 listé) |
| `resource` (RFC 8707) | **non géré** (dit dans sa doc) : l'audience se règle par la portée | oui, natif | non listé |
| Login, accord, comptes | déjà là | à écrire | à écrire |

**Keycloak** évite d'écrire un serveur d'autorisation (écran d'accord, comptes et journal des connexions existent).
Ses limites : pas de paramètre `resource` (on lie l'audience à la portée `linky:read` par un mappeur d'audience),
et pas de fin automatique d'un accord (le mandat `jusqu_a` et le périmètre viennent d'attributs posés au moment
de l'accord). Si ces limites gênent, oidc-provider est le repli.

Les tests utilisent un faux serveur d'autorisation (`tests/faux_as.py`), qui fait le flux standard.

## Le journal

Chaque appel ajoute une ligne dans un fichier local (par défaut `~/.nexaura/journal/linky.jsonl`,
réglable avec `LINKY_MCP_JOURNAL`). Une ligne = un appel, lisible par un humain :

```json
{"quand": "2026-10-28T07:31:02+00:00", "service": "linky", "outil": "conso_periode", "parametres": {"debut": "2026-10-20", "fin": "2026-10-26"}, "acces": "lecture", "duree_ms": 412, "resultat": "ok"}
```

En mode http, la ligne porte aussi `mandat` (l'identifiant), `pour` et `agent` ; en stdio, `mandat` vaut `null`.
Un appel hors périmètre, ou un jeton refusé, s'écrit `resultat: refuse`.

En cas d'erreur, `resultat` vaut `erreur` et un court texte l'explique. Jamais de jeton ni de PRM :
même dans les messages d'erreur, ils sont remplacés par `***`.

## Les accès dans un fichier privé

Copie `secrets.exemple` dans un dossier privé, par exemple `~/.nexaura/acces/linky.env`,
puis protège-le avec `chmod 600 ~/.nexaura/acces/linky.env` avant d'y mettre tes vrais accès.
Le fichier doit t'appartenir et être un fichier ordinaire, sans lien symbolique.
Ne le mets jamais dans Git.

Dans le bloc `env` de la configuration ci-dessus, remplace les accès par ce chemin :

```yaml
      LINKY_SECRETS_FILE: "~/.nexaura/acces/linky.env"
```

Écris une ligne `CLE=valeur` par accès. Les lignes vides et les commentaires commençant
par `#` sont ignorés ; les espaces autour des clés et valeurs et les guillemets simples
ou doubles autour des valeurs sont retirés. Seules les clés `LINKY_TOKEN` et `LINKY_PRM` sont lues.
Une variable déjà présente dans l'environnement garde la priorité, même vide.
Si un accès obligatoire manque et que le fichier est absent ou vide, l'erreur indique son chemin.

## Variables

| Variable | Rôle |
|---|---|
| `LINKY_SECRETS_FILE` | Chemin du fichier privé des accès (facultatif, `~` accepté). |
| `LINKY_TOKEN` | jeton Conso API (obligatoire) |
| `LINKY_PRM` | numéro du compteur, 14 chiffres (obligatoire) |
| `LINKY_MCP_JOURNAL` | emplacement du journal (facultatif) |
| `LINKY_BASE_URL` | autre adresse d'API, pour les tests (facultatif) |
| `LINKY_MCP_TRANSPORT` | `stdio` (défaut) ou `http` |
| `LINKY_MCP_ISSUER`, `LINKY_MCP_RESOURCE` | http : serveur d'autorisation, adresse publique de ce MCP (obligatoires) |
| `LINKY_MCP_JWKS_URL` | http : adresse des clés publiques (sinon trouvée par RFC 8414) |
| `LINKY_MCP_HOST`, `LINKY_MCP_PORT`, `LINKY_MCP_HOTES` | http : écoute (127.0.0.1:8791) et noms d'hôte publics |

## Tests

```bash
.venv/bin/pip install ".[test]"   # PyJWT et pytest
.venv/bin/python -m pytest
```

Les tests utilisent de fausses réponses écrites à la main, une fausse API locale et un faux serveur d'autorisation : aucun appel réel (ni Enedis, ni serveur d'autorisation).

## Licence

MIT (voir `LICENSE`). Les formats de réponse viennent de la documentation publique de Conso API ;
aucun code de `bokub/linky` (GPL-3.0) n'est repris.
