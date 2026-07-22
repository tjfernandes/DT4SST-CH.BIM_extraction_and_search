# HBIM-042 — Lexical Filters and Classification Aggregation

> **Tipo:** especificação executável de issue.
> **Estado:** aprovada para implementação.
> **Branch obrigatória:** `feat/hbim-042-lexical-filters-classification-aggregation`.
> **Depende de:** HBIM-041 (parser determinístico — merged em `main`, `c8eafb8`),
> HBIM-040 (router), HBIM-005 (harness + baseline congelada), HBIM-004 (CI).
> **Bloqueia:** HBIM-050.

---

## 1. Estado auditado

Todos os factos abaixo foram verificados no repositório em `c8eafb8`.

### 1.1 Alvo de retrieval ativo

A API lê **exclusivamente** o índice legacy `bim_elements`
(`shared/config.py:45` — `OPENSEARCH_INDEX = os.getenv("OPENSEARCH_INDEX",
"bim_elements")`), consumido por `execute_search`, `execute_aggregation` e
`fetch_by_id` em `api/search.py`. Os aliases canónicos `hbim_*` (HBIM-021/022)
existem e estão populáveis mas **nenhum código da API os consome** — é o gap
HBIM-023, documentado em `IMPLEMENTATION_STATUS.md`, que esta issue **não**
fecha nem alarga.

### 1.2 Mapping ativo (legacy `bim_elements`)

Definido em `ingestion/index_to_opensearch.py:101–176` (`dynamic: strict`,
normalizer `lc` = lowercase):

| Campo | Tipo | Nota |
|---|---|---|
| `material` | `keyword`, normalizer `lc` | array de strings; o normalizer aplica-se também ao valor da query em `term`/`terms` |
| `spatial_hierarchy.storey_name` | `keyword`, normalizer `lc` | label livre do IFC (`"floor 1"`, `"Piso 1"`, …) |
| `name` | `text` + subcampo `name.keyword` (`keyword`, `lc`) | igualdade exata case-insensitive via `name.keyword` |
| `ifc_class` | `keyword` (sem normalizer) | já filtrado hoje, com `IFC_CLASS_VARIANTS` |
| `project_id` | `keyword`, `lc` | já filtrado hoje |
| `metrics.*` | `double` | já filtrado hoje (conditions) |
| `classifications` | **`nested`** com `source`/`code` `keyword` `lc` e `name` **`text` sem subcampo keyword** | ver defeito §1.3 |

### 1.3 Defeitos ativos

1. **Filtros não aplicados.** `build_opensearch_query` (`api/search.py:305`)
   ignora `material`, `storey` e `name` do `SearchPlan` — o GAP §1.5 do
   ROADMAP. O parser HBIM-041 já produz estes valores; eles morrem no plano.
2. **Agregação de classificação inválida.** `AGG_FIELD_MAP["classification"] =
   "classifications.name"` gera uma `terms` **plana** sobre um campo **`text`
   dentro de `nested`**: num OpenSearch real isto ou falha
   (`illegal_argument_exception`, fielddata desativado em `text`) ou, com
   caminho keyword mas sem contexto `nested`, devolve **zero buckets** — os
   documentos nested são invisíveis a agregações planas.
3. `build_aggregation_query` também ignora `material`/`storey`/`name` do plano.

### 1.4 `classification_codes` não existe

O campo `classification_codes: keyword[]` aparece **apenas** no esboço futuro
de `HBIM_RAG_DECISIONS.md` (elements v2). Não existe no mapping ativo
(§1.2), não existe em `canonical/mappings/elements_v1.json` (verificado:
campos `description, element_id, global_id, ifc_class, location, materials,
metrics, name, object_type, predefined_type, project_id, schema_version,
semantic_label, source`) e as classificações canónicas vivem no índice
separado `classification_facts_v1`. A instrução literal do ROADMAP §843
("agregação usa `classification_codes` keyword") é **inaplicável a qualquer
mapping committed**.

### 1.5 HBIM-005 e o snapshot de compatibilidade

`eval/run_eval.py` usa os **builders de produção** (`build_opensearch_query`
:486/:504, `build_aggregation_query`+`execute_aggregation` :527–528) sobre o
mapping legacy (importa `ingestion.index_to_opensearch.create_index`). O
HBIM-005 congelou **dois** snapshots de compatibilidade do comportamento
defeituoso, com destinos diferentes nesta issue:

1. **`q-rs-material-ignored`** — plano `{ifc_class: IfcBeam, material:
   ["steel"]}` com o material ignorado; snapshot `{tie_groups: [[beam-a-16,
   beam-a-21, sem-a-03, beam-b-12]], total: 4}`. **Facto verificado:** os
   quatro `IfcBeam*` do corpus têm todos `material=["steel"]` e os filtros
   novos entram em contexto `filter` (sem scoring). Aplicar o filtro devolve o
   mesmo conjunto, mesmos `tie_groups`, mesmo `total` — **este snapshot fica
   invariante sem intervenção**.
2. **`q-rs-classification-agg`** — `{agg_field: "classification"}`; a
   agregação plana sobre `classifications.name` (text em nested) rebentava
   com `RequestError`, e o snapshot congelado é literalmente
   `{"error": "RequestError"}` (o runner grava a falha verbatim para gates
   `compatibility`). Corrigir esta agregação é o **propósito da milestone**,
   logo este snapshot **muda por construção**: o resultado correto,
   derivado **à mão do corpus** (28 documentos, todos com exatamente o código
   `ss_25`), é `{"agg_total": 28, "buckets": {"ss_25": 28}}` — contagem de
   elementos (§M3), igual à de factos neste corpus porque nenhum documento
   repete o código.

As agregações de correção (`q-agg-material`, `q-agg-storey`) não transportam
filtros lexicais e não agregam classificação; `correctness_metrics` não é
tocado por nenhum ramo novo.

### 1.6 Infraestrutura de integração

`tests/integration/conftest.py` fornece `opensearch_service` (fixture de
sessão, Testcontainers `opensearchproject/opensearch:2.19.1`, efémero,
loopback-only, guard de rede). `run_eval` demonstra o padrão para criar o
índice com o mapping de produção: `create_index(client)` de
`ingestion.index_to_opensearch` (import-safe; cliente só em runtime).

---

## 2. Precedência de fontes

1. Intenção de HBIM-042 no ROADMAP §840–845 + `HBIM_RAG_DECISIONS.md`.
2. Contratos committed aceites: HBIM-005 (harness/baseline), HBIM-020
   (mappings canónicos), HBIM-021 (aliases), HBIM-022 (indexers), HBIM-040
   (router), HBIM-041 (parser — fonte normativa dos valores extraídos).
3. Comportamento público atual da API e compatibilidade.
4. Correção, validade de mapping, segurança, determinismo, import-safety,
   testabilidade.
5. Scope mínimo de HBIM-042.
6. Fronteiras futuras, sobretudo HBIM-050.

## 3. Conflitos e decisões fixadas

| # | Conflito | Decisão | Autoridade |
|---|---|---|---|
| M1 | API lê legacy `bim_elements`; canónicos `hbim_*` sem consumidor | Operar **no contrato ativo legacy**. Nenhuma migração de alias (gap HBIM-023 continua aberto e documentado). Caminhos de campo exclusivamente do mapping §1.2 — nunca misturar mappings. | 3, 5 |
| M2 | ROADMAP manda agregar `classification_codes` (keyword), campo inexistente | Preservar o resultado pretendido (buckets corretos) sobre o contrato ativo: **agregação `nested` em `classifications` com `terms` sobre `classifications.code` (keyword `lc`) + `reverse_nested`**. Nunca fabricar `classification_codes`, nunca alterar mappings canónicos. | instrução do próprio pedido; 1→2 |
| M3 | Contagem por bucket: factos nested vs elementos | **Contagem de elementos** (`reverse_nested.doc_count`): responde a "quantos elementos têm cada classificação". O `doc_count` nested (factos) não é exposto. `total` da resposta continua a ser `hits.total` (elementos que passam o filtro). | 4 |
| M4 | Parser canoniza storey como `"1"`/`"0"`/`"L0"`; o índice guarda labels livres (`"floor 1"`, `"Piso 1"`) | **Expansão determinística fechada** `storey_term_values(canonical)` → `terms` sobre `spatial_hierarchy.storey_name` (§15). Sem wildcard, sem query_string, sem re-parse. | 4, 5 |
| M5 | Materiais canónicos PT (`pedra`) vs valores indexados livres | `terms` sobre `material` com os valores canónicos do parser **verbatim** (o normalizer `lc` cobre caso; acentos e compostos são fronteira v1 documentada). **OR dentro da dimensão, AND entre dimensões.** | 4, 5 |
| M6 | Semântica de `name` | `term` sobre `name.keyword` (igualdade exata case-insensitive via `lc`). O produtor determinístico é o parser HBIM-041 (spans citados/identificadores) — nenhum parsing novo em `api/search.py`. | 2, 4 |
| M7 | Aplicar filtros também às agregações? | **Sim** — `build_aggregation_query` passa a aplicar material/storey/name do plano (já os recebe). "Quantas paredes de pedra existem?" passa a contar só paredes de pedra. `count` global sem filtros mantém-se intacto. | 1, 3 |
| M8 | Snapshot `q-rs-material-ignored` congela o defeito de filtro | **Invariante sem intervenção** (§1.5.1): conjunto e scoring não mudam. Nada em `queries.jsonl`/`dataset.json`/corpus/qrels é tocado. | 2 |
| M10 | Snapshot `q-rs-classification-agg` congela **a falha** (`{"error": "RequestError"}`) que esta issue corrige | O mecanismo de snapshots do HBIM-005 é "gated separately, **not ground truth**" — existe para tornar mudanças de comportamento **deliberadas e visíveis**, não para as proibir. Autoriza-se a atualização **cirúrgica de exatamente esta chave** em `current_system.json` para o valor declarado independentemente em §1.5.2 (`{"agg_total": 28, "buckets": {"ss_25": 28}}`, derivado à mão do corpus), reserializando com o próprio formato do harness (`json.dumps(..., indent=2, sort_keys=True)+"\n"`, `save_baseline`). **Prova estrutural obrigatória:** um diff programático entre a baseline anterior e a nova mostra exatamente um caminho alterado (`compatibility_metrics.snapshots.q-rs-classification-agg`) — `correctness_metrics`, o outro snapshot, `config` e `dataset` byte-idênticos. O gate `test_eval_baseline` volta a ficar verde contra a baseline atualizada, provando que o sistema reproduz o valor declarado. Regenerar a baseline por atacado, tocar em `correctness_metrics` ou alterar o valor esperado depois de ver uma divergência continuam proibidos. | 1, 2 (§M2 por analogia) |
| M9 | ROADMAP M4 menciona `lexical.py` a "aplicar de facto" | `retrieval/lexical.py` é **puro** (stdlib): constrói cláusulas/agregações como dicts; quem as anexa é `api/search.py`. Nenhum cliente, nenhum BM25 (HBIM-050). | 4, 6 |

## 4. Objetivos

1. `backend/retrieval/lexical.py`: camada lexical determinística e pura —
   cláusulas de filtro material/storey/name, expansão de storey, corpo da
   agregação de classificação e parser da resposta.
2. `build_opensearch_query` aplica material/storey/name em **todos** os
   caminhos (structured e prefiltro kNN semântico) e a paginação preserva-os.
3. `build_aggregation_query` aplica os mesmos filtros; a agregação
   `classification` passa a `nested`+`reverse_nested` válida no mapping ativo.
4. `execute_aggregation` interpreta a resposta nested corretamente (contagem
   de elementos, ordenação determinística, resposta malformada → erro).
5. Prova em OpenSearch real efémero: `"paredes de pedra no piso 1"` devolve
   exatamente o conjunto esperado declarado independentemente; buckets de
   classificação exatos; a forma histórica errada falha.

## 5. Não objetivos (fronteira HBIM-050 e outros)

- BM25/candidate generation, dense retrieval, RRF, hybrid ranking, reranking,
  EvidencePack, geração de respostas grounded, serviços de embeddings/modelos.
- Migração da API para os aliases `hbim_*` (gap HBIM-023).
- Alterar mappings (legacy ou canónicos), indexers, lifecycle.
- Alterar router (HBIM-040) ou parser (HBIM-041).
- Alterar `api/main.py` (os builders mantêm assinaturas; nada muda na
  orquestração), `api/prompts.py`, o dataset/baseline HBIM-005.
- Novas dependências; novos jobs CI.

## 6. Ficheiros permitidos

**Criar:**

- `backend/retrieval/lexical.py`
- `backend/tests/test_lexical.py`
- `backend/tests/integration/test_lexical_filters_apply.py`
- esta spec

**Modificar (lista fechada):**

- `backend/api/search.py` — **exclusivamente**: anexar as cláusulas lexicais
  em `build_opensearch_query` e `build_aggregation_query`; ramo
  `classification` da agregação; parsing nested em `execute_aggregation`;
  atualização do valor `AGG_FIELD_MAP["classification"]` para
  `"classifications.code"` (documental — o ramo nested tem precedência).
  Nenhuma outra função muda.
- `backend/retrieval/__init__.py` — **exclusivamente o docstring** (a frase
  "lexical … deliberately absent" fica obsoleta). `__all__` e os re-exports
  **não mudam**: o teste protegido
  `test_query_parser.py::test_public_surface_is_exact` pina a superfície do
  pacote a router+parser, e `api.search` consome `retrieval.lexical`
  diretamente — re-exportar quebraria um teste protegido de §7.
- `pyproject.toml` — `retrieval.lexical` no override strict do mypy.
- `.github/workflows/ci.yml` — `backend/retrieval/lexical.py` na lista mypy.
- `docs/development/LOCAL_SETUP.md` — secção operacional HBIM-042.
- `docs/implementation/IMPLEMENTATION_STATUS.md` — só no fim, números reais.
- `backend/eval/baselines/current_system.json` — **exclusivamente** a chave
  `compatibility_metrics.snapshots["q-rs-classification-agg"]`, nos termos de
  §3 M10 (prova estrutural de um-só-caminho obrigatória).

Qualquer outra alteração é violação de scope e bloqueia o commit.

## 7. Ficheiros protegidos (byte-idênticos)

`backend/api/main.py`, `backend/api/prompts.py`,
`backend/retrieval/router.py`, `backend/retrieval/query_parser.py`,
`backend/eval/**` **exceto** a chave única de `current_system.json` autorizada
em §6/M10 (`queries.jsonl`, `dataset.json`, corpus, qrels, `parser_gold.jsonl`,
`legacy_extraction.json`, `metrics.py`, `run_eval.py`, `dataset.py` e todo o
resto de `current_system.json` byte-idênticos), `backend/canonical/**`,
`backend/ingestion/**`
(incluindo `index_to_opensearch.py`), `backend/shared/**`,
`backend/tests/conftest.py`, `backend/tests/integration/conftest.py`, todos os
testes existentes, `backend/tests/fixtures/**`, `frontend/**`,
`backend/requirements*.txt`, `.gitignore`. SHA-256 antes e depois.

## 8. Interfaces públicas de `retrieval/lexical.py`

Stdlib-only (§24). Exporta exatamente:

```
LEXICAL_TERMS_VERSION: str = "1"
MATERIAL_FIELD = "material"
STOREY_FIELD = "spatial_hierarchy.storey_name"
NAME_FIELD = "name.keyword"
CLASSIFICATION_NESTED_PATH = "classifications"
CLASSIFICATION_CODE_FIELD = "classifications.code"
CLASSIFICATION_AGG_SIZE = 200
storey_term_values(canonical: str) -> tuple[str, ...]
material_clause(materials: Sequence[str]) -> dict | None
storey_clause(canonical: str | None) -> dict | None
name_clause(name: str | None) -> dict | None
lexical_filter_clauses(materials, storey, name) -> list[dict]
classification_aggregation(size: int = CLASSIFICATION_AGG_SIZE) -> dict
parse_classification_buckets(aggregations: dict) -> list[dict]
```

Funções totais e puras: input vazio/`None` → `None`/`[]`; `TypeError` para
tipos errados (sem ecoar valores); **nenhuma re-normalização** dos valores do
parser (passam verbatim — a divergência de normalização é estruturalmente
impossível porque não há segunda normalização); nenhum estado mutável.

## 9. Integração com ParsedQuery/SearchPlan/Condition

Nenhum modelo novo, nenhum duplicado: `lexical` recebe **primitivos**
(`Sequence[str]`, `str | None`) extraídos pelo parser HBIM-041 e já presentes
no `SearchPlan` (`material: List[str] | None`, `storey: str | None`,
`name: str | None` — serializados desde sempre, pelo que planos de paginação
antigos e novos funcionam sem migração). `api/search.py` chama
`lexical_filter_clauses(plan.material or [], plan.storey, plan.name)` e anexa
o resultado a `bool_filter`. `Condition` e o resto do plano não mudam.

## 10. Registo de caminhos de campo

Única fonte no código: as constantes de §8 (valores de §1.2). Nenhum caminho
de mapping canónico entra nesta issue. Teste unitário assere que os caminhos
usados nos dicts construídos são exatamente estes e que
`classifications.name` **não** aparece em nenhuma query/agregação construída.

## 11. Ordem e composição das cláusulas

`build_opensearch_query` mantém a ordem atual (`ifc_class`, `project_id`,
conditions) e anexa **no fim, por esta ordem fixa**: material, storey, name —
todos em `bool.filter` (contexto de filtro: sem scoring, AND entre cláusulas).
O prefiltro kNN semântico herda automaticamente (as cláusulas entram em
`bool_filter` antes do ramo kNN existente). `build_aggregation_query` anexa a
mesma lista após os filtros atuais (`ifc_class`, `project_name`). Query final
byte-determinística para o mesmo plano (teste com dict exato).

## 12–16. Semântica por dimensão

### 12. Material (§M5)

`material_clause(["pedra"])` → `{"terms": {"material": ["pedra"]}}`.
Multi-material = OR dentro da dimensão (`terms` com a lista, dedup preservando
a primeira ocorrência, ordem de input preservada — o parser já entrega
ordenado). Lista vazia/`None` → `None` (cláusula ausente; nunca `terms` vazio,
que devolveria zero resultados). Itens não-`str` → `TypeError`. AND com as
outras dimensões via `bool.filter`.

### 13. (reservado)

### 14. Name (§M6)

`name_clause("Muralha_Sul")` → `{"term": {"name.keyword": {"value":
"Muralha_Sul"}}}`. Igualdade exata do nome completo, case-insensitive pelo
normalizer `lc` do índice. O valor é um literal JSON — caracteres especiais
(`*`, `?`, `"`, `\`, `/`) não têm interpretação sintática em `term` (teste
adversarial). String vazia/whitespace → `None`. Nomes parciais/frases soltas
são fronteira v1 (o produtor é o parser: spans citados e identificadores).

### 15. Storey (§M4)

`storey_term_values` — expansão fechada, minúscula, ordem fixa, dedup:

| Canónico (parser) | Valores emitidos |
|---|---|
| inteiro `N` (regex `^-?\d+$`) | `N`, `piso N`, `andar N`, `nivel N`, `nível N`, `level N`, `storey N`, `floor N`; e, se `N` for um único dígito sem sinal, também `0N` |
| `"0"` adicionalmente | `r/c`, `res-do-chao`, `rés-do-chão`, `terreo`, `térreo` |
| `"-1"` adicionalmente | `cave` |
| token letra+dígitos (`^[A-Za-z]\d+$`, ex. `L0`) | forma minúscula `l0`, `piso l0`, `andar l0`, `nivel l0`, `nível l0`, `level l0`, `storey l0`, `floor l0` |
| qualquer outro valor | o próprio valor em minúsculas, sozinho |

`storey_clause("1")` → `{"terms": {"spatial_hierarchy.storey_name": [<valores>]}}`.
Acentos: o normalizer `lc` **não remove acentos**, por isso a expansão inclui
as variantes acentuada e não acentuada (`nível`/`nivel`, `térreo`/`terreo`,
`rés-do-chão`/`res-do-chao`). Labels de piso fora da expansão são fronteira v1
documentada. Sem wildcard/regexp/query_string em circunstância alguma.

### 16. Compatibilidade dos filtros existentes

`ifc_class` (+`IFC_CLASS_VARIANTS`), `project_id`, conditions numéricas,
exact lookup (`fetch_by_id`) e agregação `count` global mantêm o comportamento
byte-exato atual quando o plano não traz valores lexicais — teste golden: para
um plano sem material/storey/name, a query construída é **idêntica** à de
antes desta issue (dict literal esperado escrito à mão no teste).

## 17. (reservado)

## 18. Forma da query estruturada (normativa)

Para o plano de aceitação `{search_strategy: "structured", ifc_class:
"IfcWall", material: ["pedra"], storey: "1"}`:

```json
{"size": 10, "from": 0, "track_total_hits": true,
 "query": {"bool": {"must": [{"match_all": {}}], "filter": [
   {"terms": {"ifc_class": ["IfcWall", "IfcWallStandardCase"]}},
   {"terms": {"material": ["pedra"]}},
   {"terms": {"spatial_hierarchy.storey_name": ["1", "piso 1", "andar 1",
     "nivel 1", "nível 1", "level 1", "storey 1", "floor 1", "01"]}}
 ]}}}
```

(teste unitário assere este dict exato; a variante kNN assere as mesmas
cláusulas dentro de `knn.semantic_embedding.filter.bool.filter`).

## 19. Prefiltro semântico

Sem alterações estruturais: as cláusulas lexicais entram em `bool_filter`
antes do ramo kNN existente, logo o prefiltro passa a contê-las. Teste assere
que, com `search_strategy="semantic"` e embedding fornecido, o filtro kNN
contém material+storey+name além de `ifc_class`.

## 20. Paginação

O ramo de paginação reexecuta o plano armazenado através do mesmo
`build_opensearch_query` — os filtros lexicais aplicam-se automaticamente ao
replay. Planos serializados **antes** desta issue: os campos já existiam no
`SearchPlan` (nenhuma migração); um plano antigo com `material` preenchido
passa agora a filtrar — é exatamente a correção pretendida. Teste unitário
com plano armazenado prova a presença das cláusulas no replay.

## 21–23. Agregação de classificação

### 21. Alvo e forma (normativa)

`build_aggregation_query(agg_field="classification", …)` emite:

```json
{"aggs": {"agg_result": {"nested": {"path": "classifications"},
  "aggs": {"codes": {"terms": {"field": "classifications.code", "size": 200},
    "aggs": {"elements": {"reverse_nested": {}}}}}}}}
```

com os filtros (`ifc_class`, `project_name`, lexicais) no `query.bool.filter`
exterior, como hoje. `AGG_FIELD_MAP["classification"]` passa a
`"classifications.code"` (documental); o ramo nested decide **antes** do
lookup plano, pelo que a agregação plana nunca é usada para classificação.
Agregações planas existentes (`material`, `storey`, `ifc_class`, `project*`)
ficam byte-idênticas.

### 22. Extração da resposta

`parse_classification_buckets(aggregations)` lê
`aggregations["agg_result"]["codes"]["buckets"]` e devolve
`[{"key": bucket["key"], "count": bucket["elements"]["doc_count"]}, …]` —
**contagem de elementos** (M3). Ordenação determinística no cliente:
`(-count, key)`. Chave em falta em qualquer nível → `ValueError`
("malformed classification aggregation response"), nunca aceitação
silenciosa. `execute_aggregation` deteta o ramo nested pela presença de
`codes` dentro de `agg_result` e delega; caso contrário mantém o parsing
plano atual, byte-idêntico. `total` continua `hits.total`.

### 23. Semânticas de contagem e vazios

- bucket.count = nº de **elementos** com ≥1 facto com aquele código (um
  elemento com o mesmo código repetido conta **uma** vez — teste dedicado com
  facto duplicado);
- elementos sem classificações não gerem bucket nenhum (nunca um bucket
  fantasma);
- filtro que exclui todos os elementos classificados → `buckets == []` e o
  formatter atual responde "Nenhum resultado encontrado." (inalterado);
- truncagem: `size=200` como a agregação plana legacy; fronteira documentada
  (nenhum cenário de teste excede 200 códigos).

## 24. Segurança, import-safety, determinismo

- `retrieval/lexical.py` importa **apenas stdlib** (`re`, `typing`,
  `dataclasses` se necessário); proibido: clientes, `api.*`, `shared.*`,
  pydantic, relógio, aleatoriedade, I/O, sockets. Subprocess fresco + AST como
  HBIM-041 §8.1.
- Nenhum input do utilizador entra em `script`, `query_string`, `wildcard` ou
  `regexp` — os únicos tipos de query emitidos são `term` e `terms` (AST/scan
  do source + teste sobre os dicts).
- Mesmo input → mesmo output; sem iteração de `set`/`dict` no output; byte-
  igual sob `PYTHONHASHSEED ∈ {0,1,7,4242}` (subprocess).
- Exceções nunca ecoam valores do utilizador.

## 25. (reservado)

## 26. Observabilidade

Nenhum evento novo (scope mínimo): os eventos existentes
(`opensearch_query`, `aggregation_opensearch_query`, `aggregation_result`)
já registam a query construída e os buckets — passam a mostrar as cláusulas
novas sem alteração de código. Nada de conteúdo sensível novo.

## 27. Dataset sintético de integração (normativo)

Índice efémero **dedicado `hbim_lexical_test_v1`** (o container Testcontainers
é partilhado pela sessão de integração — nunca usar o nome `bim_elements` nem
tocar índices de outros testes), criado com o **mapping de produção** pelo
padrão estabelecido em `run_eval` (§1.6): definir `OPENSEARCH_INDEX=
hbim_lexical_test_v1`, `OPENSEARCH_HOST`/`OPENSEARCH_PORT` do container e
`EMBEDDING_DIM=40` no ambiente, **reimportar frescos** `shared.config`,
`shared.opensearch`, `api.search` e `ingestion.index_to_opensearch`
(constantes e `lru_cache` ligados no import) e chamar
`create_index(client)` de produção. O teste **recusa** correr se o índice já
existir e o teardown apaga **apenas** `hbim_lexical_test_v1` (guard explícito
no nome). Embeddings: vetores literais de dimensão 40 (sem ML). Elementos
(ids sintéticos, projeto `synthetic-lex`):

| id | ifc_class | material | storey_name | name | classifications (source/code/name) |
|---|---|---|---|---|---|
| `lex-wall-stone-p1` | IfcWall | `["pedra"]` | `Piso 1` | `Parede Norte` | uniclass/`ss_25`/walls |
| `lex-wall-wood-p1` | IfcWall | `["madeira"]` | `Piso 1` | `Parede Sul` | uniclass/`ss_25`/walls |
| `lex-wall-stone-p2` | IfcWallStandardCase | `["pedra"]` | `Piso 2` | `Parede Poente` | uniclass/`ss_30`/columns |
| `lex-col-stone-p1` | IfcColumn | `["pedra"]` | `Piso 1` | `Pilar Um` | uniclass/`ss_30`/columns |
| `lex-wall-multi-p1` | IfcWall | `["pedra", "granito"]` | `Piso 1` | `Muralha_Sul` | uniclass/`ss_25`/walls **e** secclass/`ss_25`/walls (código duplicado no mesmo elemento) |
| `lex-beam-wood-p2` | IfcBeam | `["madeira"]` | `Piso 2` | `Viga Velha` | **sem classificações** |

### 27.1 Conjuntos esperados (declarados independentemente, à mão)

- **Aceitação** — plano equivalente a `"paredes de pedra no piso 1"`
  (`ifc_class="IfcWall"`, `material=["pedra"]`, `storey="1"`):
  **`{lex-wall-stone-p1, lex-wall-multi-p1}`** — exatamente, como conjunto
  (`lex-wall-wood-p1` cai pelo material, `lex-wall-stone-p2` pelo piso,
  `lex-col-stone-p1` pela classe, `lex-beam-wood-p2` por tudo).
  `lex-wall-stone-p2` prova que a variante `IfcWallStandardCase` continua
  incluída pela classe mas excluída pelo piso.
- **Material-only** `["pedra"]`: `{lex-wall-stone-p1, lex-wall-stone-p2,
  lex-col-stone-p1, lex-wall-multi-p1}`.
- **Storey-only** `"1"`: `{lex-wall-stone-p1, lex-wall-wood-p1,
  lex-col-stone-p1, lex-wall-multi-p1}`.
- **Name-only** `"Muralha_Sul"`: `{lex-wall-multi-p1}` (e `"muralha_sul"`
  idem, case-insensitive).
- **Multi-material** `["pedra","madeira"]` sem classe:
  `{lex-wall-stone-p1, lex-wall-wood-p1, lex-wall-stone-p2, lex-col-stone-p1,
  lex-wall-multi-p1, lex-beam-wood-p2}` — os seis elementos (OR dentro da
  dimensão: todos têm pedra ou madeira).
- **Semântico com prefiltro**: kNN com `ifc_class="IfcWall"`,
  `material=["pedra"]`, `storey="1"` devolve ⊆ do conjunto de aceitação.
- **Agregação classification (sem filtro):** buckets exatos
  `[{"key": "ss_25", "count": 3}, {"key": "ss_30", "count": 2}]` — `ss_25`
  conta `lex-wall-stone-p1`, `lex-wall-wood-p1`, `lex-wall-multi-p1`
  (o código duplicado de `lex-wall-multi-p1` conta **uma** vez: elementos,
  não factos — se contasse factos seria 4, e o teste distingue-o);
  `ss_30` conta `lex-wall-stone-p2`, `lex-col-stone-p1`;
  `lex-beam-wood-p2` não aparece em nenhum bucket.
- **Agregação classification filtrada** (`ifc_class="IfcBeam"`):
  `buckets == []`.
- **Agregação count com filtro lexical** (`agg_field="count"`,
  `ifc_class="IfcWall"`, `material=["pedra"]`, `storey="1"`): `total == 2`.
- **Forma histórica errada falha:** (a) a agregação plana legacy
  `{"terms": {"field": "classifications.name"}}` contra o cluster real
  produz erro OpenSearch (fielddata em `text`) — o teste espera exceção;
  (b) `{"terms": {"field": "classifications.code"}}` **sem** `nested` devolve
  zero buckets apesar de existirem 5 elementos classificados — o teste
  assere `buckets == []` para provar que o wrapper `nested` é obrigatório.

## 28. Métricas e gates de avaliação dirigida

A avaliação dirigida vive na integração (§27) com comparação por **conjunto
exato** (`set(actual_ids) == expected_ids` — nem subconjunto nem
superconjunto) e buckets por **lista exata ordenada** (`[(key, count), …] ==`).
Baseline HBIM-005: o gate existente (`test_eval_baseline`, 6 testes) tem de
ficar verde contra a baseline com a **única** atualização cirúrgica de M10;
`queries.jsonl`, `dataset.json`, corpus e qrels byte-idênticos; a prova
estrutural de um-só-caminho (M10) faz parte da validação.

## 29. Anti-tautologia (vinculativo)

1. Repetir a query de aceitação **sem** a cláusula de storey devolve um
   **superconjunto estrito** (inclui `lex-wall-stone-p2`) — prova que o
   filtro de storey faz trabalho e que igualdade de conjuntos falharia.
2. Repetir sem a cláusula de material inclui `lex-wall-wood-p1` — idem para
   material.
3. Uma cópia mutada dos buckets esperados (uma contagem +1) falha a
   comparação exata (teste demonstra `!=`).
4. Os conjuntos esperados são constantes literais no teste, escritas de §27.1
   — nunca derivadas da resposta nem construídas pelos builders de produção.
5. A agregação plana histórica (§27.1) falha contra o cluster real — o teste
   pina o modo de falha.

## 30. Testes unitários normativos

`backend/tests/test_lexical.py` (offline):

1. cláusulas exatas por dimensão (dicts literais); `None`/vazio → ausência;
   dedup de materiais; `TypeError` para tipos errados sem eco.
2. tabela completa de `storey_term_values` (todas as linhas de §15, incluindo
   `0`→r/c/terreo, `-1`→cave, `L0`, o zero-padding `7`→inclui `07`, negativo
   `-1` sem forma `0N`, e o fallback minúsculo para valores fora das formas).
3. `lexical_filter_clauses` ordem fixa material→storey→name; lista vazia
   quando nada há.
4. `build_opensearch_query`: dict **exato** de §18; golden sem lexicais
   (§16); kNN com prefiltro (§19); replay de paginação (§20); condições
   numéricas + lexicais juntas; plano com tudo (`ifc_class`+`project_id`+
   conditions+material+storey+name) — composição AND completa.
5. `build_aggregation_query`: classification nested exata (§21); flat
   inalterada para `material`/`storey`/`count`; filtros lexicais anexados;
   `count` sem aggs.
6. `parse_classification_buckets`: contagem de elementos, ordenação
   `(-count, key)`, resposta malformada → `ValueError` (por nível de chave em
   falta), buckets vazios → `[]`.
7. `execute_aggregation` (com cliente fake): despacho nested vs flat; flat
   byte-idêntico ao atual.
8. caminhos de campo: só os de §8/§10; `classifications.name` ausente de
   qualquer dict construído; tipos de query emitidos ⊆ {term, terms}
   (varrimento estrutural dos dicts).
9. import-safety subprocess + socket-bomb + AST (sem `open(`/clientes/
   `query_string`/`wildcard`/`regexp`/`script` no source).
10. determinismo: igualdade repetida; `PYTHONHASHSEED` subprocess;
    inputs não mutados (lista de materiais do caller fica igual).
11. `LEXICAL_TERMS_VERSION == "1"`; superfície pública exata de §8 no
    **módulo** `retrieval.lexical` (`__all__`); a superfície do **pacote**
    `retrieval` permanece a de HBIM-041 (o teste protegido
    `test_public_surface_is_exact` continua verde sem alterações).

## 31. Testes de integração obrigatórios

`backend/tests/integration/test_lexical_filters_apply.py` (marker
`integration`; Testcontainers `opensearchproject/opensearch:2.19.1`, efémero,
loopback; índice criado com `create_index` de produção; **builders e parsers
de produção em todo o lado no caminho "actual"** — queries JSON escritas à mão
existem **apenas** nas sondas de forma-histórica-errada de §27.1, cujo
propósito é provar que essa forma falha no cluster, nunca para produzir o
resultado "actual" de uma comparação):

1. aceitação `"paredes de pedra no piso 1"` — conjunto exato §27.1, incluindo
   a prova storey com label real `"Piso 1"` casada pela expansão canónica
   `"1"`;
2. material-only, storey-only, name-only (incl. case-insensitive), multi-
   material — conjuntos exatos;
3. semântico kNN com prefiltro — ⊆ aceitação (com embeddings literais);
4. paginação: replay do plano devolve o mesmo conjunto por páginas;
5. buckets de classificação exatos + elemento-vs-facto (código duplicado) +
   filtrada vazia + count filtrado (§27.1);
6. formas históricas erradas falham (§27.1 (a) e (b));
7. anti-tautologia §29.1–29.3;
8. limpeza: apenas o índice sintético criado pelo teste é apagado (nome
   dedicado; teardown falha se o índice não for o esperado).

## 32. Testes de regressão

- Suites HBIM-040 (166) e HBIM-041 (188) verdes sem alterações.
- Suite unitária completa; suite de integração completa (incluindo
  `test_eval_baseline` — baseline byte-idêntica, prova M8);
  `test_opensearch_smoke`, lifecycle, indexers, mappings intactos.
- Ruff; mypy bloqueante incluindo `retrieval.lexical`; `git diff --check`;
  SHA-256 dos protegidos §7.

## 33. Gates de validação completos

Ordem §36; nenhum resultado reutilizado de sessões anteriores; focused em
default + seeds 1,2,3,7,99 + `no:randomly`.

## 34. Critérios de aceitação

1. `retrieval/lexical.py` existe com a superfície exata de §8. *(30.11)*
2. Material aplicado: terms sobre `material`; OR interno; AND externo.
   *(30.1, 31.2)*
3. Storey aplicado com expansão §15; label real `"Piso 1"` casa com canónico
   `"1"` em OpenSearch real. *(30.2, 31.1)*
4. Name aplicado: term exato case-insensitive em `name.keyword`. *(30.1, 31.2)*
5. Query estruturada de aceitação = dict §18; kNN herda prefiltro; paginação
   preserva. *(30.4, 31.3, 31.4)*
6. `"paredes de pedra no piso 1"` → exatamente
   `{lex-wall-stone-p1, lex-wall-multi-p1}` em cluster real. *(31.1)*
7. Agregação classification nested §21 com contagem de **elementos** §22–23;
   buckets exatos `ss_25:3, ss_30:2`; duplicado conta uma vez; filtrada vazia
   `[]`. *(30.5–30.7, 31.5)*
8. Formas históricas erradas falham em cluster real. *(31.6)*
9. Filtros lexicais também nas agregações; `count` global intacto. *(30.5, 31.5)*
10. Golden: plano sem lexicais → query byte-idêntica à anterior; agregações
    planas byte-idênticas. *(30.4, 30.5)*
11. Anti-tautologia §29 provada. *(31.7, 30.6)*
12. HBIM-005: `test_eval_baseline` verde; `queries.jsonl`, `dataset.json`,
    corpus e qrels byte-idênticos; `current_system.json` difere da versão
    anterior **exatamente** na chave
    `compatibility_metrics.snapshots["q-rs-classification-agg"]`
    (`{"error": "RequestError"}` → `{"agg_total": 28, "buckets":
    {"ss_25": 28}}`), provado por diff programático de um-só-caminho. *(32, M10)*
13. Import-safety, sem query types proibidos, determinismo hash-seed. *(30.8–30.10)*
14. Suites HBIM-040/041 verdes sem alterações; suite completa verde; Ruff e
    mypy verdes; `git diff --check` limpo. *(32, 33)*
15. Nenhum ficheiro fora de §6; protegidos §7 intactos; nenhuma dependência
    nova; nenhum código HBIM-050. *(git + SHA + inspeção)*
16. Nenhuma decisão pendente. *(inspeção)*

## 35. (reservado)

## 36. Comandos de validação

```bash
conda run -n hbim-rag python -m pytest backend/tests/test_lexical.py -q -o addopts=""
# + --randomly-seed=1,2,3,7,99 e -p no:randomly

conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_lexical_filters_apply.py -m integration -q -o addopts=""

conda run -n hbim-rag python -m pytest \
  backend/tests/test_router.py backend/tests/test_routing_gold.py \
  backend/tests/test_query_parser.py backend/tests/test_parser_gold.py -q -o addopts=""

conda run -n hbim-rag python -m pytest backend/tests -m "not integration" -q -o addopts=""
conda run -n hbim-rag python -m pytest backend/tests -m integration -q -o addopts=""
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -p no:randomly
conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_eval_baseline.py -m integration -q -o addopts=""

conda run -n hbim-rag python -m ruff check backend
# mypy: comando explícito do ci.yml já atualizado (inclui lexical.py)

sha256sum backend/eval/baselines/current_system.json backend/eval/dataset/dataset.json \
          backend/eval/dataset/queries.jsonl backend/api/main.py \
          backend/retrieval/router.py backend/retrieval/query_parser.py

git status --short --untracked-files=all
git --no-pager diff --name-status
git diff --check
```

## 37. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Labels de storey reais fora da expansão fechada | Fronteira v1 documentada; expansão cobre PT/EN comuns + formas acentuadas; `LEXICAL_TERMS_VERSION` versiona; fixture prova o label realista `"Piso 1"` |
| Materiais compostos (`"pedra calcária"`) não casam com `pedra` | Fronteira v1 (igualdade exata de keyword); resolver exige analisador/expansão — candidato a HBIM-050+, não silenciosamente aqui |
| Filtro derruba resultados esperados no eval | §1.5.1/M8 verificado: snapshot de material invariante por construção; gate de integração prova |
| Atualização da baseline esconder drift não intencional | M10: diff programático prova um-só-caminho; `correctness_metrics` intocado; valor novo declarado à mão do corpus antes de correr o gate |
| Resposta nested malformada aceite | `ValueError` por chave em falta; teste dedicado |
| Truncagem de buckets | `size=200` como legacy; documentado |
| Query drift por ordem de dict | Ordem fixa de cláusulas; testes de dict exato; `PYTHONHASHSEED` |
| Teardown apagar índices alheios | Nome dedicado + guard no teardown |

## 38. Adiado deliberadamente (HBIM-050+)

BM25/candidatos lexicais a sério, dense/RRF/rerank, EvidencePack, expansão de
sinónimos de materiais, migração de aliases (HBIM-023), remoção dos modelos
mortos de `api/search.py`, métricas Prometheus de filtros.

## 39. Entregáveis

**Criar:** `backend/retrieval/lexical.py`, `backend/tests/test_lexical.py`,
`backend/tests/integration/test_lexical_filters_apply.py`, esta spec.
**Modificar:** `backend/api/search.py`, `backend/retrieval/__init__.py`,
`pyproject.toml`, `.github/workflows/ci.yml`,
`docs/development/LOCAL_SETUP.md`, `docs/implementation/IMPLEMENTATION_STATUS.md`.
**Dois commits:** spec e implementação, mensagens exatas da configuração.
**Relatório final** com `Self-review findings` e §34 avaliado
`PASS`/`FAIL`/`PARTIAL` com evidência concreta.
