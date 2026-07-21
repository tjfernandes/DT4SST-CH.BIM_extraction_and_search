# HBIM-040 — Deterministic Router

> **Tipo:** especificação executável de issue.
> **Branch obrigatória:** `feat/hbim-040-deterministic-router`.
> **Depende de:** HBIM-002/003/004 (settings, API, CI), HBIM-005 (harness de
> avaliação), HBIM-022 (indexers canónicos) — todos merged em `main` (`ef98d23`).
> **Bloqueia:** HBIM-041 (query parser determinístico), HBIM-042, HBIM-050.

---

## 1. Contexto e problema observado

O routing é hoje feito por um LLM. Em `backend/api/main.py:271–278` o endpoint
`/chat` chama `CLASSIFY_INTENT` e deriva **todo** o control flow do resultado:

```python
classify_prompt = CLASSIFY_INTENT.format(user_input=effective_query)
classify_message = get_response(classify_prompt, history, {"type": "json_object"})
classify_result = ClassifyResult.model_validate_json(classify_message.content)
needs_search   = classify_result.search_strategy not in ("chat", "aggregation", "detail")
is_aggregation = classify_result.search_strategy == "aggregation"
is_detail      = classify_result.search_strategy == "detail"
```

`classify_result.search_strategy` é ainda lido em `main.py:312` (decide se há
embedding) e `main.py:323` (entra no `SearchPlan`). São **quatro** consumidores.

Consequências verificadas no código:

- a mesma pergunta pode encaminhar de forma diferente entre execuções;
- o routing não é testável sem rede nem sem chave de LLM;
- não existe nenhuma métrica de routing (`grep -rn "routing" backend/eval/`
  devolve zero linhas);
- não existe `backend/retrieval/` (o diretório não existe no repositório);
- não existe `backend/eval/dataset/routing_gold.jsonl`.

O ROADMAP §829–833 e §404–446 (M4) e a decisão-mãe
`docs/architecture/HBIM_RAG_DECISIONS.md` §6 (*"O router deve ser uma funcao
Python testavel. AMALIA nao deve classificar a query no caminho principal."*)
exigem substituir isto por uma função determinística.

---

## 2. Precedência de fontes

1. **Código e testes implementados** em `main` (`ef98d23`).
2. `docs/architecture/HBIM_RAG_DECISIONS.md` §6 — regras e termos de routing.
3. `docs/implementation/ROADMAP.md` §404–446 (M4) e §829–833 (HBIM-040).
4. `docs/implementation/IMPLEMENTATION_STATUS.md`.
5. Esta especificação (decisões de review, §3).

Onde o ROADMAP e o código divergirem, prevalece o código observado e esta spec
regista a reconciliação (§3).

---

## 3. Conflitos ROADMAP ↔ código e decisões fixadas

Cada linha foi verificada no repositório antes de ser escrita.

### C1 — `routing_gold.jsonl` não existe, apesar de ser dependência declarada

- **ROADMAP §832:** *"Dependências. HBIM-022, **HBIM-005** (harness +
  `routing_gold` têm de existir antes de mexer no retrieval)"*.
- **Observado:** `backend/eval/dataset/` contém apenas `corpus.jsonl`,
  `queries.jsonl`, `qrels.jsonl`, `dataset.json`. O `routing_gold.jsonl` **não
  existe**. A HBIM-005 implementada limitou-se às categorias de retrieval já
  suportadas; routing não existia.
- **DECISÃO:** a HBIM-040 **cria** `backend/eval/dataset/routing_gold.jsonl`.
  Isto é coerente com o ROADMAP M4 (§417), que lista esse ficheiro entre os
  **novos ficheiros de M4** — a milestone a que a HBIM-040 pertence. Não é
  expansão de scope: sem gold não existe critério de aceitação verificável.

### C2 — Vocabulário de rotas incompatível com o legacy; falta `chat`

- **ROADMAP §418:** `class Route(str, Enum): exact_lookup; aggregation;
  structured; graph; multimodal; document_hybrid; hybrid_semantic` (7 membros).
- **Observado:** o contrato legacy `search_strategy` (`backend/api/search.py:30`
  e `:66`) é `{chat, structured, semantic, aggregation, detail}`. **`chat` não
  tem equivalente** no enum do ROADMAP.
- **Consequência:** remover `CLASSIFY_INTENT` sem substituir a deteção de `chat`
  enviaria `"olá, como estás?"` para retrieval, ou obrigaria a manter o LLM para
  essa decisão — violando o critério *"0 LLM no routing"*.
- **DECISÃO:** o enum `Route` tem **oito** membros: os sete do ROADMAP **mais
  `CHAT`**. A extensão é obrigatória para satisfazer o próprio critério de
  aceitação do ROADMAP e está registada aqui como desvio deliberado e
  justificado. Nenhum outro membro é acrescentado.

### C3 — Três rotas não têm backend implementado

- `GRAPH` exige Neo4j (HBIM-082, não implementado — não existe `shared/neo4j.py`
  nem `retrieval/graph.py`).
- `MULTIMODAL` exige `hbim_media_v1` (HBIM-090, não implementado).
- `DOCUMENT_HYBRID` exige `hbim_chunks_v1` (HBIM-070, não implementado; a
  HBIM-020 §52 adiou chunks explicitamente).
- **Consequência:** se o router emitisse estas rotas e o endpoint as despachasse,
  `/chat` falharia em runtime.
- **DECISÃO:** o router é a **única fonte de verdade de routing** e emite o
  vocabulário completo de oito rotas — só assim o `routing_gold` pode afirmar a
  rota verdadeira e a HBIM-070/082/090 herdam um router já correto. O endpoint
  aplica uma **capability map explícita e testada** (§10.3) que degrada as rotas
  sem backend para uma estratégia executável e regista a degradação no evento
  `router_decision` (canal universal) e, onde exista plano, também no plano
  (§10.5). A degradação é propriedade do *endpoint*, nunca do router: a
  `decision.route` nunca é reescrita.

### C4 — O pacote `backend/retrieval/` não existe

- **DECISÃO:** a HBIM-040 cria `backend/retrieval/__init__.py` e
  `backend/retrieval/router.py`. Não cria `query_parser.py` (HBIM-041) nem
  `lexical.py` (HBIM-042).

### C5 — Tooling não conhece o pacote `retrieval`

- **Observado:** `pyproject.toml:38`
  `known-first-party = ["api", "ingestion", "shared", "tests", "eval", "canonical"]`
  — **não inclui `retrieval`**; não existe override strict de mypy para
  `retrieval.*`; `.github/workflows/ci.yml` não lista nenhum ficheiro de
  `retrieval`.
- **DECISÃO:** a HBIM-040 acrescenta `"retrieval"` a `known-first-party`, e
  `retrieval.router` (+ `retrieval`) ao override strict de `pyproject.toml` **e**
  à lista explícita de `mypy` em `ci.yml` — o gate só é efetivo nos dois sítios
  (precedente HBIM-021 §24, HBIM-022 §28).

### C6 — O router não pode receber texto produzido por LLM

- **Observado:** `main.py:262–267` reescreve a pergunta com `REWRITE_QUERY`
  (LLM) e `main.py:271` classifica sobre `effective_query` (o texto reescrito).
- **Consequência:** mesmo com um router puro, o routing end-to-end seria
  não determinístico, porque a sua entrada é output de LLM.
- **DECISÃO:** o router é invocado sobre **`request.message` verbatim** (a
  pergunta do utilizador), nunca sobre `effective_query`. A deteção de follow-up
  usa o sinal determinístico `has_previous_results` do `RouterContext`
  (§9.2), exatamente como `references_previous_result(query)` em §6. O
  `REWRITE_QUERY` permanece **apenas** para alimentar a extração a jusante
  (território da HBIM-041) e §6 autoriza-o explicitamente (*"AMALIA pode
  reescrever follow-ups conversacionais"*). Um teste obrigatório prova que o
  router nunca é chamado com output de LLM (§19.7).

### C7 — Perguntas definicionais: desvio deliberado ao prompt legacy

- **Observado:** `CLASSIFY_INTENT` classifica `"o que é o IFC?"` como `chat`.
- **Problema:** uma regra determinística que mande para `CHAT` tudo o que
  "parece definicional" é ambígua e não testável sem enumerar linguagem natural
  aberta.
- **DECISÃO:** `CHAT` é decidido por um **conjunto fechado** de padrões
  conversacionais (§11.3) avaliado em **último** lugar. Perguntas definicionais
  que não pertençam a esse conjunto fechado caem em `HYBRID_SEMANTIC`. Isto é um
  desvio consciente ao prompt legacy, na direção segura: uma pesquisa que devolve
  zero resultados é recuperável; recusar pesquisar não é. Registado em §25.

---

## 4. Objetivos

1. `backend/retrieval/router.py` com uma função pura
   `route(query: str, context: RouterContext) -> RoutingDecision`, que implementa
   integralmente as regras e os termos de `HBIM_RAG_DECISIONS.md` §6.
2. **Zero LLM no routing**: o endpoint `/chat` deixa de chamar `CLASSIFY_INTENT`.
3. **Determinismo total**: a mesma `(query, context)` produz sempre a mesma
   `RoutingDecision`, em qualquer ordem de testes e em qualquer máquina.
4. `backend/eval/dataset/routing_gold.jsonl` versionado, com uma métrica
   `routing_accuracy` **offline** (sem Docker, sem OpenSearch, sem rede).
5. **Routing accuracy ≥ 0.95** no gold, verificada por teste que falha o CI.
6. O endpoint permanece funcional: rotas sem backend degradam de forma explícita,
   determinística e observável — nunca com erro 500.
7. Nenhuma regressão: HBIM-005 baseline byte-idêntica, suites existentes verdes.

---

## 5. Não objetivos

Fora de scope, sem exceção:

- `retrieval/query_parser.py` e a remoção de `EXTRACT_IFC_CLASS` /
  `EXTRACT_FILTERS` / `EXTRACT_CONDITIONS` / `EXTRACT_AGGREGATION` /
  `EXTRACT_DETAIL_REF` / `EXTRACT_EMBEDDING_QUERY` (HBIM-041);
- `retrieval/lexical.py`, aplicar filtros material/storey/name, corrigir a
  agregação de classificação (HBIM-042);
- remover `FILTER_RESULTS_BATCH` (HBIM-051);
- BM25/dense/RRF/reranker/EvidencePack (HBIM-050+);
- Neo4j, grafo, chunks, media, VLM (HBIM-070/082/090/091);
- migrar a API/retrieval para os aliases `hbim_*` (lacuna HBIM-023);
- alterar `REWRITE_QUERY` ou o prompt de resposta final;
- alterar o schema canónico, os mappings HBIM-020 ou o lifecycle HBIM-021;
- alterar o dataset de retrieval da HBIM-005 (`corpus/queries/qrels/dataset.json`);
- qualquer modelo ML, embedding, GPU ou serviço remoto.

---

## 6. Ficheiros permitidos

**Criar:**

- `backend/retrieval/__init__.py`
- `backend/retrieval/router.py`
- `backend/tests/test_router.py`
- `backend/tests/test_routing_gold.py`
- `backend/eval/dataset/routing_gold.jsonl`
- esta spec

**Modificar (e apenas estes):**

- `backend/api/main.py` — substituir o bloco `CLASSIFY_INTENT` (§10.2)
- `backend/api/search.py` — **exclusivamente** para acrescentar campos opcionais
  ao `SearchPlan` conforme §10.4; nenhuma alteração a `build_opensearch_query`,
  `build_aggregation_query`, `AGG_FIELD_MAP` ou a qualquer função de pesquisa
- `backend/eval/metrics.py` — **exclusivamente aditivo**: nova função
  `routing_accuracy`; nenhuma função existente é alterada
- `backend/tests/conftest.py` — **exclusivamente** o ajuste mínimo de
  compatibilidade da fixture `fake_llm` descrito em §16.1, tornado necessário
  por §10.2: remover a chamada `CLASSIFY_INTENT` altera a **ordem e o consumo**
  das respostas do LLM falso. Nenhuma outra fixture, guarda de rede, isolamento
  de `.env` ou constante do módulo pode ser tocada
- `pyproject.toml` — `known-first-party` + override strict de mypy
- `.github/workflows/ci.yml` — lista explícita de mypy
- `docs/development/LOCAL_SETUP.md` — secção operacional
- `docs/implementation/IMPLEMENTATION_STATUS.md` — **só no fim**, após todos os
  gates

Qualquer outra alteração é violação de scope e bloqueia o commit.

---

## 7. Ficheiros protegidos (byte-idênticos)

- `backend/canonical/**` (schema, ids, serialization, `__init__`, os quatro mappings)
- `backend/ingestion/**` (incluindo `indexers/`, `index_lifecycle.py`,
  `migrate.py`, `canonical_ifc.py`, `index_to_opensearch.py`)
- `backend/api/prompts.py` — **incluindo `CLASSIFY_INTENT`, que permanece
  definido mas deixa de ser importado e usado** (deprecação é HBIM-041)
- `backend/eval/run_eval.py`, `backend/eval/dataset.py`
- `backend/eval/dataset/{corpus,queries,qrels}.jsonl`, `dataset.json`
- `backend/eval/baselines/current_system.json`
- `backend/shared/**`
- `frontend/**`
- `backend/tests/fixtures/**`
- `backend/tests/**` — **exceto** os dois ficheiros novos de §6 e o ajuste
  mínimo de `backend/tests/conftest.py` autorizado por §16.1. Nenhum teste
  existente pode ser alterado, adaptado, relaxado ou desativado para acomodar
  o comportamento novo; se um teste legado falhar, a causa é do código sob
  teste ou da fixture de §16.1, nunca da asserção
- `backend/requirements*.txt` — **nenhuma dependência nova**
- `.gitignore`

Guardar SHA-256 dos ficheiros protegidos antes de começar e reverificar no fim.

---

## 8. Contratos e interfaces públicas

`backend/retrieval/router.py` exporta **exatamente**:

```
Route, RouterContext, RoutingDecision, RouteSignals,
route, normalize_query, ROUTE_PRECEDENCE, TERMS_VERSION
```

Nada mais é público. O módulo é **puro**: sem I/O, sem rede, sem cliente, sem
settings, sem estado global mutável.

### 8.1 Import-safety (invariante dura)

Importar `retrieval.router` **não pode** importar nem criar:

| Proibido | Porquê |
|---|---|
| `shared.config`, `shared.opensearch` | settings / cliente / `.env` |
| `dotenv` | leitura de `.env` |
| `openai` | LLM |
| `opensearchpy` | cliente de pesquisa |
| `fastapi`, `api.*` | camada superior |
| `torch`, `sentence_transformers`, `transformers` | ML |
| `ifcopenshell`, `ingestion.*` | extração |
| `eval.*` | harness |
| qualquer socket | rede |

**Permitido:** apenas a stdlib (`enum`, `dataclasses`, `re`, `unicodedata`,
`typing`, `types`). **`pydantic` não é permitido** em `router.py` — os tipos são
`dataclass` frozen da stdlib, o que mantém o módulo trivialmente puro e sem
custo de import.

Verificado por subprocess em interpretador fresco (§19.8).

---

## 9. Tipos, estruturas e assinaturas

### 9.1 `Route`

```python
class Route(str, Enum):
    EXACT_LOOKUP    = "exact_lookup"
    AGGREGATION     = "aggregation"
    STRUCTURED      = "structured"
    GRAPH           = "graph"
    MULTIMODAL      = "multimodal"
    DOCUMENT_HYBRID = "document_hybrid"
    HYBRID_SEMANTIC = "hybrid_semantic"
    CHAT            = "chat"
```

Oito membros, valores exatamente estas strings (C2). `str` como base torna o
valor serializável sem conversão.

`ROUTE_PRECEDENCE: tuple[Route, ...]` é a ordem de avaliação de §11.1 e é
declarada explicitamente para poder ser asserida por teste.

### 9.2 `RouterContext`

```python
@dataclass(frozen=True)
class RouterContext:
    has_previous_results: bool = False   # há resultados anteriores citáveis
    has_image_input: bool = False        # o pedido traz uma imagem
```

Frozen: o router não pode mutar o contexto. Ambos os campos são **sinais
determinísticos do pedido**, nunca output de LLM (C6).

### 9.3 `RouteSignals`

```python
@dataclass(frozen=True)
class RouteSignals:
    contains_global_id: bool
    references_previous_result: bool
    asks_count_or_distinct: bool
    has_numeric_condition: bool
    has_ifc_class: bool
    has_storey: bool
    has_material: bool
    has_spatial_relation_terms: bool
    mentions_visual_terms: bool
    mentions_document_terms: bool
    is_conversational: bool

    def to_dict(self) -> dict[str, bool]: ...
```

Os nomes replicam literalmente os predicados de §6. Expor os sinais torna cada
decisão auditável e permite testar o predicado isoladamente da precedência.

### 9.4 `RoutingDecision`

```python
@dataclass(frozen=True)
class RoutingDecision:
    route: Route
    signals: RouteSignals
    matched_terms: tuple[str, ...]   # termos normalizados, ordenados, sem duplicados
    reason: str                      # identificador estável da regra que decidiu

    def to_dict(self) -> dict[str, object]: ...
```

`reason` pertence a um conjunto **fechado** de identificadores estáveis, um por
ramo de §11.1: `"global_id"`, `"previous_result"`, `"count_or_distinct"`,
`"structured_filters"`, `"spatial_relation"`, `"image_input"`, `"visual_terms"`,
`"document_terms"`, `"conversational"`, `"default_semantic"`. Um teste assere que
todo `reason` produzido pertence ao conjunto.

`matched_terms` contém os termos do vocabulário que dispararam — **nunca** a
query do utilizador nem fragmentos livres dela (§14).

### 9.5 Assinatura principal

```python
def route(query: str, context: RouterContext = RouterContext()) -> RoutingDecision: ...
def normalize_query(text: str) -> str: ...
```

`route` é **total**: para qualquer `str` (incluindo `""`) devolve uma
`RoutingDecision` válida. Nunca levanta para entradas `str`.

Validação de tipos, explícita e no início da função (não delegada a um
`AttributeError` acidental):

- `query` não-`str` ⇒ `TypeError` (nunca coagido com `str()`);
- `context` que não seja uma instância de `RouterContext` — incluindo `None`
  passado explicitamente — ⇒ `TypeError`.

O default é `RouterContext()` (instância imutável partilhada, segura por ser
`frozen`), nunca `None`.

---

## 10. Fluxo de execução

### 10.1 Normalização (`normalize_query`)

Determinística, sem dependências:

1. `unicodedata.normalize("NFKD", text)`;
2. remover os *combining marks* (`unicodedata.combining(ch) != 0`) — `"betão" →
   "betao"`, `"calcário" → "calcario"`, alinhando com o vocabulário sem acentos
   de §6;
3. `casefold()`;
4. substituir por espaço todo o carácter que não seja `[a-z0-9_]`;
5. colapsar espaços consecutivos e fazer `strip()`.

A correspondência de termos é feita **por fronteira de palavra** sobre o texto
normalizado, para que `"porta"` não case com `"portanto"` nem `"laje"` com
`"lajedo"`. Termos multi-palavra (`"lista de materiais"`, `"acima de"`,
`"parecido com"`) são procurados como sequência de palavras.

### 10.2 Substituição em `api/main.py`

O bloco `main.py:271–278` passa a:

```python
router_context = RouterContext(
    has_previous_results=bool(request.result_ids),
    has_image_input=False,                        # sem canal de imagem nesta issue
)
routing_decision = route(user_input, router_context)   # verbatim, nunca effective_query (C6)
strategy, route_degraded = execution_strategy(routing_decision, router_context)  # §10.3
needs_search   = strategy not in ("chat", "aggregation", "detail")
is_aggregation = strategy == "aggregation"
is_detail      = strategy == "detail"
```

`has_image_input` é sempre `False` nesta issue porque o endpoint não tem canal de
imagem; o ramo 6 de §11.1 existe na mesma e é exercitado **ao nível do router**
(o gold pode ter casos com `has_image_input=true`). Quando um canal de imagem
existir, basta passar o sinal real — o router não muda.

Os outros dois consumidores passam a ler `strategy`:

- `main.py:312` — `if strategy == "semantic":`
- `main.py:323` — `search_strategy=strategy`

`CLASSIFY_INTENT` e `ClassifyResult` deixam de ser importados em `main.py`.
`CLASSIFY_INTENT` **permanece definido** em `prompts.py` e `ClassifyResult`
**permanece definido** em `api/search.py` — ambos ficam sem consumidores, o que
é deliberado: removê-los é HBIM-041. Como deixam de ser importados, o Ruff
(regra `F401`) exige que os nomes saiam da lista de imports de `main.py`;
nenhuma outra alteração a esses ficheiros é permitida.

O ramo de paginação (`main.py:231–258`) **não é alterado**: reexecuta um plano já
guardado e não faz routing.

### 10.3 Capability map (obrigatória, explícita, testada)

Vive em `backend/api/main.py` — é política do endpoint, nunca do router.

```python
BASE_STRATEGY: Mapping[Route, str] = MappingProxyType({
    Route.CHAT:            "chat",
    Route.AGGREGATION:     "aggregation",
    Route.EXACT_LOOKUP:    "detail",
    Route.STRUCTURED:      "structured",
    Route.HYBRID_SEMANTIC: "semantic",
    # sem backend nesta fase (C3):
    Route.GRAPH:           "structured",
    Route.MULTIMODAL:      "semantic",
    Route.DOCUMENT_HYBRID: "semantic",
})
UNIMPLEMENTED_ROUTES: frozenset[Route] = frozenset(
    {Route.GRAPH, Route.MULTIMODAL, Route.DOCUMENT_HYBRID}
)

def execution_strategy(
    decision: RoutingDecision, context: RouterContext
) -> tuple[str, bool]:
    """Devolve (estratégia legacy, degraded). Total sobre Route."""
```

**Regra de degradação — exaustiva e normativa.** `execution_strategy` degrada em
**exatamente dois** casos, e `route_degraded` é `True` sse um deles se aplicar:

| # | Condição | Estratégia | `route_degraded` |
|---|---|---|---|
| D1 | `decision.route in UNIMPLEMENTED_ROUTES` | `BASE_STRATEGY[route]` | `True` |
| D2 | `decision.route is Route.EXACT_LOOKUP` **e** `not context.has_previous_results` | `"structured"` | `True` |
| — | qualquer outro caso | `BASE_STRATEGY[route]` | `False` |

D2 existe porque o caminho `detail` legacy (`main.py:344–371`) lê
`request.result_ids`; sem resultados anteriores devolveria vazio. Só é
alcançável via `reason="global_id"`, porque o ramo 2 de §11.1 já exige
`has_previous_results`. A `decision.route` **não** é reescrita: continua
`EXACT_LOOKUP` no plano e no log, e `reason` continua `global_id` — só a
*estratégia de execução* muda. Assim o gold afirma sempre a rota verdadeira.

Regras invariantes, cada uma com teste:

- `BASE_STRATEGY` é **total** sobre `Route`: `set(BASE_STRATEGY) == set(Route)`,
  pelo que acrescentar um membro de `Route` sem o mapear falha o CI;
- `set(BASE_STRATEGY.values()) ⊆ {chat, structured, semantic, aggregation, detail}`;
- `execution_strategy` devolve `route_degraded=True` **se e só se** D1 ou D2;
- para as cinco rotas implementadas com contexto normal, `route_degraded is False`.

### 10.4 `SearchPlan` (aditivo)

`backend/api/search.py` acrescenta ao `SearchPlan` **dois campos opcionais**, com
default, para não quebrar planos de paginação já serializados pelo frontend:

```python
route: str | None = None            # Route.value decidido
route_degraded: bool = False        # True sse a rota foi degradada (§10.3)
```

Nenhum outro campo é alterado. `build_opensearch_query` **não** lê estes campos
nesta issue.

### 10.5 Resposta e observabilidade

⚠ **Auditoria:** `/chat` tem **oito** pontos de retorno `ChatResponse`
(`main.py:342, 348, 364, 369, 432, 449, 472, 492`, numeração anterior à
implementação). Três devolvem `plan=None`
(chat e dois caminhos de `detail` sem resultado) e dois constroem um **dict
literal**, não um `SearchPlan` (`detail` em `:369–373`, `aggregation` em
`:432–440`). Uma regra do tipo "o plano passa a incluir a rota" seria, por isso,
**inaplicável em cinco dos oito caminhos**. O contrato é o seguinte:

**Canal obrigatório e universal — o log.** Exatamente **um** evento
`log_preprocess_json("router_decision", …)` é emitido **imediatamente após**
`execution_strategy(...)` e **antes** de qualquer ramificação, de forma que
cobre os oito caminhos de retorno sem exceção. Payload com **exatamente** estas
chaves:

```
route, strategy, degraded, reason, signals, matched_terms
```

`signals` é `RouteSignals.to_dict()`; `matched_terms` é a tupla de §9.4.
**A query do utilizador nunca entra neste payload** (§14).

**Canal secundário — o plano.** Onde já existe um plano, ganha os dois campos:

| Caminho | `main.py` | Ação |
|---|---|---|
| `SearchPlan` (structured/semantic) | `:322–332` | preencher `route` e `route_degraded` (§10.4) |
| `detail` (dict literal) | `:369–373` | acrescentar as chaves `"route"` e `"route_degraded"` |
| `aggregation` (dict literal) | `:432–440` | acrescentar as chaves `"route"` e `"route_degraded"` |
| `plan=None` | `:342, :348, :364` | **permanece `None`** — a rota é observável só pelo log |

O ramo de paginação não é alterado (§10.2); o plano reexecutado conserva o
`route`/`route_degraded` que tiver sido serializado.

---

## 11. Regras de routing (normativas)

### 11.1 Precedência

Avaliação **em ordem**, primeira que dispara ganha. Replica §6 e acrescenta os
dois ramos exigidos por C2/C3:

| # | Condição | Rota | `reason` |
|---|---|---|---|
| 1 | `contains_global_id` | `EXACT_LOOKUP` | `global_id` |
| 2 | `references_previous_result` **e** `context.has_previous_results` | `EXACT_LOOKUP` | `previous_result` |
| 3 | `asks_count_or_distinct` | `AGGREGATION` | `count_or_distinct` |
| 4 | `has_numeric_condition` ou `has_ifc_class` ou `has_storey` ou `has_material` | `STRUCTURED` | `structured_filters` |
| 5 | `has_spatial_relation_terms` | `GRAPH` | `spatial_relation` |
| 6 | `context.has_image_input` | `MULTIMODAL` | `image_input` |
| 7 | `mentions_visual_terms` | `MULTIMODAL` | `visual_terms` |
| 8 | `mentions_document_terms` | `DOCUMENT_HYBRID` | `document_terms` |
| 9 | `is_conversational` **e** `not references_previous_result` | `CHAT` | `conversational` |
| 10 | (fallback) | `HYBRID_SEMANTIC` | `default_semantic` |

A condição do ramo 9 é **exatamente** `is_conversational and not
references_previous_result` — não "nenhum sinal de domínio", que seria ambíguo.
Chegado ao ramo 9, os ramos 1 e 3–8 já não dispararam, pelo que todos os sinais
de domínio são necessariamente `False`; o único que pode continuar `True` é
`references_previous_result` (o ramo 2 exige também `has_previous_results`), e é
esse que a condição exclui explicitamente. Assim `"detalha esse"` numa sessão sem
resultados nunca vai para `CHAT`.

Notas normativas, cada uma com teste próprio:

- a ordem **1 antes de 3** garante que `"quantas propriedades tem o 3xY…?"` é
  `EXACT_LOOKUP` e não `AGGREGATION`;
- a ordem **3 antes de 4** garante que `"quantas portas há no piso 1?"` é
  `AGGREGATION` e não `STRUCTURED`, mesmo com classe e piso presentes;
- a ordem **4 antes de 5** segue §6 literalmente: filtros claros ganham a
  relações espaciais;
- o passo **9** exige ausência de **todos** os sinais de domínio, pelo que
  `"olá, quantas paredes há?"` é `AGGREGATION` e não `CHAT`.

`ROUTE_PRECEDENCE` expõe esta ordem e um teste compara-a com a ordem realmente
observada ao forçar cada sinal isoladamente (§19.2).

### 11.2 Vocabulários fechados

Os vocabulários vivem em `router.py` como constantes imutáveis (`frozenset` /
`tuple`), **normalizados na forma de §10.1** (sem acentos, minúsculas). Derivam
literalmente de `HBIM_RAG_DECISIONS.md` §599–623:

- **aggregation:** `quantos`, `quantas`, `contar`, `contagem`, `quantidade`,
  `lista de materiais`, `quais pisos`, `distribuicao`, `por piso`, `por material`,
  `quais materiais`, `que tipos`, `valores distintos`, `estatistica`
- **ifc_class:** `porta`, `portas`, `janela`, `janelas`, `parede`, `paredes`,
  `viga`, `vigas`, `pilar`, `pilares`, `escada`, `escadas`, `laje`, `lajes`
- **storey:** `piso`, `pisos`, `andar`, `andares`, `storey`
- **material:** `material`, `materiais`, `betao`, `madeira`, `pedra`,
  `calcario`, `tijolo`, `granito`, `argamassa`
- **numeric condition:** `maior que`, `menor que`, `acima de`, `abaixo de`,
  `mais de`, `menos de`, `entre`, `pelo menos`, `no maximo`, e o padrão
  numérico-com-unidade `(\d+([.,]\d+)?)\s*(m|metro|metros|cm|mm|m2|m3)\b`
- **spatial:** `acima`, `abaixo`, `adjacente`, `perto`, `dentro`, `contem`,
  `suporta`, `ligado a`, `pertence a`, `esta em`, `abre para`, `comunica com`
- **visual:** `parecido com`, `visualmente semelhante`, `fotografia`, `imagem`,
  `artefacto`, `acervo`, `museu`, `decoracao`, `ornamento`, `escultura`, `capitel`
- **document:** `documento`, `documentos`, `pdf`, `relatorio`, `fonte`, `pagina`,
  `menciona`, `historia`, `epoca`, `seculo`

Os termos acrescentados aos de §6 (plurais, `contagem`, `quantidade`,
`granito`, `argamassa`, `documentos`) são **flexões e sinónimos diretos** dos
termos ratificados, não categorias novas. `TERMS_VERSION: str = "1"` identifica
o vocabulário; alterá-lo obriga a regerar o gold.

**Forma do padrão numérico.** O padrão acima está escrito na forma legível; a
implementação pode usar grupos não-capturadores e ordenar as alternativas da
mais longa para a mais curta (`metros|metro|mm|cm|m2|m3|m`). As duas formas
**aceitam exatamente a mesma linguagem** — com a ordem curta-primeiro o motor
de regex chega ao mesmo resultado por *backtracking*, porque `\b` rejeita `m`
seguido de `e`/`m`/`2`/`3` — mas a ordem longa-primeiro não depende de
*backtracking* e satisfaz melhor §13.4. O contrato normativo é a linguagem
aceite, não a grafia do padrão.

⚠ **Ambiguidade resolvida:** `material`/`materiais` pertence a `material` **e**
aparece em `lista de materiais`/`quais materiais` (aggregation). A precedência
(3 antes de 4) resolve-a: `"quais materiais existem?"` → `AGGREGATION`;
`"paredes de pedra"` → `STRUCTURED`. Teste obrigatório para ambas.

### 11.3 Conjunto conversacional fechado

`is_conversational` é `True` sse a query **normalizada segundo §10.1** for igual
a um dos padrões, ou começar por um deles seguido de **fronteira de palavra**
(fim de string ou espaço). A normalização de §10.1 já converteu toda a pontuação
em espaço, pelo que "seguido de pontuação" e "seguido de espaço" são
indistinguíveis nesta fase: a fronteira de palavra é a única regra
implementável, e é a normativa. Consequências fixadas por teste: `"olá, ..."` e
`"ola ..."` são ambos conversacionais, enquanto `"olaf o construtor"` e
`"ajudante de pedreiro"` **não** disparam, porque o padrão não termina em
fronteira. Padrões: `ola`, `olá`→`ola`, `bom dia`, `boa tarde`,
`boa noite`, `obrigado`, `obrigada`, `adeus`, `ate logo`, `como estas`,
`como estao`, `tudo bem`, `quem es tu`, `o que podes fazer`, `ajuda`.

É um conjunto **fechado e literal**. Não há heurística de "parece uma saudação".
Perguntas definicionais fora deste conjunto caem em `HYBRID_SEMANTIC` (C7).

### 11.4 `contains_global_id`

IFC GlobalId é uma string base64-IFC de **22 caracteres** no alfabeto
`[0-9A-Za-z_$]`. O predicado corre sobre a query **original** (não normalizada,
porque o GlobalId é *case-sensitive* — `backend/canonical/schema.py:174` guarda
`global_id` exatamente) e exige fronteira de token:

```
(?<![0-9A-Za-z_$])[0-9A-Za-z_$]{22}(?![0-9A-Za-z_$])
```

**Contrato: sintaxe, não semântica.** O predicado é *puramente sintático* —
comprimento exato, alfabeto e fronteira de token. Não inspeciona a mistura de
maiúsculas, minúsculas, dígitos, `_` ou `$`, não consulta o índice e não usa
contexto. É por isso total, determinístico e testável sem rede.

⚠ **Fronteira de falso positivo — assumida, normativa e documentada.** Do
contrato acima resulta que **qualquer** token de 22 caracteres nesse alfabeto é
aceite, incluindo uma palavra portuguesa de exatamente 22 letras minúsculas
(p. ex. `responsabilizavelmente`). Isto é consequência necessária do contrato,
não um defeito de implementação, e é **deliberadamente preservado**:

- `IfcGloballyUniqueId` é uma string base64-IFC de 22 caracteres sobre
  `[0-9A-Za-z_$]`; **todas as combinações são sintaticamente válidas**, incluindo
  as compostas apenas por letras minúsculas;
- exigir pelo menos uma maiúscula, um dígito, `_` ou `$` **rejeitaria GlobalIds
  sintaticamente válidos**. Trocaria um falso positivo raro por **falsos
  negativos em identificadores reais**, que é o erro mais grave: uma query com
  um GlobalId legítimo deixaria de fazer *exact lookup* e cairia numa pesquisa
  lexical, devolvendo o elemento errado ou nenhum. A probabilidade de um id real
  ser todo minúsculo é `(26/64)^22 ≈ 4.3e-9`, mas a regra estreitada falharia
  **sempre** nesse caso, e não existe autoridade — ROADMAP §407–430/§829–833 ou
  `HBIM_RAG_DECISIONS.md` §6 — que exija a heurística mais estrita;
- `backend/canonical/schema.py:174` guarda `global_id` **exatamente**
  (*case-sensitive*, nunca normalizado): é um contrato já aceite da HBIM-010, e o
  router não pode assumir nenhuma forma canónica que estreite o alfabeto;
- o custo do falso positivo é **limitado e conhecido**: sem `has_previous_results`
  a degradação **D2** de §10.3 leva a estratégia a `"structured"`, isto é, uma
  pesquisa lexical — exatamente o que o fallback faria de qualquer modo.

**Testes obrigatórios.** Todos deterministas e sobre o contrato real:

1. os GlobalIds de 22 caracteres das fixtures canónicas
   (`backend/tests/fixtures/canonical/`) **são** aceites;
2. comprimento 21 e comprimento 23 **não** são aceites (o comprimento é exato);
3. a fronteira de token é respeitada: um id de 22 caracteres colado a mais
   caracteres do alfabeto **não** é aceite;
4. palavras portuguesas longas de comprimento ≠ 22 **não** são aceites;
5. um token de exatamente 22 letras minúsculas **é** aceite, e o teste declara no
   seu nome e docstring que esta é a fronteira sintática documentada aqui. Assim,
   estreitar o predicado passa a **falhar um teste** e a exigir uma decisão de
   especificação, em vez de acontecer por deriva silenciosa.

Não existe teste a exigir que uma palavra de 22 letras seja rejeitada: seria
incompatível com o regex normativo acima.

**Deliberadamente adiado.** Qualquer heurística **sensível ao contexto** —
confiança por mistura de caracteres, verificação do token contra o índice, ou
exigência de um marcador explícito (`id:`, `GlobalId`) — fica fora desta issue.
O ROADMAP §836 atribui a extração determinística de GlobalId à **HBIM-041**
(`retrieval/query_parser.py`) e §890 atribui o *entity linking* por
GlobalId/nome/localização à **HBIM-090**. Da HBIM-040 o ROADMAP §833 exige
apenas "GlobalId detetado" e `routing accuracy ≥ 95%` — ambos satisfeitos pelo
contrato sintático.

Quando `contains_global_id` decide `EXACT_LOOKUP` **sem** `has_previous_results`,
o endpoint aplica a degradação **D2** de §10.3: estratégia `"structured"`,
`route_degraded=True`, `decision.route` continua `EXACT_LOOKUP` e `reason`
continua `global_id`. A degradação é do endpoint; o router não a conhece.

### 11.5 `references_previous_result`

Termos fechados sobre o texto normalizado: `esse`, `essa`, `este`, `esta`,
`aquele`, `aquela`, `o primeiro`, `o segundo`, `o terceiro`, `o ultimo`,
`detalha`, `mais sobre`, `desse`, `dessa`, `deste`, `desta`.

O predicado **isolado** pode ser `True`, mas o ramo 2 da precedência só dispara
com `context.has_previous_results=True`. Isto impede que `"fala-me sobre esta
igreja"` numa sessão nova vá para `EXACT_LOOKUP`.

---

## 12. Precedência de erros e falhas parciais

O router **não tem falhas parciais**: é puro, total e sem I/O.

| Entrada | Comportamento |
|---|---|
| `""` ou só espaços | `HYBRID_SEMANTIC`, `reason="default_semantic"`, `matched_terms=()`; nunca levanta |
| String muito longa (≈ 9 500 chars, ver §19.9) | Processada normalmente; a normalização é O(n) e não há backtracking exponencial (§13.4) |
| Caracteres de controlo, emoji, RTL | Removidos pela normalização; sem exceção |
| `query` não-`str` | `TypeError` explícito |
| `context=None` | `TypeError` explícito (o default é `RouterContext()`, não `None`) |

No endpoint, um `TypeError` inesperado do router é tratado pelo handler de erros
existente (`api/errors.py`) e **não** expõe a query. O routing nunca devolve 500
por rota desconhecida, porque a capability map é total (§10.3).

---

## 13. Invariantes

1. **Totalidade:** `route()` devolve sempre um `Route` válido para qualquer `str`.
2. **Determinismo:** duas chamadas com o mesmo `(query, context)` devolvem
   `RoutingDecision` iguais (`==`), incluindo `matched_terms` e `reason`.
3. **Pureza:** nenhuma chamada de rede, ficheiro, relógio ou aleatoriedade.
   `router.py` não pode conter `random`, `time`, `datetime`, `open(`, `requests`,
   `socket` — verificado por inspeção de source no teste §19.8.
4. **Sem backtracking catastrófico:** todos os regexes são literais ou classes de
   caracteres com quantificador limitado; nenhum tem alternância aninhada com
   quantificadores sobrepostos. Um teste corre o router sobre uma string
   adversarial de ≈ 9 500 caracteres e exige conclusão (limite de tempo do teste
   documentado em §19.9).
5. **Imutabilidade:** `Route`, `ROUTE_PRECEDENCE` e os vocabulários são
   imutáveis; `RouterContext`, `RouteSignals` e `RoutingDecision` são `frozen`.
6. **Capability map total:** `set(BASE_STRATEGY) == set(Route)`.
7. **Vocabulário legacy fechado:** `set(BASE_STRATEGY.values()) ⊆
   {chat, structured, semantic, aggregation, detail}`.
8. **`reason` fechado:** todo `reason` pertence ao conjunto de §9.4.
9. **Degradação exaustiva:** `execution_strategy` devolve `degraded=True` se e
   só se D1 ou D2 de §10.3; nunca reescreve `decision.route` nem `reason`.

---

## 14. Segurança

- **Nenhuma variável de ambiente nova**; o router não lê configuração.
- **A query do utilizador nunca é escrita em logs, métricas, `matched_terms`,
  `reason` ou mensagens de erro.** Só entram: nomes de rota, nomes de sinais,
  termos do **vocabulário fechado** e identificadores de regra — todos
  constantes do próprio código, nunca dados do utilizador.
- Nenhum host, URL, porta, credencial ou body em código, testes, gold ou docs.
- `routing_gold.jsonl` contém **apenas** perguntas sintéticas escritas para esta
  issue; nenhum dado real de projeto, nenhum GlobalId de IFC real (usar os
  GlobalId sintéticos das fixtures canónicas ou strings sintéticas de 22 chars).
- Testes com domínios `.example.test` quando aplicável; sem rede, sem Docker,
  sem ML, sem GPU.

---

## 15. Determinismo e idempotência

- `route()` é uma função pura: idempotente por construção.
- `routing_gold.jsonl` é **byte-stable**. A forma canónica é **exatamente** esta
  expressão, replicada no teste (não é importada de `canonical/`, para que o
  gold não dependa do pacote canónico):

  ```python
  json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
  ```

  Uma linha por caso, terminada por `"\n"`; o ficheiro tem newline final e
  nenhuma linha em branco. Codificação UTF-8 sem BOM.
- Um teste regenera essa serialização a partir de `json.loads` de cada linha e
  compara o resultado byte a byte com o ficheiro committed, para que o gold não
  sofra deriva de formatação nem de escape de acentos.
- A ordem das linhas do gold é irrelevante para a métrica, mas o ficheiro é
  mantido ordenado por `id` para diffs legíveis; um teste assere essa ordenação.

---

## 16. Compatibilidade e comportamento legado

- O contrato HTTP de `/chat` mantém-se: os campos existentes de `ChatResponse` e
  `SearchPlan` não mudam de tipo nem de semântica; `route` e `route_degraded` são
  **acrescentos** opcionais.
- Planos de paginação serializados **antes** desta issue continuam a desserializar
  (os campos novos têm default) — teste obrigatório §19.15.
- `CLASSIFY_INTENT` permanece em `prompts.py` (protegido); apenas deixa de ser
  usado. A sua remoção é HBIM-041.
- A API continua a ler o índice legacy `bim_elements`; a lacuna HBIM-023
  mantém-se por resolver e **não** é tocada aqui.
- `REWRITE_QUERY`, `EXTRACT_*`, `FILTER_RESULTS_BATCH` e os prompts de resposta
  continuam a funcionar como hoje.

### 16.1 Fixture legada `fake_llm` — ajuste mínimo obrigatório

`backend/tests/conftest.py` define a fixture `fake_llm` com uma lista de
respostas cuja **primeira entrada** é `'{"search_strategy": "chat"}'`. Essa
entrada existe **exclusivamente** para satisfazer a chamada `CLASSIFY_INTENT`
que §10.2 elimina; a fixture entrega as respostas por ordem de chamada e fixa a
última. Removida a classificação, no caminho `chat` a primeira — e única —
chamada ao LLM passa a ser a resposta final ao utilizador, pelo que a fixture
devolveria o JSON de classificação como texto visível e as duas asserções
`response == "resposta final"` de `backend/tests/test_auth.py` falhariam.

Sem este ajuste, §6 (lista de ficheiros) e §22 (suite unitária verde) seriam
mutuamente incompatíveis. A alteração é uma **consequência mecânica de §10.2**,
não uma decisão de desenho, e não toca em código de produção.

O ajuste autorizado é **exatamente** este e nada mais:

- remover da lista a entrada que servia o `CLASSIFY_INTENT`, ficando
  `responses = ["resposta final"]`, com um comentário que nomeia a HBIM-040 e a
  razão;
- **nenhuma** alteração a `_fake_get_response`, `make_app`, `network_guard`,
  `forbid_real_env_files`, `isolated_opensearch_env`, `reset_api_state`,
  `client_constructor_recorder`, `_GuardedSocket`, `_LoopbackOnlySocket` ou às
  constantes do módulo;
- **nenhum** teste existente pode ser adaptado: `test_auth.py` continua a exigir
  `response == "resposta final"` e permanece byte-idêntico (§7).

**Teste obrigatório associado (§19.17).** A remoção não pode ficar demonstrada
apenas por convenção: um teste prova, por comportamento, que o endpoint já não
depende da resposta de routing removida.

---

## 17. Observabilidade

Obrigatório e verificável:

1. **Um** evento `router_decision` por pedido, emitido antes de qualquer
   ramificação, com **exatamente** as seis chaves de §10.5 — teste assere o
   conjunto de chaves e que é emitido também no caminho `chat` (onde
   `plan is None`).
2. Os três caminhos com plano ganham `route` e `route_degraded` conforme a
   tabela de §10.5; os três com `plan=None` continuam `None`.
3. Um teste assere que, num pedido que degrada (rota `GRAPH` por termo
   espacial), o evento tem `route == "graph"`, `strategy == "structured"` e
   `degraded is True`; e que num pedido `structured` normal
   `degraded is False`.
4. Um teste assere que a query do utilizador não aparece no payload do evento,
   usando o sentinela de §19.13.

Não são criadas métricas Prometheus novas nesta issue (evita tocar
`api/metrics.py`, que não está na lista de ficheiros permitidos).

---

## 18. `routing_gold.jsonl` — formato e conteúdo

### 18.1 Formato

Uma linha JSON por caso, com **exatamente** estas chaves:

```json
{"expected_route":"aggregation","has_image_input":false,"has_previous_results":false,"id":"agg-001","query":"quantas paredes existem no piso 1?"}
```

| Campo | Tipo | Regra |
|---|---|---|
| `id` | `str` | `^[a-z_]+-\d{3}$`, único |
| `query` | `str` | não vazio, sintético |
| `expected_route` | `str` | um dos oito valores de `Route` |
| `has_previous_results` | `bool` | contexto |
| `has_image_input` | `bool` | contexto |

### 18.2 Cobertura mínima

- **≥ 80 casos** no total;
- **≥ 8 casos por rota** para as oito rotas (mínimo 64), mais casos de fronteira;
- **≥ 10 casos de ambiguidade** que exercitam explicitamente a precedência
  (count+classe+piso; GlobalId+contagem; saudação+contagem; material como
  agregação vs material como filtro; follow-up com e sem `has_previous_results`);
- **≥ 5 casos com acentuação** (`betão`, `calcário`, `histórico`, `século`,
  `decoração`) provando a normalização;
- **≥ 3 casos de entrada degenerada** (string vazia, só pontuação, só números).

### 18.3 Métrica

`backend/eval/metrics.py` ganha **apenas**:

```python
def routing_accuracy(predicted: Sequence[str], expected: Sequence[str]) -> float:
    """Fração de rotas exatamente corretas. Erro se os comprimentos diferirem."""
```

Pura, offline, sem OpenSearch. Comprimentos diferentes ⇒ `ValueError`. Sequência
vazia ⇒ `ValueError` (evita que um gold vazio produza 100 %).

O gate é `routing_accuracy ≥ 0.95`, avaliado em `test_routing_gold.py`
**offline** (sem Docker, sem marker `integration`).

---

## 19. Testes unitários obrigatórios

`backend/tests/test_router.py`. Offline, sem rede, sem Docker, sem ML.

1. **Enum:** os oito membros e os seus valores exatos; `Route` é `str` Enum.
2. **Precedência:** `ROUTE_PRECEDENCE` corresponde à ordem observada — para cada
   ramo, construir uma query que dispare esse sinal **e** todos os de menor
   prioridade, e exigir a rota de maior prioridade.
3. **Um teste por ramo** (10 ramos) com `reason` esperado.
4. **Degradação `EXACT_LOOKUP`:** GlobalId sem `has_previous_results` ⇒ rota
   `EXACT_LOOKUP`, estratégia `"structured"`, `route_degraded is True`;
   follow-up com `has_previous_results=True` ⇒ estratégia `"detail"`.
5. **Casos de ambiguidade** de §11.2 e §11.1 (mínimo os quatro nomeados).
6. **Normalização:** `betão`≡`betao`, `CALCÁRIO`≡`calcario`; fronteira de
   palavra (`portanto` não dispara `porta`; `lajedo` não dispara `laje`);
   pontuação, emoji e caracteres de controlo não quebram.
7. **`route` nunca recebe output de LLM:** em `api/main.py`, teste com
   `get_response` monkeypatched para falhar se chamado antes do router; assere
   que o router foi chamado com `request.message` verbatim.
8. **Import-safety:** subprocess em interpretador fresco confirma que importar
   `retrieval.router` não traz nenhum módulo da tabela de §8.1; inspeção do
   source confirma ausência de `random`/`time`/`datetime`/`open(`/`socket`.
9. **Sem catástrofe de regex:** uma query adversarial de ≈ 9 500 caracteres
   (`"a" * 5000 + "acima de " * 500`) devolve uma `RoutingDecision` válida.
   **Sem asserção de tempo de parede** — um limite em segundos seria dependente
   da carga da máquina e violaria "testes não dependem do relógio" (`CLAUDE.md`).
   A garantia é estrutural: um teste inspeciona `re.Pattern.pattern` de todos os
   regexes do módulo e assere que nenhum contém um quantificador aplicado a um
   grupo que já contenha quantificador (padrão `\)[*+]` ou `\)\{`), que é a
   forma que produz backtracking exponencial. Se o router entrasse em
   backtracking catastrófico, o teste **não terminaria** e o CI falharia por
   timeout do runner — o modo de falha é visível sem asserção temporal.
10. **Totalidade e pureza:** `""`, só pontuação, só dígitos, string longa —
    todas devolvem `RoutingDecision` válida; duas chamadas iguais devolvem
    objetos iguais.
11. **Tipos:** `query` não-`str` ⇒ `TypeError`; dataclasses são `frozen`
    (atribuição ⇒ `FrozenInstanceError`).
12. **`reason` fechado** e `matched_terms` ordenado, sem duplicados e contido no
    vocabulário.
13. **Sem fuga da query:** para uma query contendo o token sentinela
    `ZZSECRETZZ`, nenhum campo de `RoutingDecision.to_dict()` o contém.
14. **Capability map:** `set(BASE_STRATEGY) == set(Route)`; valores ⊆
    vocabulário legacy; `UNIMPLEMENTED_ROUTES` são exatamente
    `{graph, multimodal, document_hybrid}`; `execution_strategy` devolve
    `route_degraded=True` **se e só se** D1 ou D2 (tabela exaustiva: as oito
    rotas × `has_previous_results ∈ {False, True}` = 16 combinações asseridas).
15. **Compatibilidade:** `SearchPlan(**plano_antigo_sem_route)` desserializa e
    `route is None`, `route_degraded is False`.
16. **`TERMS_VERSION` fixado:** um teste assere `TERMS_VERSION == "1"`, para que
    alterar o vocabulário seja um ato deliberado que obriga a rever o gold
    (§11.2).
17. **Independência da resposta de routing removida (§16.1):** com um LLM falso
    que devolve **uma única** resposta, o `/chat` produz uma resposta de `chat`
    bem-sucedida — prova comportamental de que a chamada de classificação
    desapareceu, e não apenas de que a fixture foi encurtada. O mesmo teste (ou
    o de §19.7) assere que o router é invocado **antes** da primeira chamada ao
    LLM. `backend/tests/test_auth.py` continua verde **sem modificação**.
18. **GlobalId — os cinco testes de §11.4**, incluindo o que documenta
    explicitamente a fronteira sintática de falso positivo.

---

## 20. Testes de gold obrigatórios

`backend/tests/test_routing_gold.py`. Offline.

1. **Schema:** cada linha tem exatamente as cinco chaves de §18.1, com os tipos
   corretos; `id` único e a casar `^[a-z_]+-\d{3}$`; `expected_route` ∈ `Route`.
2. **Cobertura:** os mínimos de §18.2 (total, por rota, ambiguidade, acentos,
   degenerados) são asseridos numericamente.
3. **Byte-stability:** reserializar cada linha na forma canónica reproduz o
   ficheiro byte a byte; ficheiro ordenado por `id`; newline final.
4. **Gate:** `routing_accuracy(previstas, esperadas) >= 0.95`.
5. **O gate consegue falhar:** um teste alimenta `routing_accuracy` com uma
   sequência propositadamente errada e exige valor `< 0.95` (impede que o gate
   seja tautológico).
6. **Sem dados sensíveis:** o gold não contém `/home/`, `/mnt/`, `.ifc`,
   `http://`, `https://`, `password`, nem GlobalId de IFC real.
7. **Isolamento da HBIM-005:** `eval.dataset.load_and_validate(dataset_dir)`
   continua a passar com `routing_gold.jsonl` presente no mesmo diretório, e
   `dataset.json` permanece byte-idêntico. *(Auditoria: `_validate_checksums`
   exige a chave-set exata `{corpus,queries,qrels}.jsonl` mas **não** varre o
   diretório — nenhum `glob`/`iterdir` existe em `eval/dataset.py` nem em
   `test_eval_dataset.py` — logo um ficheiro extra é ignorado. Este teste fixa
   essa propriedade.)*

---

## 21. Testes adversariais obrigatórios

Incluídos nos ficheiros acima, nomeados explicitamente:

1. **Saudação + pedido real** (`"olá, quantas paredes há?"`) ⇒ `AGGREGATION`,
   nunca `CHAT`.
2. **GlobalId + palavra de contagem** ⇒ `EXACT_LOOKUP`.
3. **Follow-up sem histórico** ⇒ nunca `EXACT_LOOKUP` por
   `references_previous_result`.
4. **Material como agregação vs filtro** (§11.2).
5. **Termo dentro de palavra maior** (`portanto`, `lajedo`, `contemplar` vs
   `contem`) ⇒ não dispara.
6. **Acentuação e maiúsculas** ⇒ mesma rota que a forma sem acentos.
7. **Query só com pontuação/emoji** ⇒ `HYBRID_SEMANTIC`, sem exceção.
8. **Query adversarial longa** ⇒ termina (§19.9).
9. **Sentinela de fuga de dados** (§19.13).
10. **Determinismo sob ordem aleatória:** a suite corre com
    `--randomly-seed=1`, `--randomly-seed=2` e `-p no:randomly` com resultado
    idêntico; nenhum teste do router usa `monkeypatch` sobre `retrieval.router`
    e nenhum faz `importlib.reload` desse módulo (evita o *hazard* de identidade
    de classe documentado em `test_index_mappings.py` e corrigido na HBIM-022).

---

## 22. Regression gates

Devem continuar verdes e byte-idênticos:

- suite unitária completa (inclui as suites HBIM-005/010/011/012/020/021/022);
- suite de integração existente;
- `backend/eval/baselines/current_system.json` **byte-idêntico** (sha256 com
  prefixo `7bf3c8d7200f0512`);
- `backend/eval/dataset/{corpus,queries,qrels}.jsonl` e `dataset.json`
  byte-idênticos;
- todos os ficheiros de §7 byte-idênticos, verificados por SHA-256 antes e depois;
- Ruff limpo sobre `backend`;
- mypy bloqueante verde, agora incluindo `retrieval.router`;
- **nenhum job CI novo**.

---

## 23. Critérios de aceitação

Cada critério é verificável por teste, comando ou inspeção objetiva.

1. `backend/retrieval/{__init__,router}.py` existem; `router.py` exporta
   exatamente a superfície de §8. *(offline 1, 12)*
2. `Route` tem os oito membros e valores de §9.1. *(offline 1)*
3. A precedência implementada é exatamente a de §11.1, incluindo os quatro
   pontos normativos. *(offline 2, 3, 5)*
4. `route()` é total, determinística e pura; nunca levanta para `str`.
   *(offline 9, 10, 11)*
5. Import de `retrieval.router` não traz nenhum módulo proibido de §8.1 e o
   source não usa relógio, aleatoriedade, ficheiros ou sockets. *(offline 8)*
6. `CLASSIFY_INTENT` **não é importado nem chamado** em `backend/api/main.py`:
   `grep -n "CLASSIFY_INTENT" backend/api/main.py` não devolve linhas.
   *(inspeção + offline 7)*
7. O router recebe `request.message` verbatim, nunca `effective_query`.
   *(offline 7)*
8. `BASE_STRATEGY` é total sobre `Route`, com valores no vocabulário legacy;
   `UNIMPLEMENTED_ROUTES == {graph, multimodal, document_hybrid}`; e
   `route_degraded` é `True` exatamente nos casos D1/D2 de §10.3, asserido nas
   16 combinações rota × contexto. *(offline 14)*
9. `SearchPlan` aceita planos antigos sem os campos novos. *(offline 15)*
10. Exatamente um evento `router_decision` por pedido, antes de qualquer
    ramificação, com as seis chaves de §10.5, presente também no caminho `chat`;
    os três caminhos com plano carregam `route`/`route_degraded` conforme a
    tabela de §10.5. *(§17.1–17.3)*
11. A query do utilizador não aparece em nenhum campo de `RoutingDecision`, log
    de routing ou mensagem de erro. *(offline 13)*
12. `routing_gold.jsonl` cumpre schema, cobertura mínima e byte-stability.
    *(gold 1, 2, 3)*
13. **`routing_accuracy ≥ 0.95`** no gold, offline. *(gold 4)*
14. O gate de accuracy consegue falhar. *(gold 5)*
15. `eval/metrics.py` só ganhou `routing_accuracy`; nenhuma função existente
    alterada. *(git diff)*
16. A validação do dataset HBIM-005 continua a passar com o gold presente e
    `dataset.json` está byte-idêntico. *(gold 7)*
17. `pyproject.toml` inclui `"retrieval"` em `known-first-party` e
    `retrieval.router` no override strict; `ci.yml` lista os ficheiros de
    `retrieval` no comando mypy. *(inspeção + mypy verde)*
18. Todos os ficheiros de §7 byte-idênticos. *(SHA-256)*
19. Suites unitária, integração e baseline verdes; Ruff e mypy limpos.
20. Nenhuma dependência nova; `requirements*.txt` byte-idênticos.
21. Determinismo sob três ordens de teste. *(adversarial 10)*
22. Nenhum ficheiro fora de §6 alterado. *(git status/diff)*
23. `backend/tests/conftest.py` sofreu **apenas** o ajuste de §16.1; nenhum
    outro teste existente foi alterado, adaptado ou desativado, e `test_auth.py`
    está byte-idêntico. *(git diff + SHA-256)*
24. `contains_global_id` implementa a sintaxe normativa de §11.4 e os cinco
    testes desse parágrafo passam, incluindo o que fixa a fronteira de falso
    positivo. Nenhum teste exige a rejeição de uma palavra de 22 letras.
    *(§11.4, offline)*
25. Nenhuma decisão fica pendente: a especificação não contém exigência que
    contradiga o seu próprio contrato normativo. *(inspeção)*

---

## 24. Comandos de validação

Todos em WSL, ambiente `hbim-rag`, sem Docker exceto onde indicado.

```bash
# Testes focados (offline)
conda run -n hbim-rag python -m pytest \
  backend/tests/test_router.py backend/tests/test_routing_gold.py \
  -q -o addopts=""

# Ordens múltiplas (determinismo)
conda run -n hbim-rag python -m pytest backend/tests/test_router.py \
  -q -o addopts="" --randomly-seed=1
conda run -n hbim-rag python -m pytest backend/tests/test_router.py \
  -q -o addopts="" --randomly-seed=2
conda run -n hbim-rag python -m pytest backend/tests/test_router.py \
  -q -o addopts="" -p no:randomly

# Suite unitária completa
conda run -n hbim-rag python -m pytest backend/tests -m "not integration" -q -o addopts=""

# Suite completa (exige Docker local, efémero, loopback-only)
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -p no:randomly

# Baseline HBIM-005 (Docker local efémero)
conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_eval_baseline.py -m integration -q -o addopts=""

# Qualidade
conda run -n hbim-rag python -m ruff check backend
# mypy: executar o comando explícito do .github/workflows/ci.yml já atualizado

# Prova de que o LLM saiu do routing
grep -n "CLASSIFY_INTENT" backend/api/main.py   # deve devolver zero linhas

# Proteções (antes e depois)
sha256sum backend/eval/baselines/current_system.json \
          backend/eval/dataset/dataset.json \
          backend/api/prompts.py \
          backend/tests/test_auth.py \
          backend/requirements.txt backend/requirements-dev.txt

# §16.1: o único ficheiro de teste legado tocado, e só na fixture fake_llm
git --no-pager diff --stat main -- backend/tests/conftest.py
git --no-pager diff main -- backend/tests/ ':!backend/tests/test_router.py' \
                            ':!backend/tests/test_routing_gold.py' \
                            ':!backend/tests/conftest.py'   # deve ser vazio

# Scope
git status --short --untracked-files=all
git --no-pager diff --name-status
```

---

## 25. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Vocabulário fechado não cobre fraseados reais | Fallback `HYBRID_SEMANTIC` (nunca erro); gold com ≥ 80 casos e gate 0.95 deixa margem para 4 falhas; `TERMS_VERSION` versiona o vocabulário |
| Gold escrito para confirmar a implementação (auto-cumprido) | O gold é escrito **antes** de afinar o router, a partir de §6 e do prompt legacy; os testes 21.1–21.9 são derivados da spec, não do código; gold 5 prova que o gate consegue falhar |
| Degradação de rotas mascara ausência de backend | `route_degraded` no plano e no log; teste §17.3; a rota verdadeira fica no gold, pelo que HBIM-070/082/090 herdam o router correto sem regerar o gold |
| Perguntas definicionais deixam de ir para `chat` | Desvio consciente e documentado (C7); direção segura; gold codifica o comportamento |
| Ficheiro extra em `eval/dataset/` quebrar a HBIM-005 | Auditado: sem `glob`/`iterdir`; checksums são allowlist explícita não tocada; teste gold 7 fixa a propriedade |
| `SearchPlan` novo quebrar paginação do frontend | Campos opcionais com default; teste offline 15 |
| Catástrofe de regex (ReDoS) numa query longa | Regexes literais/classes com quantificador limitado; teste adversarial 8 |
| Fuga da query para logs | Proibição explícita §14 + teste sentinela offline 13 |
| Router acoplar-se ao LLM por descuido | Import-safety em subprocess (offline 8) + teste 7 |
| **Mudança de comportamento em follow-ups:** o router lê `request.message` verbatim (C6), enquanto o legacy classificava a pergunta já reescrita pelo LLM. Um follow-up elíptico (`"e as de betão?"`) perde o contexto que o `REWRITE_QUERY` reinjetava e pode passar de `structured` a `hybrid_semantic` | Aceite e deliberado: é o preço do determinismo, e `hybrid_semantic` é o fallback seguro (pesquisa mais lata, nunca erro). O `effective_query` reescrito continua a alimentar a extração a jusante, pelo que os filtros não se perdem. O gold inclui casos de follow-up com e sem `has_previous_results` (§18.2) que fixam o comportamento novo. Reintroduzir contexto no routing sem LLM é trabalho da HBIM-041 |

---

## 26. Decisões deliberadamente adiadas

- Remover `CLASSIFY_INTENT` de `prompts.py` e os prompts de extração → **HBIM-041**.
- Aplicar filtros material/storey/name e corrigir a agregação → **HBIM-042**.
- Backends reais de `graph`, `multimodal`, `document_hybrid` → **HBIM-082 / 090 / 070**;
  quando existirem, basta alterar a capability map (o router e o gold não mudam).
- Query expansion opcional por AMALIA **depois** do router (§6 "onde AMALIA ainda
  entra") → HBIM-041+.
- Métricas Prometheus de distribuição de rotas → HBIM-060.
- Migração da API/retrieval para os aliases `hbim_*` → lacuna **HBIM-023**,
  ainda sem issue própria; permanece por resolver e **não** é tocada aqui.

---

## 27. Entregáveis

**Criar:** `backend/retrieval/__init__.py`, `backend/retrieval/router.py`,
`backend/tests/test_router.py`, `backend/tests/test_routing_gold.py`,
`backend/eval/dataset/routing_gold.jsonl`, esta spec.

**Modificar:** `backend/api/main.py`, `backend/api/search.py` (aditivo),
`backend/eval/metrics.py` (aditivo), `backend/tests/conftest.py` (só o ajuste
mínimo de §16.1), `pyproject.toml`, `.github/workflows/ci.yml`,
`docs/development/LOCAL_SETUP.md`,
`docs/implementation/IMPLEMENTATION_STATUS.md` (só no fim).

**Relatório final** no formato do `CLAUDE.md`, com secção `Self-review findings`
e cada critério de §23 avaliado como `PASS`/`FAIL`/`PARTIAL` com evidência
concreta (ficheiro, símbolo, teste, comando).
