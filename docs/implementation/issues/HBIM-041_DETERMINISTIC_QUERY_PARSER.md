# HBIM-041 — Deterministic Query Parser

> **Tipo:** especificação executável de issue.
> **Estado:** aprovada para implementação.
> **Branch obrigatória:** `feat/hbim-041-deterministic-query-parser`.
> **Depende de:** HBIM-040 (router determinístico — merged em `main`, `2ff0315`),
> HBIM-005 (harness de avaliação), HBIM-002/003/004 (settings, API, CI).
> **Bloqueia:** HBIM-042 (filtros lexicais + agregação), HBIM-050.

---

## 1. Contexto auditado

O parsing estruturado é hoje feito por **cinco prompts LLM** em
`backend/api/prompts.py`, chamados em **sete call sites** de
`backend/api/main.py` (numeração do código em `main` @ `2ff0315`):

| Prompt | Call sites | Produz | Modelo pydantic |
|---|---|---|---|
| `EXTRACT_IFC_CLASS` | `main.py:344` (search), `main.py:453` (aggregation) | `ifc_class` | `ExtractedIfcClass` |
| `EXTRACT_FILTERS` | `main.py:350` (search), `main.py:459` (aggregation) | `name`, `project_id`, `project_name` | `ExtractedFilters` |
| `EXTRACT_CONDITIONS` | `main.py:358` (search) | `conditions` | `ExtractedConditions` |
| `EXTRACT_AGGREGATION` | `main.py:445` (aggregation) | `agg_field` | `ExtractedAggregation` |
| `EXTRACT_DETAIL_REF` | `main.py:414` (detail) | `index` | `DetailRef` |

Factos verificados no código:

- O prompt `EXTRACT_FILTERS` **não extrai** `material` nem `storey` (os campos
  existem em `ExtractedFilters` mas o prompt nunca os pede): a extração legacy
  destes campos é inexistente. Extraí-los deterministicamente é uma **melhoria**
  mensurável.
- `build_opensearch_query` (`api/search.py:305`) usa apenas `ifc_class`,
  `project_id` e `conditions`. `material`/`storey`/`name` **não são aplicados**
  — é o GAP §1.5 do ROADMAP, cuja correção é **HBIM-042**, não esta issue.
- `IFC_CLASS_TABLE` (`api/prompts.py:6–107`) é o mapa fechado termo→classe
  IFC do sistema legacy: **100 pares** PT/EN cobrindo **21 classes** (93
  termos únicos após normalização — 7 pares acentuado/não-acentuado colapsam).
- Os cinco prompts contêm **38 exemplares few-shot** input→output — o único
  registo committed e reprodutível do comportamento do extrator legacy.
- `CLASSIFY_INTENT` está definido mas sem consumidor desde HBIM-040; a spec
  HBIM-040 §26 fixou: *"Remover `CLASSIFY_INTENT` de `prompts.py` e os prompts
  de extração → HBIM-041"*.
- O guard `user_explicitly_mentions_project_id` + `PROJECT_ID_MARKER_RE`
  (`main.py:138–157`) impõe que `project_id` nunca é inferido — contrato ativo
  que o parser tem de respeitar por construção.
- `retrieval/` (HBIM-040) importa **apenas stdlib**; `api/search.py` importa
  `openai` e `opensearchpy` ao nível do módulo — o parser **não pode** importar
  `api.search`.
- HBIM-005: `eval/dataset.py::_validate_checksums` valida a chave-set exata
  `{corpus,queries,qrels}.jsonl` e **não varre o diretório** (sem
  `glob`/`iterdir`); `eval/baselines/` só é lido por
  `tests/integration/test_eval_baseline.py`, por nome explícito
  (`current_system.json`). Ficheiros novos nesses diretórios são inertes.
- `eval/dataset/queries.jsonl` (HBIM-005) contém **planos estruturados**, não
  linguagem natural — não serve de input a um gold de parsing.

---

## 2. Precedência de fontes

1. `docs/implementation/ROADMAP.md` §404–446 (M4) e §834–839 (HBIM-041) e
   `docs/architecture/HBIM_RAG_DECISIONS.md` §6.
2. Contratos aceites de milestones concluídas: HBIM-040 (router, normalização,
   GlobalId), HBIM-010 (`global_id` preservado byte a byte), HBIM-005 (harness).
3. Comportamento público atual e compatibilidade (`SearchPlan`, paginação,
   `/chat`).
4. Segurança, determinismo, testabilidade, import-safety.
5. Scope mínimo de HBIM-041.
6. Fronteiras de milestones futuras, sobretudo HBIM-042.

---

## 3. Conflitos ROADMAP ↔ código e decisões fixadas

| # | Conflito | Decisão | Autoridade |
|---|---|---|---|
| C1 | ROADMAP §417–424 esboça `ParsedQuery(BaseModel)` com campo `route: Route` | `ParsedQuery` é **dataclass frozen stdlib** e **não tem campo `route`**. O esboço M4 agrega router+parser; o router já foi entregue (HBIM-040) com dataclasses stdlib e o parser não pode reclassificar a rota ("não duplicar routing"). `pydantic` em `retrieval/` violaria o contrato de import-safety aceite em HBIM-040. | 2, 4 sobre esboço de 1 |
| C2 | ROADMAP M4 inclui `lexical.py`, aplicação de filtros e correção da agregação | Fora desta issue: a tabela de issues do próprio ROADMAP atribui isso a **HBIM-042** (§840–845). Nenhuma alteração a `api/search.py`. | 1, 6 |
| C3 | ROADMAP HBIM-041 lista só `query_parser.py`, `prompts.py`, `test_query_parser.py` | Insuficiente para "0 LLM no parsing": os call sites vivem em `api/main.py`. §6 autoriza explicitamente os ficheiros adicionais mínimos (lista fechada). | 1 (critério de aceitação) resolve 1 (lista de ficheiros) |
| C4 | "Deprecar prompts" (ROADMAP) vs "Remover" (HBIM-040 spec §26) | **Remover** `CLASSIFY_INTENT`, os cinco `EXTRACT_*` de parsing e `IFC_CLASS_TABLE` de `prompts.py` (ficam sem consumidor). "Deprecado" = **removido do módulo, com teste que o prova**. `REWRITE_QUERY`, `EXTRACT_EMBEDDING_QUERY`, `FILTER_RESULTS_BATCH` e os três `*_RESPONSE_FORMAT` permanecem (caminhos de resposta/semântica ativos). | 2 (contrato HBIM-040) concretiza 1 |
| C5 | `EXTRACT_EMBEDDING_QUERY` é LLM e fica | Não é parsing estruturado: constrói texto para embedding no caminho semântico. O ROADMAP M4 lista exatamente os cinco prompts substituídos e este não está lá. Fica, recebendo agora os valores do parser. | 1, 6 |
| C6 | Input do parser: `request.message` (como o router) ou `effective_query` (como os extratores legacy)? | **`effective_query`** — exatamente a string que os cinco extratores legacy recebiam (verbatim `request.message` no primeiro turno; reescrita por `REWRITE_QUERY` quando há histórico). O parser é uma função pura do seu input; nenhum LLM é *exigido* para parsing (sem histórico não há reescrita nenhuma). `REWRITE_QUERY` não pertence à lista de deprecação. O router continua a receber `request.message` (HBIM-040 §C6, inalterado). | 3, 2 |
| C7 | Fronteira route↔parser na agregação | O router decide a rota; o parser extrai `agg_field`. Quando a rota é agregação e `parsed.agg_field is None`, o endpoint usa `"count"` (default determinístico documentado, §20). O parser nunca lê nem devolve rotas. | 2 |

---

## 4. Objetivos

1. `backend/retrieval/query_parser.py`: parser determinístico, puro, total,
   stdlib-only, para `ifc_class`, `materials`, `storey`, condições numéricas,
   `global_ids`, `agg_field`, `name`, `project_id`, `project_name`,
   `refers_previous` e referência de detalhe.
2. Substituir os **sete call sites LLM** de parsing em `api/main.py` pelo
   parser: zero chamadas LLM no parsing em todos os caminhos.
3. Remover de `prompts.py` os prompts de parsing e `CLASSIFY_INTENT` (C4).
4. Gold de parsing committed + baseline legacy congelada committed, com gate
   offline de paridade/melhoria e prova anti-tautologia.
5. Preservar intactos o contrato do router (HBIM-040), o contrato HTTP de
   `/chat`, a paginação e o `SearchPlan`.

## 5. Não objetivos

- Aplicar `material`/`storey`/`name` a queries OpenSearch (HBIM-042).
- Corrigir a agregação de classificação (HBIM-042).
- `retrieval/lexical.py`, BM25, dense, RRF, rerank, EvidencePack (HBIM-042+).
- Alterar `api/search.py` (protegido nesta issue), mappings ou indexers.
- Remover `REWRITE_QUERY`, `EXTRACT_EMBEDDING_QUERY` ou `FILTER_RESULTS_BATCH`.
- Query expansion pós-router por AMALIA (decisão §6 "onde AMALIA ainda entra").
- Alterar regras de routing (qualquer necessidade real dispara o repair loop).

---

## 6. Ficheiros permitidos

**Criar:**

- `backend/retrieval/query_parser.py`
- `backend/tests/test_query_parser.py`
- `backend/tests/test_parser_gold.py`
- `backend/eval/dataset/parser_gold.jsonl`
- `backend/eval/baselines/legacy_extraction.json`
- esta spec

**Modificar (lista fechada; motivo obrigatório):**

- `backend/api/main.py` — substituir os sete call sites de parsing (§22);
  nenhuma outra alteração.
- `backend/api/prompts.py` — exclusivamente as remoções de C4.
- `backend/retrieval/router.py` — **exclusivamente aditivo**: aliases públicos
  `GLOBAL_ID_RE = _GLOBAL_ID_RE` e `fold_text = _fold` + entradas em `__all__`.
  Zero alterações de comportamento (§12).
- `backend/retrieval/__init__.py` — re-exportar a API pública do parser.
- `backend/tests/test_router.py` — **exclusivamente** atualizar
  `test_classify_intent_is_not_used_by_the_endpoint` (§23): a asserção
  `"CLASSIFY_INTENT" in prompts` inverte-se com a remoção C4. Nenhum outro
  teste pode mudar.
- `pyproject.toml` — acrescentar `retrieval.query_parser` ao override strict.
- `.github/workflows/ci.yml` — acrescentar `backend/retrieval/query_parser.py`
  à lista mypy.
- `docs/development/LOCAL_SETUP.md` — secção operacional HBIM-041.
- `docs/implementation/IMPLEMENTATION_STATUS.md` — só no fim, com números reais.

Qualquer outra alteração é violação de scope e bloqueia o commit.

## 7. Ficheiros protegidos (byte-idênticos)

- `backend/api/search.py` — **nenhum campo, modelo ou função muda** (C2)
- `backend/eval/metrics.py`, `backend/eval/run_eval.py`, `backend/eval/dataset.py`
- `backend/eval/dataset/{corpus,queries,qrels}.jsonl`, `dataset.json`,
  `routing_gold.jsonl`
- `backend/eval/baselines/current_system.json`
- `backend/retrieval/router.py` **exceto** o bloco aditivo de §6; o
  comportamento de `route()` é protegido por a suite HBIM-040 permanecer verde
  sem alterações (além da única de §6)
- `backend/tests/conftest.py`, `backend/tests/test_routing_gold.py`
- `backend/tests/**` restantes (nenhum teste existente é adaptado além do
  único caso de §6)
- `backend/canonical/**`, `backend/ingestion/**`, `backend/shared/**`
- `backend/tests/fixtures/**`, `frontend/**`
- `backend/requirements*.txt` — nenhuma dependência nova
- `.gitignore`

Guardar SHA-256 antes e reverificar depois.

---

## 8. Contrato público do parser

`backend/retrieval/query_parser.py` exporta exatamente:

```
PARSER_TERMS_VERSION: str = "1"
IFC_TERM_TO_CLASS: Mapping[str, str]          # imutável (MappingProxyType)
MATERIAL_CANONICAL: Mapping[str, str]         # imutável
AGG_FIELDS: frozenset[str]                    # {"count","material","ifc_class","storey","classification","project","project_id"}
NumericCondition                              # dataclass frozen
ParsedQuery                                   # dataclass frozen
parse_query(text: str) -> ParsedQuery
parse_detail_ref(text: str, num_results: int) -> int
```

`backend/retrieval/__init__.py` re-exporta estes nomes além dos de HBIM-040.

### 8.1 Import-safety

Importar `retrieval.query_parser` não pode puxar nenhum de: `shared.*`,
`api.*`, `pydantic`, `fastapi`, `openai`, `opensearchpy`, `dotenv`, `torch`,
`sentence_transformers`, `transformers`, `ifcopenshell`, `ingestion`, `eval`,
`requests`. Imports permitidos: stdlib (`re`, `unicodedata`, `dataclasses`,
`enum`, `types`, `typing`) e `retrieval.router`. Proibido no módulo: relógio,
aleatoriedade, ficheiros, sockets, `open(`, `eval(`, `exec(`. Verificação em
subprocess fresco + AST, como HBIM-040 §19.8.

---

## 9. Modelos de input/output

```python
@dataclass(frozen=True)
class NumericCondition:
    field: str      # "height" | "area" | "volume" | "thickness"
    op: str         # "eq" | "approx" | "gt" | "gte" | "lt" | "lte"
    value: float    # sempre float; nunca bool; finito

@dataclass(frozen=True)
class ParsedQuery:
    raw: str                                   # input verbatim (ROADMAP §423)
    ifc_class: str | None
    materials: tuple[str, ...]                 # canónicos, ordenados, únicos
    storey: str | None                         # forma canónica §17
    conditions: tuple[NumericCondition, ...]   # ordem de aparição, sem duplicados
    global_ids: tuple[str, ...]                # ordem de aparição, únicos, caso exato
    agg_field: str | None                      # ∈ AGG_FIELDS | None
    name: str | None
    project_id: str | None
    project_name: str | None
    refers_previous: bool

    def to_dict(self) -> dict[str, object]     # tuplos→listas; inclui todas as chaves
```

Regras de tipo: `parse_query` levanta `TypeError` para `text` não-`str` (sem
ecoar o input na mensagem); nunca levanta para qualquer `str`.
`parse_detail_ref` levanta `TypeError` para tipos errados (`bool` **não** é
aceite como `int`) e `ValueError` para `num_results < 1`; devolve sempre
`int` em `[1, num_results]`.

**Relação com os modelos existentes.** `api.search.Condition`, `SearchPlan` e
os `Extracted*` **não mudam**. O parser não pode importá-los (§8.1); a ponte é
feita em `api/main.py` (§22): os objetos `ExtractedIfcClass`,
`ExtractedFilters` e `ExtractedConditions` passam a ser construídos a partir do
`ParsedQuery` (deixam de ser parse de JSON de LLM) e continuam a alimentar
`extract_embedding_query` e o `SearchPlan` sem mudança de assinatura. Um teste
prova a ponte: `Condition(field=c.field, op=c.op, value=c.value)` aceita todo o
`NumericCondition` e o vocabulário de `op`/`field` do parser é exatamente o que
`build_opensearch_query` já consome (`eq/approx/gt/gte/lt/lte` ×
`height/area/volume/thickness`). Não há enum novo nem modelo incompatível.

---

## 10. Reutilização dos contratos HBIM-040

- **Normalização:** o parser usa `retrieval.router.normalize_query` (vista de
  termos) e `retrieval.router.fold_text` (vista com pontuação, novo alias
  público de `_fold`) — **nenhuma função de normalização própria**. Um teste
  assere que `query_parser` não define nenhuma função com
  `unicodedata.normalize` própria (AST).
- **GlobalId:** o parser usa `retrieval.router.GLOBAL_ID_RE` (novo alias
  público de `_GLOBAL_ID_RE`) — **não existe segundo regex**. Um teste assere
  por AST que `query_parser.py` não contém nenhum literal `re.compile` cujo
  padrão contenha `{22}`.
- **`refers_previous`:** termos `retrieval.router.PREVIOUS_RESULT_TERMS` com o
  mesmo teste de fronteira de palavra (espaço-padding). Consistência garantida
  por teste: para todas as queries do gold,
  `parse_query(q).refers_previous == route(q, RouterContext(True)).signals.references_previous_result`.
- **Rota:** o parser não lê, não calcula e não devolve rotas. A orquestração é
  do endpoint: `route()` decide, `parse_query()` extrai.

## 11. Normalização (política exata)

- Vista A — termos (`normalize_query`): NFKD, remoção de marcas combinantes,
  casefold, pontuação→espaço, colapso de espaços. Usada por: dicionário IFC,
  materiais, `agg_field`, `refers_previous`, ordinais de detalhe.
- Vista B — numérica (`fold_text`): NFKD + remoção de marcas + casefold,
  **pontuação preservada**. Usada por: condições numéricas (separador decimal
  `,`/`.`), storey (sinal negativo, `r/c`, `res-do-chao`).
- Vista C — raw: string original intocada. Usada por: `global_ids`
  (case-sensitive, HBIM-010 §174), `name`, `project_id`, `project_name`
  (preservação de maiúsculas do utilizador) e `ParsedQuery.raw`.
- Output determinístico: `materials` ordenado lexicograficamente;
  `conditions` e `global_ids` por ordem de aparição; nenhuma iteração de
  `set`/`dict` chega ao output (prova: `PYTHONHASHSEED` variado, §32).

## 12. Alterações a `retrieval/router.py` (aditivas)

Exatamente isto e nada mais:

```python
GLOBAL_ID_RE = _GLOBAL_ID_RE      # HBIM-041: contrato único de GlobalId
fold_text = _fold                 # HBIM-041: normalização única (vista B)
```

mais `"GLOBAL_ID_RE"` e `"fold_text"` em `__all__`. Prova de não-regressão: a
suite HBIM-040 inteira passa sem alterações (além da única autorizada em §6) e
`git diff` de `router.py` mostra apenas linhas adicionadas.

---

## 13. Precedência de extração

Cada campo é extraído **independentemente** — não há precedência entre campos
(o router é quem escolhe a estratégia). Dentro de cada campo a precedência é:

1. `ifc_class`: §15 (primeira posição; empate → termo mais longo).
2. `materials`: todos os que casarem (§16).
3. `storey`: primeiro padrão que casar na ordem de §17.
4. `conditions`: varrimento esquerda→direita com consumo de spans (§18).
5. `global_ids`: todas as ocorrências (§19).
6. `agg_field`: primeira regra que casar na ordem de §20.
7. `name`/`project_id`/`project_name`: §21 (spans de id/projeto excluem name).

## 14. (reservado — sem conteúdo; numeração estável)

## 15. `ifc_class`

**Dicionário.** `IFC_TERM_TO_CLASS` = os **100 pares** de `IFC_CLASS_TABLE`
(`api/prompts.py:6–107` @ `2ff0315`), com os termos normalizados na vista A
(ex.: `corrimão`→`corrimao`; os 7 pares acentuado/não-acentuado colapsam,
dando **93 chaves únicas**), **mais** os **21 nomes literais de classe**
normalizados (`ifcdoor`→`IfcDoor`, …), porque o utilizador pode escrever o nome
da classe (exemplar legacy a2). Migração provada: um teste golden assere os
100 pares exatos (na forma normalizada, 93 chaves) e as 21 classes — a tabela
legacy não perde nem altera nenhuma entrada ao mudar de ficheiro.

**Matching.** Sobre a vista A com fronteira de palavra (espaço-padding para
termos multi-palavra, como HBIM-040 `_matches`). Entre todos os termos
presentes ganha o de **menor posição inicial**; empate na posição → o termo
**mais longo** (`"fachada cortina"` ganha a `"fachada"`... que nem é termo;
`"pipe segment"` ganha a `"pipe"`). Output: exatamente **uma** classe ou
`None` (regra legacy: "Retorna APENAS UM valor (…) o principal" ⇒ o primeiro
mencionado). Sem classe → `None`; "elementos" genérico → `None` (não é termo).

## 16. `materials`

Canónicos: `betao, madeira, pedra, calcario, tijolo, granito, argamassa`
(vocabulário material de HBIM-040 §11.2 menos os genéricos
`material`/`materiais`, que não são substâncias). `MATERIAL_CANONICAL` mapeia
singular e plural (`betoes, madeiras, pedras, calcarios, tijolos, granitos,
argamassas`) → canónico singular. Matching na vista A com fronteira de
palavra; output ordenado lexicograficamente, sem duplicados. `"madeirense"`
não dispara `madeira`. A extração é **melhoria** face ao legacy (que não
extraía materiais — §1); a paridade nos pares cobertos não é afetada porque
nenhum exemplar legacy cobre o campo `materials`.

## 17. `storey`

Padrões sobre a vista B, na ordem; o primeiro que casar decide; forma canónica
entre parênteses:

1. `(piso|andar|nivel|storey)\s+(-?\d+)` → o inteiro como string (`"piso -1"`
   → `"-1"`).
2. `(-?\d+)\s*\.?\s*o\s+(piso|andar|nivel)\b` — o marcador ordinal `o` é
   **obrigatório**: cobre `1º piso`/`1.º piso`/`1o piso`, cujo `º` (U+00BA)
   decompõe por NFKD para `o` → (`"1"`). `"1 piso"` sem marcador (contagem:
   *"o edifício tem 1 piso"*) **não** casa.
3. `(primeiro|segundo|terceiro|quarto|quinto|sexto|setimo|oitavo|nono|decimo)\s+(piso|andar|nivel)`
   → (`"1"`…`"10"`); e a forma invertida `(piso|andar|nivel)\s+<ordinal>`.
4. `(piso|andar|nivel|storey)\s+([a-z]\d+)` → token maiusculizado
   (`"nivel l0"` → `"L0"`).
5. `r/c` | `res[- ]do[- ]chao` | `terreo` (fronteira de palavra) → `"0"`.
6. `cave` (fronteira de palavra) → `"-1"`.

Sem padrão → `None`. `"quantas portas ha no piso 1?"` produz **storey `"1"` e
`agg_field` `"count"` simultaneamente** — campos independentes (§13); quem
decide o que usar é a rota. `"entre o piso 1 e o piso 2"` casa o padrão 1 na
primeira ocorrência → `"1"` (documentado; intervalos de piso ficam para o
parser evoluir com gold novo). Nomes de piso fora destes padrões (ex.: `"piso
principal"`) → `None`.

## 18. Condições numéricas

**Métricas** (vista B): `altura`→`height`, `area`→`area`, `volume`→`volume`,
`espessura`→`thickness`, `largura`→`thickness`. Métricas não suportadas (ex.:
`comprimento`, `peso`) **não produzem condição** — nunca são mapeadas
silenciosamente para outra métrica.

**Unidades:** `m2`|`m²`(→NFKD `m2`)→dim área; `m3`|`m³`→dim volume;
`metros?`|`m`→dim linear; `cm`→linear ×0.01; `mm`→linear ×0.001. Números:
`\d+(?:[.,]\d+)?` — vírgula decimal equivale a ponto (`3,5`→3.5). Sem sinal,
sem notação científica, sem separador de milhares (`"1.000"` lê 1.0 —
fronteira documentada do vocabulário v1; `NaN`/`inf` são estruturalmente
impossíveis). `value` é sempre `float` (nunca `bool`) e finito.

**Operadores:** `gt`: `mais de|maior(es)? que|acima de|superior(es)? a`;
`gte`: `pelo menos|no minimo`; `lt`: `menos de|menor(es)? que|abaixo de|inferior(es)? a`;
`lte`: `no maximo`; `eq`: `exatamente|igual a`; intervalo: `entre N e M`.

**Gramática.** Varrimento esquerda→direita sobre a vista B; em cada posição
os padrões são tentados **nesta ordem fixa** — `G1, G6, G2, G4, G5` — e o
primeiro que casar consome o seu span (o varrimento continua depois dele);
ordem de aparição preservada; duplicados exatos `(field,op,value)` removidos
mantendo o primeiro. `exatamente`/`igual a` são apenas o operador `eq` dentro
de G1/G2 — não existe padrão separado. **Adjacência:** em G1 a MÉTRICA e o
OP são adjacentes, permitindo entre eles no máximo um token do conjunto
`{de, da, do, das, dos}`; qualquer outro token intermédio impede o match.

| # | Forma | Resultado |
|---|---|---|
| G1 | `MÉTRICA [conector] OP N [UNIDADE]` (`área superior a 10 m2`, `espessura inferior a 0.3`) | `(métrica, op, N)` |
| G6 | `entre N e M [UNIDADE] [de MÉTRICA]` | duas condições: `(campo, gte, min(N,M))` e `(campo, lte, max(N,M))` — extremos invertidos são normalizados; campo por métrica explícita, senão dim da unidade, senão `height` |
| G2 | `OP N (UNIDADE [de MÉTRICA] \| de MÉTRICA)` (`mais de 2 metros`, `exatamente 1.5 metros de altura`, `mais de 2 de altura`) | métrica explícita; senão dim da unidade: área→`area`, volume→`volume`, linear→`height` (regra default legacy, exemplar c1) |
| G4 | `N UNIDADE de MÉTRICA` (`1.5 metros de altura`, sem operador) | `(métrica, approx, N)` |
| G5 | `N UNIDADE` isolado (sem operador nem métrica) | `(dim-da-unidade, approx, N)` — linear→`height` (regra legacy "apenas o número → approx") |

**Guarda de métrica não suportada.** Se a palavra imediatamente anterior ao
início de um match G2/G6 pertencer ao conjunto fechado
`{comprimento, comprimentos, peso, pesos, profundidade, profundidades,
diametro, diametros}`, a condição é **descartada**: `"comprimento superior a
5 metros"` não produz condição nenhuma — uma métrica não suportada nunca
degenera no default `height`.

Coerência dimensional: se a unidade e a métrica coexistirem e as dimensões
divergirem (`altura … 10 m2`), a condição é **descartada** (nunca adivinhada).
**Conversões por divisão inteira do literal:** `cm` → `valor / 100`, `mm` →
`valor / 1000` — obrigatoriamente divisão, nunca multiplicação por `0.01`
(em binário `30 * 0.01 != 0.3`, mas `30 / 100 == 0.3`); um teste pina
`"30 cm"` → `0.3` por igualdade exata. `OP N` sem unidade e sem métrica
(`"mais de 3"`) **não** produz condição (indistinguível de contagem).
`"piso 1"` nunca produz condição (sem unidade, sem métrica, sem operador).

## 19. `global_ids`

`retrieval.router.GLOBAL_ID_RE.findall` sobre a **vista C** (raw): todas as
ocorrências, ordem de aparição, deduplicação mantendo a primeira, bytes e caso
**exatamente** preservados (HBIM-010 §174). A fronteira sintática de falso
positivo de HBIM-040 §11.4 aplica-se inalterada e não é re-testada aqui — é o
mesmo objeto regex.

## 20. `agg_field`

Vocabulário fechado `AGG_FIELDS` = `{"count"} ∪` chaves de
`api.search.AGG_FIELD_MAP` = `{count, material, ifc_class, storey,
classification, project, project_id}` (teste de consistência no lado dos
testes, que podem importar `api.search`). Regras sobre a vista A, na ordem —
a primeira que casar decide:

1. marcador explícito de project_id (§21.1 — mesmo vocabulário) → `project_id`
2. `por (piso|andar|nivel)\b` → `storey`
3. `(quantos|quantas)\s+(projetos|projectos|modelos)\b` ou
   `(quais|que)\s+(sao\s+)?(os\s+|as\s+)?(meus\s+|minhas\s+)?(projetos|projectos|modelos)\b`
   (adjacência com preenchedores fechados — `"que tipos de elementos existem
   nos projetos?"` **não** casa, porque a seguir a `que` vem `tipos`) →
   `project`
4. `materiais`\b (inclui `lista de materiais`, `quais materiais`) → `material`
5. `classificacao|classificacoes`\b → `classification`
6. `pisos|andares|niveis`\b (o token plural presente, sem mais requisitos) →
   `storey`
7. `tipos|classes`\b → `ifc_class`
8. `quantos|quantas|contar|contagem|numero de|quantidade`\b → `count`
9. nenhum → `None`

O parser devolve `None` sem sinais; **o endpoint** aplica o default `"count"`
quando a rota é agregação (C7) — decisão de orquestração, não de parsing.
Verificações contra os 12 exemplares legacy (a1–a12): todas reproduzidas —
incluindo `a6` (`quantas…por piso` → `storey`, regra 2 antes de 8), `a2`
(`número de IfcBuildingElementProxy` → `count`, nenhuma regra 1–7 casa) e
`a11` (`quantos modelos` → `project`, regra 3 antes de 8).

## 21. `name`, `project_id`, `project_name`

1. **`project_id`** — só com marcador explícito, o **mesmo vocabulário** do
   guard ativo (`main.py PROJECT_ID_MARKER_RE`): `project[_ -]?id`,
   `id d[eo] proj(e|c)?to`, `id proj(e|c)?to`, `identificador d[eo] projeto`,
   `codigo d[eo] projeto`, `codigo projeto` (grafias com acento, `código`,
   também cobertas). Valor: o token imediatamente a seguir ao marcador
   (vista C, caso preservado; aspas envolventes e pontuação final removidas),
   aceite **apenas se** contiver pelo menos um dígito, um underscore ou uma
   maiúscula — a forma de código dos exemplares (`SCV_2024`). Um token só de
   minúsculas sem dígitos **não é um valor**: `"quantos project_id distintos
   existem?"` produz `project_id=None` (e `agg_field="project_id"` pela regra
   §20.1). Sem marcador, ou sem valor válido → `None`, **sempre** — nunca
   inferido. O span de qualquer match do marcador (com ou sem valor) é
   excluído dos candidatos a `name`. Consistência com o guard provada por
   teste: para todas as queries do gold,
   `parse_query(q).project_id is not None ⇒ user_explicitly_mentions_project_id(q)`.
2. **`project_name`** — gatilho `(projeto|projecto|modelo)\s+<resto>` (singular,
   vista C case-insensitive): captura a partir do token seguinte e termina no
   **primeiro** de: fim da string; um destes tokens isolados (case-insensitive):
   `no`, `na`, `nos`, `nas`, `com`, `sem`; ou uma vírgula. Pontuação terminal
   (`?`, `.`, `!`) é removida; captura vazia → `None`. Exemplos normativos:
   `"elementos do projeto Mosteiro de Santa Clara a Velha"` →
   `"Mosteiro de Santa Clara a Velha"` (o ` a ` não é token de paragem);
   `"elementos do projeto Alpha no piso 2"` → `"Alpha"`. O span de um marcador
   de project_id (regra 1) é excluído primeiro — `"id do projeto SCV_2024"`
   produz `project_id="SCV_2024"`, `project_name=None` (exemplar f9). Plural
   (`projetos`) não dispara. Sem gatilho → `None`. Nomes de projeto que
   contenham os tokens de paragem são fronteira documentada do vocabulário v1.
3. **`name`** — (a) texto entre aspas `"…"`, `'…'` ou `«…»`; senão (b) o
   primeiro token identificador com underscore `[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+`
   (vista C) que não seja GlobalId (§19) e não esteja dentro dos spans de
   project_id/project_name. Cobre o exemplar f2 (`Artifact_0`). Frases nominais
   livres (`"porta principal"` sem aspas) → `None` — o único exemplar legacy
   committed de `name` é `Artifact_0`; alargar exige gold novo.

## 22. Integração ativa em `api/main.py`

No ramo não-paginação, imediatamente após o evento `router_decision`:

```python
parsed = parse_query(effective_query)
log_preprocess_json("query_parser", {…§27…})
```

e por caminho:

| Caminho | Antes (LLM) | Depois |
|---|---|---|
| search (structured/semantic) | 3 chamadas (`IFC_CLASS`, `FILTERS`, `CONDITIONS`) + guard | ponte pydantic a partir de `parsed` |
| semantic (embedding) | `extract_embedding_query(...)` LLM | **mantém-se** (C5), agora alimentado pela ponte |
| aggregation | 3 chamadas (`AGGREGATION`, `IFC_CLASS`, `FILTERS`) + guards | `agg_field = parsed.agg_field or "count"`; filtros da ponte; zero LLM até à resposta final |
| detail | 1 chamada (`DETAIL_REF`) | `idx = parse_detail_ref(effective_query, len(detail_ids))`; o clamp legado do endpoint desaparece (o parser já devolve clamped) |
| chat | — | — (inalterado) |
| paginação | — | — (inalterada; **não** chama o parser; plano armazenado reutilizado; `clear_plan_inferred_project_id` mantém-se chamado) |

**Guards de project_id.** Os três call sites de guard nos blocos substituídos
desaparecem com eles (2× `clear_inferred_project_id`, 1×
`clear_inferred_project_id_aggregation`): o parser garante por construção e
por teste (§21.1) que `project_id` só existe com marcador explícito, que é
exatamente a condição que os guards impunham. As **definições** das quatro
funções de guard e de `PROJECT_ID_MARKER_RE` permanecem intocadas em
`main.py`; `clear_plan_inferred_project_id` continua ativo na paginação
(planos armazenados podem previr da era LLM).

Ponte pydantic (única forma autorizada):

```python
ifc_result = ExtractedIfcClass(ifc_class=parsed.ifc_class)
filters_result = ExtractedFilters(
    name=parsed.name, material=list(parsed.materials) or None,
    storey=parsed.storey, project_id=parsed.project_id,
    project_name=parsed.project_name)
conditions_result = ExtractedConditions(
    conditions=[Condition(field=c.field, op=c.op, value=c.value)
                for c in parsed.conditions])
```

Chamadas LLM restantes por pedido (primeiro turno): chat 1, structured 2
(`FILTER_RESULTS_BATCH` + resposta), semantic 3 (+embedding query),
aggregation 1, detail 1 — todas de **resposta/relevância**, nenhuma de
parsing. Com histórico soma-se 1 (`REWRITE_QUERY`).

`SearchPlan`, `ChatResponse`, os dict literais de plano e todos os eventos
existentes mantêm chaves e semântica; `route`/`route_degraded` (HBIM-040)
inalterados. Os modelos `ClassifyResult` e `DetailRef` em `api/search.py`
ficam **como estão** (ficheiro protegido); `DetailRef` deixa de ser importado
por `main.py`.

## 23. Deprecação objetiva de prompts

Após C4, `prompts.py` define exatamente: `REWRITE_QUERY`,
`EXTRACT_EMBEDDING_QUERY`, `FILTER_RESULTS_BATCH`, `FINAL_RESPONSE_FORMAT`,
`DETAIL_RESPONSE_FORMAT`, `AGGREGATION_RESPONSE_FORMAT`. Testes:

1. os identificadores `CLASSIFY_INTENT`, `EXTRACT_IFC_CLASS`,
   `EXTRACT_FILTERS`, `EXTRACT_CONDITIONS`, `EXTRACT_AGGREGATION`,
   `EXTRACT_DETAIL_REF`, `IFC_CLASS_TABLE` **não existem** em
   `api.prompts` (`hasattr` falso para todos) nem como texto em `main.py`;
2. por AST de `main.py`: a contagem de call sites de `get_response` é
   exatamente **7** (rewrite, embedding-extract, chat, resposta detail,
   resposta aggregation, filter-batch, resposta final) — eram **14** antes
   desta issue;
3. `test_router.py::test_classify_intent_is_not_used_by_the_endpoint`
   atualizado (§6) para exigir a ausência também em `prompts.py`;
4. os seis prompts mantidos são **byte-idênticos** aos de `2ff0315`: o diff de
   `prompts.py` contém apenas remoções (nenhuma linha adicionada além de
   eventuais linhas em branco removidas juntas) — impossível esconder
   instruções de parsing num prompt mantido.

## 24. Import-safety e segurança

- §8.1 integral (subprocess fresco + socket-bomb + AST).
- Nenhum valor do utilizador em mensagens de exceção do parser.
- Evento `query_parser` (§27) não contém `raw` nem a query completa.
- Nenhum segredo, host, porta, credencial em código, gold, baseline ou testes.
- `parse_query` não muta o input nem estado global; sem caches mutáveis.

## 25. Determinismo e idempotência

`parse_query(q) == parse_query(q)` (igualdade estrutural) para todo o gold;
1000 repetições estáveis; byte-igual sob `PYTHONHASHSEED ∈ {0,1,7,4242}`
(subprocess); sem dependência de locale, relógio ou ordem de dicionários.

## 26. Compatibilidade e paginação

- Contrato HTTP `/chat` inalterado (mesmos campos, mesma semântica).
- Planos de paginação pré-HBIM-041 continuam a desserializar e a executar sem
  parser. Esta issue **não acrescenta campos** ao `SearchPlan` — a
  serialização de planos é byte-compatível nos dois sentidos.
- `fake_llm` (conftest) inalterado: o caminho chat continua a custar
  exatamente 1 chamada LLM.
- A suite HBIM-040 (`test_router.py`, `test_routing_gold.py`) passa sem
  alterações além da única de §6.

## 27. Observabilidade

Exatamente **um** evento `log_preprocess_json("query_parser", payload)` por
pedido não-paginação, emitido imediatamente após `parse_query`, antes de
qualquer ramificação de estratégia, com exatamente estas chaves:

```
ifc_class, materials, storey, conditions, global_ids_count, agg_field,
name_present, project_id_present, project_name_present, refers_previous,
terms_version
```

- `conditions` é a lista de tripletos `{field, op, value}`;
- `global_ids_count` é `len(global_ids)` — os ids **não** entram no log;
- `name_present`/`project_*_present` são booleanos — os valores livres do
  utilizador **não** entram no log;
- `terms_version` é `PARSER_TERMS_VERSION`.
- O caminho de paginação não emite o evento (teste).
- O caminho detail emite adicionalmente `log_preprocess_json("detail_ref",
  {"index": idx})` (substitui o evento `extract_detail_ref`).

## 28. `parser_gold.jsonl` e baseline legacy congelada

### 28.1 Gold — formato

`backend/eval/dataset/parser_gold.jsonl`, uma linha JSON canónica por caso
(`sort_keys`, `ensure_ascii=False`, separadores `(",", ":")`), ordenado por
`id`, newline final, sem CRLF/BOM:

```json
{"context":{"num_results":null},"expected":{"agg_field":null,"conditions":[],"detail_index":null,"global_ids":[],"ifc_class":"IfcDoor","materials":["madeira"],"name":null,"project_id":null,"project_name":null,"refers_previous":false,"storey":"1"},"id":"par-001","legacy_id":"leg-fil-001","query":"portas de madeira do piso 1"}
```

| Campo | Regra |
|---|---|
| `id` | `^par-\d{3}$`, único |
| `query` | `str` (vazia permitida só nos degenerados) |
| `context.num_results` | `int ≥ 1` ou `null`; obrigatório não-nulo sse `expected.detail_index` não-nulo |
| `expected` | exatamente as 11 chaves acima, tipos do §9 |
| `legacy_id` | `null` ou id existente na baseline §28.2 (`^leg-[a-z]{3}-\d{3}$`) |

**Proveniência e independência.** As labels são curadas manualmente a partir
das regras normativas desta spec (§15–§21) e dos vocabulários committed
(IFC_CLASS_TABLE, HBIM-040 §11.2) — nunca geradas pelo parser. A regra
anti-circularidade é estrutural e testável: o gate §29 tem de **poder falhar**
(teste que corrompe uma predição) e a baseline legacy (§28.2) é transcrição
verbatim de artefactos committed anteriores ao parser.

### 28.2 Baseline legacy congelada

`backend/eval/baselines/legacy_extraction.json` — documento único:

```json
{
  "provenance": {
    "source": "backend/api/prompts.py",
    "source_commit": "2ff0315628b3bf2f756c8a1c5a9b7c0a4e53b76c",
    "method": "transcricao verbatim dos exemplares few-shot dos 5 prompts de extracao",
    "detail_ref_num_results": 5
  },
  "records": [
    {"id": "leg-ifc-001", "prompt": "EXTRACT_IFC_CLASS",
     "query": "mostra-me as portas do piso 1", "fields": {"ifc_class": "IfcDoor"}},
    …
  ]
}
```

- **38 registos**: 8 `EXTRACT_IFC_CLASS`, 9 `EXTRACT_FILTERS` (campos
  `name`,`project_id`,`project_name`; chaves omitidas nos exemplares =
  `null`, semântica pydantic), 6 `EXTRACT_CONDITIONS` (campo `conditions`),
  12 `EXTRACT_AGGREGATION` (campo `agg_field`), 3 `EXTRACT_DETAIL_REF`
  (campo `detail_index`, com `num_results=5` fixado na proveniência).
- **56 pares (input, campo) cobertos**: 8 + 9×3 + 6 + 12 + 3.
- Cada `query` da baseline existe no gold com `legacy_id` a apontar para o
  registo; `fields` ⊆ chaves de `expected`.
- Serialização: `json.dumps(..., ensure_ascii=False, indent=2,
  sort_keys=True)` + newline final. **Byte-stability**: o teste reserializa e
  compara byte a byte, e fixa o SHA-256 do ficheiro como constante no teste —
  regenerá-lo por código de implementação falha o teste.
- Os prompts são removidos neste mesmo commit (C4); a transcrição é auditável
  contra `git show 2ff0315:backend/api/prompts.py` (comando documentado, não
  testado em CI — checkout shallow).

### 28.3 Cobertura mínima do gold

- **≥ 75 casos**; os 38 da baseline incluídos (com `legacy_id`).
- Por campo (casos com o campo não-trivial): `ifc_class` ≥ 12 cobrindo ≥ 10
  classes distintas + ≥ 3 casos `null`; `materials` ≥ 8 (incluindo
  multi-material e plural); `storey` ≥ 8 (padrões 1–6 de §17 todos
  representados); `conditions` ≥ 12 (os 6 operadores todos, vírgula decimal,
  `m2`/`m3`, `cm` ou `mm`, intervalo, multi-condição, ordem de aparição);
  `global_ids` ≥ 5 (0, 1 e ≥2 ids; ids sintéticos das fixtures canónicas);
  `agg_field` ≥ 10 (os 7 valores todos); `detail_index` ≥ 5 (ordinal, `o N`,
  `ultimo`, default, com `num_results` variado); `name`/`project_*` ≥ 6;
  `refers_previous` ≥ 4 (2 true, 2 false).
- **Adversariais ≥ 10:** `portanto`/`lajedo`/`madeirense`/`contemplar`
  (substrings que não disparam); `"mais de 3"` sem unidade (sem condição);
  `"1.000 metros"` (fronteira documentada → 1.0); métrica não suportada
  (`comprimento`) sem condição; unidade×métrica incompatível descartada;
  `"piso 1"` sem condição numérica; GlobalId colado a token (não extraído);
  query vazia; só pontuação; query longa (≥ 5 000 chars) terminando.

## 29. Métricas e gates (exatos)

Sejam `G` o gold, `L` a baseline (§28.2), `P(q)` = `parse_query(q).to_dict()`
mais `detail_index = parse_detail_ref(q, num_results)` quando
`context.num_results` não é nulo (senão `detail_index = null`). O scoring usa
**exatamente as 11 chaves de `expected`**; `raw` nunca é pontuado.

**Igualdade por campo** `match(a, b)`: igualdade estrutural exata
(`None == None` conta como match; listas comparadas por ordem; floats por
igualdade exata — os valores do gold são os produtos exatos do parsing).
Campos em falta **não existem**: `P` emite sempre as 11 chaves (dataclass) e
`expected` tem sempre as 11 chaves (schema §28.1); um campo extra ou em falta
em qualquer dos lados falha o teste de schema antes de qualquer métrica.

1. `parser_field_accuracy(campo)` = média de `match(P(q)[campo],
   expected[campo])` sobre os casos de `G` (micro, todos os casos contam
   para todos os campos — um falso positivo num campo que devia ser `null`
   é penalizado).
2. `parser_full_record` = média de `∀campo: match` sobre `G`.
3. **Pares cobertos** `C` = {(q, campo) : registo legacy de q cobre campo}:
   `legacy_covered = média de match(L[q][campo], expected[campo]) sobre C`
   `parser_covered = média de match(P(q)[campo], expected[campo]) sobre C`

**Gates (todos offline, sem Docker, sem marker):**

- **G1 (paridade/melhoria, ROADMAP §839):** `parser_covered ≥ legacy_covered`.
- **G2 (qualidade absoluta):** `parser_full_record ≥ 0.95`.
- **G3 (sem regressão catastrófica por campo):** para cada um dos 11 campos,
  `parser_field_accuracy ≥ 0.90`.
- **G4 (anti-tautologia):** um teste corrompe deliberadamente uma predição
  coberta e prova `parser_covered' < legacy_covered` ⇒ G1 falharia; outro
  corrompe uma predição qualquer e prova `parser_full_record' < 0.95` com um
  gold sintético pequeno ⇒ G2 falharia. As funções de scoring são elas
  próprias testadas com pares certos/errados construídos à mão (o scorer
  penaliza campo errado, campo extra tornado não-nulo e lista desordenada).
- Relatório do teste de gate: contagens, os três números, delta
  `parser_covered − legacy_covered` e lista de misses `(id, campo, esperado,
  obtido)` na mensagem de asserção.

As funções de scoring vivem em `tests/test_parser_gold.py` como funções puras
sem I/O (não em `eval/metrics.py`, que fica protegido — autoridade §2, ponto 5).

## 30. Testes unitários normativos

`backend/tests/test_query_parser.py` (offline, sem rede, sem Docker, sem ML,
sem relógio; sem `importlib.reload` de módulos `retrieval.*`):

1. dicionário IFC: golden dos 100 pares (93 chaves normalizadas) + 21
   literais; primeira-posição; empate→mais longo; `IfcWall` vs
   `fachada cortina` vs `pipe segment`; plural/singular; acentos
   (`corrimão`≡`corrimao`); `None` sem termo.
2. materiais: canónicos, plural→singular, ordenação, dedup, fronteira
   (`madeirense` não dispara), multi-material.
3. storey: os 6 padrões de §17, incluindo `1º`/`1.º` (NFKD `º`→`o`),
   `r/c`, `rés-do-chão`, `terreo`, `cave`, `piso -1`, `nivel l0`→`L0`,
   ordinais, ausência → `None`, `"piso principal"` → `None`.
4. condições: G1–G6 todos; 6 operadores; vírgula/ponto; `m²`/`m³` NFKD;
   `cm`/`mm` convertidos; default-height; `de espessura` explícito;
   intervalo com extremos invertidos normalizado; dimensão incompatível
   descartada; `mais de 3` sem condição; multi-condição em ordem de
   aparição; dedup; `value` nunca `bool`; sempre finito.
5. `global_ids`: 0/1/vários; ordem; dedup-primeiro; caso preservado
   byte a byte; regex é **o mesmo objeto** que `router.GLOBAL_ID_RE` (`is`).
6. `agg_field`: as 9 regras de §20 na ordem; os 12 exemplares a1–a12;
   `None` sem sinais; vocabulário ⊆ `{"count"} ∪ AGG_FIELD_MAP` (importado de
   `api.search` no teste).
7. `name`/`project_id`/`project_name`: f1–f9 todos; aspas; identificador com
   underscore; exclusão de spans; marcador explícito; consistência com
   `user_explicitly_mentions_project_id` sobre o gold inteiro.
8. `refers_previous`: consistência com o router sobre o gold inteiro (§10).
9. `parse_detail_ref`: d1–d3; ordinais 1–10 por extenso; `o N`; `o 2º`
   (NFKD `º`→`o`, forma `2o`); `numero N`; `ultimo`; default 1; clamp;
   `num_results=1`; `TypeError` (incl. `bool`); `ValueError` para
   `num_results < 1`.
10. totalidade/pureza: vazio, espaços, pontuação, emoji, 10 000 chars,
    não-latim; `TypeError` não ecoa input; frozen dataclasses; `to_dict`
    com exatamente as 11 chaves + `raw`.
11. import-safety §8.1 (subprocess forbidden-modules + socket-bomb + AST sem
    `re.compile` com `{22}` + sem `unicodedata` próprio) **e superfície
    pública exata**: os exports de `retrieval.query_parser` e de
    `retrieval.__init__` são exatamente os de §8.
12. determinismo §25 (igualdade repetida + `PYTHONHASHSEED` em subprocess).
13. ponte: `api.search.Condition(field=c.field, op=c.op, value=c.value)`
    valida para todo o `NumericCondition` produzido a partir do gold; o
    vocabulário emitido pelo parser é exatamente
    `{height, area, volume, thickness}` × `{eq, approx, gt, gte, lt, lte}`
    (constantes literais no teste — o vocabulário que
    `build_opensearch_query` já consome).
14. endpoint (fixture `chat` própria, espelhando `test_router.py`, com
    comportamento **pinado**: `execute_search` devolve exatamente **um** hit
    sintético com total 1; a resposta JSON falsa inclui
    `"relevant_indices": [1]`; `execute_aggregation` devolve `([], 0)`;
    `fetch_by_id` devolve um documento sintético; `get_query_embedding`
    devolve `[0.0]`):
    - **zero LLM de parsing por caminho** com contagens exatas de chamadas
      `get_response`: chat=1, structured=2 (filter batch + resposta),
      aggregation=1, detail=1, semantic=3 (embedding + filter + resposta);
      +1 em qualquer caminho quando há histórico (rewrite);
    - **bomba de parsing**: a fixture explode se receber uma chamada com
      `response_format` JSON cujo prompt não contenha um dos dois marcadores
      dos prompts JSON mantidos — `"relevant_indices"`
      (`FILTER_RESULTS_BATCH`) ou `"embedding_query"`
      (`EXTRACT_EMBEDDING_QUERY`); qualquer extração LLM residual em qualquer
      caminho rebenta a fixture;
    - `parse_query` é chamado com `effective_query` verbatim (spy);
    - evento `query_parser` único, com exatamente as chaves de §27, sem a
      query e sem GlobalIds; emitido também quando o plano é `None`;
    - `detail_ref` event no caminho detail;
    - paginação: parser **não** chamado, evento ausente, plano armazenado
      executa;
    - o fluxo detail usa `parse_detail_ref` com `len(result_ids)`;
    - `internal_error_response` explode na fixture (nenhum teste passa por
      um 500 engolido — lição HBIM-040 F3).
15. deprecação §23 (hasattr, AST, contagem de call sites).
16. `PARSER_TERMS_VERSION == "1"`.

## 31. Testes de gold e baseline

`backend/tests/test_parser_gold.py` (offline):

1. schema §28.1 (chaves exatas, tipos, ids únicos e ordenados, byte-stability
   canónica, newline final, sem CRLF/BOM).
2. baseline §28.2: schema, 38 registos, 56 pares, `legacy_id` bijetivo com os
   registos, byte-stability + SHA-256 fixado, `fields` ⊆ chaves de expected,
   proveniência presente com o commit exato.
3. cobertura §28.3 asserida numericamente.
4. gates G1–G3 com relatório; G4 anti-tautologia; testes do scorer.
5. isolamento HBIM-005: `load_and_validate` continua a passar com
   `parser_gold.jsonl` presente; `dataset.json` byte-idêntico; a suite de
   `current_system.json` não é afetada por `legacy_extraction.json`.
6. sem dados sensíveis no gold/baseline (sem `/home/`, `/mnt/`, `http`,
   `password`, ids IFC reais — só os sintéticos das fixtures).
7. consistência de vocabulário: todo o `expected.agg_field` não-nulo do gold
   pertence a `AGG_FIELDS`; todo o `agg_field` devolvido pelo parser sobre as
   queries do gold pertence a `AGG_FIELDS ∪ {None}`.

## 32. Testes adversariais obrigatórios

Nos ficheiros acima, nomeados:

1. substring traps §28.3 (quatro palavras) — nenhum campo dispara;
2. `"1.000 metros"` → `height approx 1.0` (fronteira documentada, pinada);
3. métrica não suportada nunca vira condição; unidade incompatível descartada;
4. dois GlobalIds na mesma query: ordem, caso, dedup;
5. `"quantas portas ha no piso 1?"` → `agg_field="count"` **e** `storey="1"`
   **e** `ifc_class="IfcDoor"` simultâneos;
6. `ParsedQuery.raw` é o input **verbatim** (igualdade com o argumento
   original, incluindo maiúsculas, acentos e pontuação);
7. `parse_query` chamado duas vezes → objetos iguais (e `to_dict` igual);
8. a bomba de parsing de §30.14 corre em **todos** os caminhos (chat,
   structured, semantic, aggregation, detail, paginação) sem explodir;
9. paginação com plano legacy serializado antes de HBIM-041 → executa sem
   parser e sem evento;
10. `PYTHONHASHSEED` ∈ {0,1,7,4242}: output byte-igual (subprocess);
11. gate G1 falha com uma predição corrompida (G4);
12. query com `entre 5 e 3 metros` → `gte 3.0` + `lte 5.0` (normalizado).

## 33. Anti-tautologia (resumo vinculativo)

- G4 (§29) prova que G1 e G2 podem falhar.
- O scorer é testado com casos errados construídos à mão (§29).
- A baseline tem SHA-256 fixado no teste; regenerá-la por código falha.
- As labels do gold nunca são produzidas pelo parser (§28.1); a auditoria é a
  revisão do diff + o facto de os 38 casos legacy serem transcrições.

## 34. Regression gates completos

Verdes e byte-idênticos onde aplicável:

- suites HBIM-005/010/011/012/020/021/022/040 completas, sem alterações além
  da única de §6;
- `test_router.py` + `test_routing_gold.py` (166 testes) verdes;
- suite unitária completa em ≥ 3 ordens (default, seed=1, +4 seeds, no:randomly);
- suite de integração (Docker local efémero);
- baseline HBIM-005 byte-idêntica (`current_system.json`, prefixo
  `7bf3c8d7200f0512`);
- Ruff limpo; mypy bloqueante verde incluindo `retrieval.query_parser`;
- `git diff --check` limpo; ficheiros §7 byte-idênticos (SHA-256);
- nenhum job CI novo.

## 35. Critérios de aceitação

1. `retrieval/query_parser.py` existe e exporta exatamente §8. *(30.11, 30.16)*
2. Os sete call sites LLM de parsing desapareceram de `main.py`; a contagem
   de `get_response` call sites é 7. *(30.15, AST)*
3. Zero chamadas LLM de parsing em todos os caminhos, provado por fixture que
   explode e por contagens exatas. *(30.14, 32.8)*
4. `prompts.py` já não define os 7 identificadores de C4. *(30.15)*
5. `parse_query` é pura, total, determinística, tipada; `TypeError`/`ValueError`
   conforme §9. *(30.9, 30.10, 30.12)*
6. Vocabulário IFC = tabela legacy migrada sem perdas (100 pares → 93 chaves
   + 21 literais). *(30.1)*
7. Condições: gramática §18 completa com conversões e descartes. *(30.4)*
8. GlobalId: mesmo objeto regex de HBIM-040; ordem/caso/dedup. *(30.5)*
9. `agg_field`: §20 com os 12 exemplares. *(30.6)*
10. `name`/`project_*`: §21 com os 9 exemplares e consistência com o guard.
    *(30.7)*
11. `refers_previous` consistente com o router em todo o gold. *(30.8)*
12. `parse_detail_ref`: §9 + d1–d3. *(30.9)*
13. Gold ≥ 75 casos com cobertura §28.3, canónico e byte-estável. *(31.1, 31.3)*
14. Baseline: 38 registos/56 pares, SHA-256 fixado, proveniência exata. *(31.2)*
15. **G1 paridade/melhoria** verde e reportado com delta. *(31.4)*
16. G2 ≥ 0.95 e G3 ≥ 0.90 verdes. *(31.4)*
17. G4 prova que os gates falham. *(31.4, 32.11)*
18. HBIM-005 isolado (gold extra inerte; `dataset.json` intacto). *(31.5)*
19. Evento `query_parser` com exatamente as chaves §27; sem query nem ids;
    paginação sem evento e sem parser. *(30.14, 32.9)*
20. Router intocado em comportamento: suite HBIM-040 verde; diff de
    `router.py` só linhas aditivas. *(§12, 34)*
21. `api/search.py` byte-idêntico. *(SHA-256)*
22. Nenhuma alteração de queries OpenSearch, mappings, indexers (HBIM-042
    não implementado): `build_opensearch_query`/`build_aggregation_query`
    byte-idênticos dentro de `search.py` protegido. *(21, git diff)*
23. Suite completa verde nas ordens exigidas; integração verde; Ruff/mypy
    verdes; `git diff --check` limpo. *(34)*
24. Nenhum ficheiro fora de §6; protegidos §7 intactos. *(git status + SHA)*
25. Nenhuma decisão pendente; spec sem contradições. *(inspeção)*

## 36. Comandos de validação

```bash
# WSL, conda hbim-rag, a partir da raiz do repo
conda run -n hbim-rag python -m pytest \
  backend/tests/test_query_parser.py backend/tests/test_parser_gold.py \
  -q -o addopts=""

conda run -n hbim-rag python -m pytest backend/tests/test_query_parser.py \
  backend/tests/test_parser_gold.py -q -o addopts="" --randomly-seed=1
# + seeds 2, 3, 7, 99 e -p no:randomly

conda run -n hbim-rag python -m pytest \
  backend/tests/test_router.py backend/tests/test_routing_gold.py -q -o addopts=""

conda run -n hbim-rag python -m pytest backend/tests -m "not integration" -q -o addopts=""
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -p no:randomly
conda run -n hbim-rag python -m pytest backend/tests -m integration -q -o addopts=""
conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_eval_baseline.py -m integration -q -o addopts=""

conda run -n hbim-rag python -m ruff check backend
# mypy: comando explícito do ci.yml já atualizado (inclui query_parser.py)

grep -n "EXTRACT_IFC_CLASS\|EXTRACT_FILTERS\|EXTRACT_CONDITIONS\|EXTRACT_AGGREGATION\|EXTRACT_DETAIL_REF\|CLASSIFY_INTENT\|IFC_CLASS_TABLE" backend/api/main.py backend/api/prompts.py
# deve devolver zero linhas

# auditoria manual da baseline (fora de CI):
git show 2ff0315:backend/api/prompts.py | less

sha256sum backend/api/search.py backend/eval/baselines/current_system.json \
          backend/eval/dataset/dataset.json backend/eval/dataset/routing_gold.jsonl \
          backend/eval/baselines/legacy_extraction.json

git status --short --untracked-files=all
git --no-pager diff --name-status
git diff --check
```

## 37. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Regex/dicionários não cobrem fraseados raros | Campos ficam `None`/vazios — o fallback semântico do router cobre; gold adversarial pina as fronteiras; `PARSER_TERMS_VERSION` versiona |
| Gold auto-confirmatório | Labels curadas das regras normativas; 38/75+ casos são transcrições legacy committed; G4 prova falhabilidade; scorer auto-testado |
| Baseline fabricada | Transcrição verbatim com proveniência `2ff0315` + SHA-256 fixado + comando de auditoria manual |
| Divergência parser↔router (normalização, GlobalId, previous) | Reutilização por alias público (mesmo objeto, teste `is`); testes de consistência sobre o gold inteiro |
| `main.py` com LLM de parsing residual num caminho raro | Fixture-bomba §32.8 corre todos os caminhos; AST conta call sites |
| Quebra do caminho semântico (embedding query) | `EXTRACT_EMBEDDING_QUERY` intocado; ponte pydantic mantém a assinatura; teste de contagem semantic=3 |
| Ambiguidade storey vs contagem vs condição | Regras disjuntas por construção (§17 exige palavra-chave de piso; §18 exige unidade/métrica/operador); testes adversariais 32.5 |
| `1.000` lido como 1.0 | Fronteira documentada e pinada em teste (32.2); separadores de milhares ficam fora do vocabulário v1 |
| Follow-ups elípticos degradam | Idêntico ao legacy: o parser recebe a mesma `effective_query` reescrita que os extratores recebiam (C6) — sem regressão estrutural |

## 38. Adiado deliberadamente (HBIM-042+)

- Aplicar `material`/`storey`/`name`/`project_name` em
  `build_opensearch_query` (lexical.py).
- Corrigir agregação de classificação (`classification_codes` keyword).
- Migração API para aliases `hbim_*` (gap HBIM-023, não alargado aqui).
- Intervalos de piso, separadores de milhares, mais métricas/unidades,
  nomes livres sem aspas — exigem gold novo e bump de
  `PARSER_TERMS_VERSION`.
- Remoção de `ClassifyResult`/`DetailRef`/`Extracted*` de `api/search.py`
  (ficheiro protegido aqui; limpeza pertence a HBIM-042, que o vai editar).

## 39. Entregáveis

**Criar:** `backend/retrieval/query_parser.py`,
`backend/tests/test_query_parser.py`, `backend/tests/test_parser_gold.py`,
`backend/eval/dataset/parser_gold.jsonl`,
`backend/eval/baselines/legacy_extraction.json`, esta spec.

**Modificar:** `backend/api/main.py`, `backend/api/prompts.py`,
`backend/retrieval/router.py` (aditivo §12), `backend/retrieval/__init__.py`,
`backend/tests/test_router.py` (só §6), `pyproject.toml`,
`.github/workflows/ci.yml`, `docs/development/LOCAL_SETUP.md`,
`docs/implementation/IMPLEMENTATION_STATUS.md` (no fim).

**Dois commits:** `docs: specify HBIM-041 deterministic query parser` (só a
spec) e `feat: implement HBIM-041 deterministic query parser` (todo o resto).

**Relatório final** no formato do `CLAUDE.md`, com `Self-review findings` e
cada critério de §35 avaliado `PASS`/`FAIL`/`PARTIAL` com evidência.
