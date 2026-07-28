# Local development setup (HBIM-004)

Ambiente de referência: **WSL2 (filesystem Linux)**, conda env **`hbim-rag`**
(Python 3.10). Nota de reconciliação: o README histórico referia um env conda
`bim_data` criado a partir de `backend/environment.yml`; esse ficheiro já não
existe e o ambiente operativo é `hbim-rag`. Todos os comandos Python correm
via `conda run -n hbim-rag`.

## Dependências

```bash
# Runtime (não-ML) + tooling de desenvolvimento — suficiente para toda a suite
~/miniconda3/bin/conda run -n hbim-rag python -m pip install \
  -r backend/requirements.txt -r backend/requirements-dev.txt

# Stack ML (embeddings; multi-GB) — apenas para indexação/rota semântica
~/miniconda3/bin/conda run -n hbim-rag python -m pip install \
  -r backend/requirements-ml.txt
```

Os jobs de CI unit, Ruff, mypy e integração **não** instalam
`requirements-ml.txt`.

## Testes

Testes sem marker são tratados como **unitários**. Integração é **opt-in**
(o default `addopts = -m "not integration"` vive no `pyproject.toml`).

```bash
# Unit (inclui os testes sem marker; exclui integração; sem Docker)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"

# Integração (exige Docker local; container OpenSearch efémero)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -m integration

# Suite completa (unmarked + unit + integration)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -o addopts=""
```

A ordem dos testes é aleatória por defeito (`pytest-randomly`; a seed é
impressa e reproduzível com `--randomly-seed=N`). A guarda de rede nunca é
desativada: testes unit não podem abrir sockets de rede; testes integration
só podem contactar loopback.

## Qualidade

```bash
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend

# Gate bloqueante do mypy (módulos tipados de HBIM-002/003 + backend/eval de HBIM-005)
~/miniconda3/bin/conda run -n hbim-rag python -m mypy \
  backend/shared/config.py backend/shared/opensearch.py \
  backend/shared/security.py backend/shared/logging.py \
  backend/api/health.py backend/api/metrics.py \
  backend/api/middleware.py backend/api/errors.py \
  backend/eval/dataset.py backend/eval/metrics.py backend/eval/run_eval.py

# Scan informativo (não bloqueante) do resto do backend
~/miniconda3/bin/conda run -n hbim-rag python -m mypy backend
```

## Avaliação (HBIM-005)

Baseline determinístico do comportamento de retrieval atual, contra um
OpenSearch local real, com dados sintéticos versionados e vetores fixos de 40
dimensões (sem modelo, sem inferência, sem downloads). O runner **não** inicia
containers; recusa hosts não-loopback; nunca lê `.env`.

```bash
# Testes unitários das métricas / dataset / relatório (offline)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_eval_metrics.py backend/tests/test_eval_dataset.py \
  backend/tests/test_eval_report.py -q

# Integração real (Testcontainers efémero, gates absolutos + determinismo)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration

# Runner end-to-end contra o serviço Compose local (loopback)
docker compose -f docker-compose.dev.yml up -d --wait opensearch
~/miniconda3/bin/conda run -n hbim-rag python -m eval.run_eval run \
  --opensearch-host 127.0.0.1 --opensearch-port 9200 \
  --dataset backend/eval/dataset --report-dir backend/eval/reports --runs 2 \
  --save-baseline backend/eval/baselines/current_system.json
docker compose -f docker-compose.dev.yml down
```

Fluxo do baseline: a **primeira** baseline é gerada localmente com Docker
(comando acima, `--save-baseline`), **revista por uma pessoa** e só então
**committed** (`backend/eval/baselines/current_system.json`). O CI nunca cria
nem aprova a baseline — apenas reproduz, compara (`--compare-baseline`) e
publica o relatório. Alterar a baseline exige, no mesmo changeset: a alteração
funcional documentada, o diff do relatório revisto, justificação no PR e a
baseline atualizada. Os relatórios de execução (`backend/eval/reports/`,
`report.json` + `report.md`) são git-ignored; apenas o dataset e a baseline
aprovada são versionados.

## Schema canónico (HBIM-010)

`backend/canonical/` define o contrato de dados HBIM — a representação
intermédia (IR) tipada, versionada e determinística entre a extração IFC e os
consumidores futuros (indexers, grafo, documentos, retrieval). É **apenas o
contrato**: `schema.py` (modelos Pydantic v2 estritos), `ids.py` (IDs
determinísticos SHA-256+netstring), `serialization.py` (JSON/JSONL canónico
byte-stable). Não importa OpenSearch, FastAPI, settings nem lê `.env`; a
conversão IFC→canónico pertence à HBIM-011/012.

```bash
# Testes canonical (offline, sem Docker)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_canonical_schema.py backend/tests/test_canonical_ids.py \
  backend/tests/test_canonical_serialization.py backend/tests/test_canonical_import_safety.py \
  -q -o addopts=""

# Qualidade (canonical está no gate bloqueante do mypy e no Ruff)
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend/canonical
~/miniconda3/bin/conda run -n hbim-rag python -m mypy \
  backend/canonical/schema.py backend/canonical/ids.py backend/canonical/serialization.py
```

Fixtures e cobertura: `backend/tests/fixtures/canonical/*.jsonl` são golden files
**sintéticos e anónimos**, serializados pela forma canónica (byte-stable, newline
final, sem timestamps); `coverage_manifest.json` classifica cada categoria de
entidade/valor observada na auditoria como `supported`/`planned_atomization`/
`unsupported_v1`. Os testes de serialização validam que os golden files coincidem
byte-a-byte com a re-serialização canónica.

### Política de IFCs (`local_data/`)

IFCs reais, grandes ou potencialmente confidenciais vivem em `local_data/ifc/`,
que está **git-ignored** (`.gitignore`). Estes ficheiros **nunca** são
committed, copiados para fixtures nem incluídos em patches, relatórios ou
documentação. As fixtures canónicas são exclusivamente sintéticas; **é proibido
committar qualquer IFC real** (o CI não depende de `local_data/`).

## Extração canónica IFC (HBIM-011)

`backend/ingestion/canonical_ifc.py` (+ `ifc_spatial.py`, `ifc_materials.py`,
`ifc_values.py`) converte um IFC em records canónicos (HBIM-010) e escreve JSONL
determinístico, publicado **atomicamente por diretório**. A lógica dependente de
IfcOpenShell vive em `ingestion/`; `backend/canonical` permanece livre de
IfcOpenShell. Não importa OpenSearch/FastAPI/settings, não lê `.env`, não abre
rede.

```bash
# Testes HBIM-011 (offline, sem Docker; IFC sintético em tmp_path)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_ifc_values.py backend/tests/test_ifc_spatial.py \
  backend/tests/test_ifc_materials.py backend/tests/test_canonical_ifc.py \
  backend/tests/test_canonical_ifc_import_safety.py -q -o addopts=""

# Qualidade (os 4 módulos estão no gate bloqueante do mypy e no Ruff)
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend/ingestion
~/miniconda3/bin/conda run -n hbim-rag python -m mypy \
  backend/ingestion/canonical_ifc.py backend/ingestion/ifc_spatial.py \
  backend/ingestion/ifc_materials.py backend/ingestion/ifc_values.py
```

**Validação local (fora do CI)** contra as amostras privadas em `local_data/ifc/`,
por CLI, com `output_dir` que **não pode pré-existir**. O modo `--summary` imprime
**apenas** contagens/categorias/códigos de warning — nunca nomes, paths ou
conteúdo do IFC:

```bash
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.canonical_ifc \
  --source <path-para-o-ifc> --project-id <id> --source-id <id> \
  --output-dir <dir-novo> --summary
```

As fixtures canónicas de extração (`backend/tests/fixtures/canonical/ifc_extraction/`)
são golden **sintéticos** gerados pelos builders válidos (`ifc_builder.py`,
`build_valid_ifc4`); **nenhum `.ifc` é committed**.

## Atomização de PropertyFact (HBIM-012)

O produtor de `PropertyFact` deixou de usar `get_psets`: `ingestion/ifc_properties.py`
faz o **traversal raw** dos property/quantity sets (instância + tipo) e constrói uma
união tipada fechada de ocorrências; `ingestion/property_facts.py` é **puro (sem
IfcOpenShell)** e atomiza-a em `PropertyFact` v1.0 (enum/list/bounded/table/complex/
physical-complex-quantity), com gramática fechada de `occurrence_key` (caminhos
complexos por netstring), precedência instância>tipo, deduplicação, conflitos
fail-closed e limites de explosão. As **métricas** mantêm o caminho `get_psets`
(heurístico, nunca produz `PropertyFact`). O `backend/canonical` (schema v1.0) não é
alterado.

```bash
# Suite pura de atomização (sem IfcOpenShell) + traversal + integração
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_property_facts.py backend/tests/test_ifc_properties.py \
  backend/tests/test_canonical_ifc.py -q -o addopts=""

~/miniconda3/bin/conda run -n hbim-rag python -m mypy \
  backend/ingestion/ifc_properties.py backend/ingestion/property_facts.py
```

## Mapeamentos estáticos de índice OpenSearch (HBIM-020)

`backend/canonical/mappings/*.json` definem os mappings **estáticos, versionados
e `dynamic: strict`** dos quatro records canónicos (`elements_v1`,
`property_facts_v1`, `classification_facts_v1`, `documents_v1`): sem mapping
explosion, sem vetores, sem aliases, sem criação de índices e sem `settings`
operacionais (esses são HBIM-021). São **dados** (JSON) — não há loader nem
`__init__.py`; o primeiro consumidor é a HBIM-021. O `PropertyFact.value`
polimórfico é mapeado como projeção tipada e disjunta (`value_type`/`value_is_null`/
`value_text`/`value_integer`/`value_number`/`value_boolean`); a projeção em si
pertence à HBIM-022.

```bash
# Validação offline dos mappings (sem Docker; lê os JSON com json+pathlib,
# cobertura de campos por model_fields, byte-stability golden)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_index_mappings.py -q -o addopts=""

# Aplicação real contra OpenSearch efémero (exige Docker local; Testcontainers
# 2.19.1): index/get, term/full-text/range/nested/agg, rejeição de campos
# desconhecidos e de coerção, prova anti-mapping-explosion
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_index_mappings_apply.py -m integration -q -o addopts=""
```

**`--dry-run`:** `create` e `create-all --dry-run` produzem um plano **puramente
local** (record type, alias, índice físico, versão, mapping, settings) — **não**
constroem cliente nem exigem `OPENSEARCH_PASSWORD`, e imprimem que o estado
remoto **não foi consultado**. Já `promote`/`rollback`/`promote-all`/`rollback-all`
com `--dry-run` **constroem cliente** e fazem um preflight **read-only** (sem
mutação), porque o plano de alias depende do estado atual dos aliases/índices.

Pré-requisito do teste de integração: **Docker local** (mesma deteção WSL da
secção "Nota WSL + Docker Desktop"); sem Docker faz **skip** com razão explícita,
e em CI (`HBIM_REQUIRE_DOCKER=1`) é falha dura. Reutiliza o job existente
`integration-opensearch` — sem job novo.

## Lifecycle de índices e migração por alias (HBIM-021)

`backend/ingestion/index_lifecycle.py` implementa o lifecycle **não destrutivo**
dos quatro índices HBIM-020 (registry fixo `element`/`property_fact`/
`classification_fact`/`document` → aliases `hbim_elements`/`hbim_property_facts`/
`hbim_classification_facts`/`hbim_documents`): loader dos mappings (só `json`+
`pathlib`), settings operacionais mínimos (1 shard, 0 réplicas,
`mapping.total_fields.limit=1000`, **sem vetores**), comparação **recursiva** de
compatibilidade, criação **idempotente** (nunca apaga), promoção/rollback
**atómicos** (uma só chamada `update_aliases`) e status determinístico. Recebe
sempre um cliente OpenSearch **injetado**; nada é criado no import. A CLI fina
`backend/ingestion/migrate.py` constrói o cliente em runtime.

O `create_index` legacy (`index_to_opensearch.py`) passou a ser
**create-if-absent**: se o índice já existir, retorna sem apagar nem recriar
(nenhum `indices.delete` automático). A API/retrieval continuam a usar
`bim_elements`; os novos aliases ainda **não** são consumidos (HBIM-022).

```bash
# CLI: status; create nao exige confirmacao; promote/rollback exigem --yes
# (--dry-run mostra o plano sem mutar)
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.migrate status --json
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.migrate create-all --physical-version 1
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.migrate promote-all --physical-version 1 --yes

# Testes offline dos lifecycle (sem Docker; cliente OpenSearch em memoria)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_index_lifecycle.py -q -o addopts=""

# Integracao real (exige Docker; OpenSearch efemero 2.19.1, loopback)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_index_lifecycle_apply.py -m integration -q -o addopts=""
```

## Indexers canónicos e projeção de PropertyFact (HBIM-022)

`backend/ingestion/indexers/` lê os quatro JSONL canónicos produzidos pela
HBIM-011/012 (`elements.jsonl`, `property_facts.jsonl`,
`classification_facts.jsonl`, `documents.jsonl`) **em streaming**, valida cada
linha com o modelo canónico, projeta-a para o mapping HBIM-020 e indexa-a
**diretamente no índice físico** composto pelo registry HBIM-021. O `_id` é o
campo de identidade do próprio record, **verbatim** (`element_id`/`fact_id`/
`classification_id`/`document_id`) — nunca recomputado nem concatenado com
`project_id`.

O `PropertyFact.value` polimórfico **nunca** chega ao OpenSearch: é projetado na
forma tipada e disjunta da HBIM-020 §5 (`value_type` e `value_is_null` sempre
presentes; exatamente um de `value_text`/`value_integer`/`value_number`/
`value_boolean` para valores não-null; zero payloads para `null`).

**Zero ML.** Nenhum modelo, embedding, vetor ou kNN é carregado — importar
qualquer módulo do pacote não puxa `shared.config`, `shared.opensearch`,
`dotenv`, `ifcopenshell`, `torch` nem `sentence_transformers`; o cliente
OpenSearch é construído **apenas** no caminho runtime da CLI.

### Fluxo operacional: create → index → promote

A indexação acontece **sempre** num índice físico ainda não promovido; a
promoção é um ato separado e explícito da CLI HBIM-021.

```bash
# 1. criar os quatro índices físicos (HBIM-021)
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.migrate create-all --physical-version 1

# 2. validar o input localmente (nunca constrói cliente, nunca lê settings)
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.indexers validate --input-dir <dir-canónico> --json

# 3. plano local, sem tocar no estado remoto
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.indexers index --input-dir <dir-canónico> \
  --physical-version 1 --dry-run

# 4. indexar os quatro record types
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.indexers index --input-dir <dir-canónico> --physical-version 1

# 4b. (alternativa) indexar um único record type
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.indexers index-one --input-dir <dir-canónico> \
  --record-type property_fact --physical-version 1

# 5. só depois de a verificação passar, promover os aliases (HBIM-021)
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.migrate promote-all --physical-version 1 --yes
```

Opções de `index`/`index-one`: `--batch-size` (default 500), `--request-timeout`
(default 60), `--require-empty` (exige o target vazio no preflight),
`--dry-run`, `--json`. **Não existem** `--max-failures`, `--allow-duplicate-ids`
nem `--index-name`: o nome físico é sempre composto por
`index_lifecycle.physical_index_name(record_type, physical_version)`.

### ⚠ Target live

Por defeito, o indexer **recusa** escrever num índice físico que seja, nesse
momento, alvo do alias correspondente (`LiveTargetError`, exit 1) — escrever aí
é imediatamente visível para os consumidores do alias. Para uma correção
deliberada são exigidos **os dois** flags juntos:

```bash
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag \
  python -m ingestion.indexers index --input-dir <dir> --physical-version 1 \
  --allow-live-target --yes
```

Um flag sem o outro é erro de utilização (**exit 2**), detetado antes de
qualquer cliente. Qualquer **conflito de alias** (múltiplos targets, colisão
alias/índice concreto) é recusado fail-closed com `TargetIndexError` e **zero
escrita em qualquer target** — `alias_missing` (alias ainda não promovido) não é
conflito.

### Garantias operacionais

- **Duas passagens com digest**: a validação local dos quatro inputs e os
  preflights remotos dos quatro targets acontecem **antes** da primeira ação
  bulk; os digests SHA-256 são reconfirmados antes da primeira escrita, antes do
  bulk de cada record type e no fim de cada leitura. Um input inválido — ou
  alterado entre a validação e a escrita — produz **zero escrita remota**.
- **Idempotência**: `_op_type=index` com `_id` canónico ⇒ um rerun substitui os
  mesmos documentos e converge; uma execução interrompida é recuperável.
- **Nunca destrutivo**: o indexer não cria, apaga nem promove índices ou
  aliases. Documentos extra no target são **detetados** (`VerificationError`),
  nunca apagados; a remediação é criar uma nova versão física e reindexar.
- **Exit codes**: `0` sucesso; `1` falha operacional (input, validação,
  projeção, duplicados, target, alias, live target, bulk, interrupção,
  verificação, OpenSearch); `2` argparse/configuração/flags inválidos.
- **`--json`**: stdout contém exatamente um documento JSON
  (`{"reports": [...], "error": ...}`) em qualquer caminho; o texto humano vai
  para stderr e nunca há traceback.

```bash
# Testes offline (sem Docker, sem rede, sem ML, sem sleeps reais)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_canonical_indexers.py -q -o addopts=""

# Integração real (exige Docker; OpenSearch efémero 2.19.1, loopback)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_canonical_indexers_apply.py \
  -m integration -q -o addopts=""

# Qualidade (os sete módulos novos estão no gate bloqueante do mypy e no Ruff)
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend
```

**Docker é necessário apenas para a integração.** As fixtures sintéticas de
indexação vivem em `backend/tests/fixtures/canonical/indexing/`; os goldens
canónicos da HBIM-010 não são alterados.

## Router determinístico (HBIM-040)

`backend/retrieval/router.py` substitui a classificação `CLASSIFY_INTENT` feita
por LLM no `/chat`. É uma função **pura, total e determinística**
(`route(query, context) -> RoutingDecision`) que importa **apenas a biblioteca
padrão**: importá-la não puxa `shared.config`, `shared.opensearch`, `dotenv`,
`openai`, `opensearchpy`, `fastapi`, `pydantic`, `torch` nem
`sentence_transformers`, não lê o relógio nem uma fonte aleatória, não abre
ficheiros e não cria sockets.

- **Oito rotas** (`Route`): `exact_lookup`, `aggregation`, `structured`,
  `graph`, `multimodal`, `document_hybrid`, `hybrid_semantic`, `chat`.
- **Precedência fixa de 10 ramos** com `reason` estável (vocabulários fechados,
  versionados por `TERMS_VERSION`). Alterar um vocabulário obriga a rever o gold.
- **Normalização** NFKD + remoção de diacríticos + `casefold`, com comparação em
  fronteira de palavra: `betão ≡ betao`, mas `portanto` não dispara `porta`.
- O router recebe `request.message` **verbatim**, nunca a query reescrita pelo
  LLM, para que a decisão seja reproduzível a partir do pedido.

**A degradação é do endpoint, nunca do router.** `backend/api/main.py` expõe
`BASE_STRATEGY` (total sobre `Route`) e `execution_strategy(decision, context)`,
que devolve `(estratégia legacy, route_degraded)`. Degrada em exatamente dois
casos: **D1** rotas ainda sem backend (`graph`, `multimodal`, `document_hybrid`)
e **D2** `exact_lookup` sem resultados anteriores. `decision.route` e
`decision.reason` nunca são reescritos — o plano e o log guardam sempre a rota
verdadeira, pelo que HBIM-070/082/090 só terão de mudar a capability map.

**Observabilidade.** Um único evento `router_decision` por pedido, emitido antes
de qualquer ramificação (cobre os oito pontos de retorno, incluindo o caminho
`chat`, onde `plan is None`), com exatamente as chaves `route`, `strategy`,
`degraded`, `reason`, `signals`, `matched_terms`. **A query do utilizador nunca
entra neste payload**: `matched_terms` contém só constantes do vocabulário e
`reason` só identificadores fechados. Os planos que já existiam ganham
`route`/`route_degraded` (opcionais, com default — planos de paginação
serializados antes desta issue continuam a desserializar).

O gold de routing vive em `backend/eval/dataset/routing_gold.jsonl` (86 casos,
uma linha JSON canónica por caso, ordenado por `id`). O gate
`routing_accuracy ≥ 0.95` corre **offline**, sem Docker e sem marker
`integration`. O ficheiro fica ao lado do dataset da HBIM-005 sem interferir com
ele: `_validate_checksums` usa uma allowlist explícita e não varre o diretório.

```bash
# Testes offline (sem Docker, sem rede, sem ML, sem relógio)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_router.py backend/tests/test_routing_gold.py -q -o addopts=""

# Determinismo sob ordens diferentes
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_router.py -q -o addopts="" --randomly-seed=1
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_router.py -q -o addopts="" -p no:randomly

# Prova de que o LLM saiu do routing (deve devolver zero linhas)
grep -n "CLASSIFY_INTENT" backend/api/main.py

# Qualidade (retrieval está no gate bloqueante do mypy e no Ruff)
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend
```

`CLASSIFY_INTENT` permanece definido em `backend/api/prompts.py` mas deixa de ser
importado e chamado; removê-lo é HBIM-041 *(feito — ver secção seguinte)*.

## Query parser determinístico (HBIM-041)

`backend/retrieval/query_parser.py` substitui os cinco prompts LLM de extração
(`EXTRACT_IFC_CLASS`, `EXTRACT_FILTERS`, `EXTRACT_CONDITIONS`,
`EXTRACT_AGGREGATION`, `EXTRACT_DETAIL_REF`) por regex e dicionários fechados.
`parse_query(text) -> ParsedQuery` é **pura, total e determinística** e importa
apenas a stdlib e `retrieval.router` — reutiliza `normalize_query`, `fold_text`
e `GLOBAL_ID_RE` do router (mesmos objetos; nunca uma cópia), pelo que parser e
router não podem divergir em normalização nem em GlobalId.

- **Campos extraídos:** `ifc_class` (dicionário legacy `IFC_CLASS_TABLE`
  migrado sem perdas: 100 pares → 93 chaves normalizadas + 21 nomes literais),
  `materials` (canónicos, ordenados), `storey` (forma canónica; `piso N`,
  ordinais, `1.º`, `R/C`, `rés-do-chão`, `térreo`, `cave`, `nível L0`),
  condições numéricas (`eq/approx/gt/gte/lt/lte` × `height/area/volume/
  thickness`, vírgula decimal, `m²`/`m³` por NFKD, conversão `cm`/`mm` por
  divisão exata, intervalos `entre N e M` normalizados), `global_ids` (ordem
  de aparição, caso preservado), `agg_field` (vocabulário =
  `AGG_FIELD_MAP` ∪ `{count}`), `name`/`project_id`/`project_name`
  (`project_id` **só** com marcador explícito — a mesma condição do guard do
  endpoint) e `refers_previous`; `parse_detail_ref(text, num_results)` resolve
  ordinais de detalhe já clamped.
- **Zero LLM no parsing.** O `/chat` faz agora, por pedido (primeiro turno):
  chat 1, structured 2, aggregation 1, detail 1, semantic 3 chamadas LLM —
  todas de resposta/relevância (`REWRITE_QUERY`, `EXTRACT_EMBEDDING_QUERY` e
  `FILTER_RESULTS_BATCH` mantêm-se). Uma fixture-bomba falha qualquer chamada
  JSON que não seja dos dois prompts mantidos.
- **Prompts removidos** de `prompts.py`: os cinco de extração,
  `CLASSIFY_INTENT` e `IFC_CLASS_TABLE` (migrada para o parser com teste
  golden). O diff de `prompts.py` é só remoções.
- **Gold e baseline congelada:** `backend/eval/dataset/parser_gold.jsonl`
  (96 casos curados à mão) e `backend/eval/baselines/legacy_extraction.json`
  (38 exemplares few-shot transcritos verbatim dos prompts legacy @ `2ff0315`,
  SHA-256 fixado no teste). Gates offline: paridade `parser ≥ legacy` nos 56
  pares cobertos, full-record ≥ 0.95, por-campo ≥ 0.90, com provas de que os
  gates conseguem falhar.

```bash
# Testes offline (sem Docker, sem rede, sem ML, sem relógio)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_query_parser.py backend/tests/test_parser_gold.py \
  -q -o addopts=""

# Prova de que o LLM saiu do parsing (deve devolver zero linhas)
grep -n "EXTRACT_IFC_CLASS\|EXTRACT_FILTERS\|EXTRACT_CONDITIONS\|EXTRACT_AGGREGATION\|EXTRACT_DETAIL_REF\|CLASSIFY_INTENT\|IFC_CLASS_TABLE" \
  backend/api/main.py backend/api/prompts.py

# Auditoria manual da baseline legacy (fora de CI)
git show 2ff0315:backend/api/prompts.py | less

# Qualidade (retrieval.query_parser está no gate bloqueante do mypy e no Ruff)
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend
```

Os filtros extraídos (`material`/`storey`/`name`/`project_name`) **ainda não
são aplicados** ao OpenSearch — `build_opensearch_query` continua a usar apenas
`ifc_class`, `project_id` e `conditions`. Aplicá-los é HBIM-042
(`retrieval/lexical.py`), tal como a correção da agregação de classificação
*(feito — ver secção seguinte)*.

## Filtros lexicais e agregação de classificação (HBIM-042)

`backend/retrieval/lexical.py` (stdlib-only, puro, sem clientes) constrói as
cláusulas que aplicam os valores do parser HBIM-041 ao índice ativo
`bim_elements`, e `api/search.py` anexa-as em `build_opensearch_query` e
`build_aggregation_query`:

- **material** — `terms` sobre o keyword `material` (OR dentro da dimensão,
  AND com as restantes; valores canónicos do parser verbatim, caso coberto
  pelo normalizer `lc` do índice);
- **storey** — `terms` sobre `spatial_hierarchy.storey_name` com a expansão
  determinística fechada do canónico (`"1"` → `piso 1`, `andar 1`, `nivel 1`,
  `nível 1`, `level 1`, `storey 1`, `floor 1`, `01`, …; `"0"` inclui `r/c`,
  `rés-do-chão`, `térreo`; `"-1"` inclui `cave`; `LEXICAL_TERMS_VERSION`
  versiona o vocabulário);
- **name** — `term` exato case-insensitive sobre `name.keyword`;
- os filtros entram em **contexto `filter`** (sem scoring), aplicam-se também
  ao prefiltro kNN semântico e à reexecução de paginação, e as agregações
  passam a respeitá-los ("quantas paredes de pedra existem?" conta só paredes
  de pedra). Apenas `term`/`terms` — nunca `query_string`, `wildcard`,
  `regexp` ou `script`.

**Agregação de classificação corrigida.** `classifications` é `nested` e
`classifications.name` é `text` sem keyword — a `terms` plana histórica
rebentava com `RequestError` (e, mesmo no caminho keyword, devolvia zero
buckets sem o wrapper `nested`). A agregação válida é `nested` sobre
`classifications` + `terms` em `classifications.code` (keyword) +
`reverse_nested`, e os buckets contam **elementos** (um elemento com o mesmo
código repetido conta uma vez), ordenados por `(-count, key)`.

O snapshot de compatibilidade HBIM-005 `q-rs-classification-agg`, que
congelava a falha (`{"error": "RequestError"}`), foi atualizado
cirurgicamente para o comportamento correto
(`{"agg_total": 28, "buckets": {"ss_25": 28}}`, derivado à mão do corpus) —
única chave alterada em `current_system.json`; `correctness_metrics` e o
snapshot de material ficaram byte-idênticos. O snapshot
`q-rs-material-ignored` é invariante por construção (os quatro beams do
corpus são todos de aço).

```bash
# Testes offline (builders e parsing, sem Docker)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_lexical.py -q -o addopts=""

# Prova em OpenSearch real efémero (Docker local, loopback):
# "paredes de pedra no piso 1" devolve o conjunto exato; buckets exatos;
# as formas históricas erradas falham
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_lexical_filters_apply.py -m integration -q -o addopts=""

# Qualidade (retrieval.lexical está no gate bloqueante do mypy e no Ruff)
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend
```

Fronteiras v1 documentadas: labels de piso fora da expansão fechada, materiais
compostos (`"pedra calcária"` não casa com `pedra`) e nomes parciais não são
cobertos — evoluir exige bump de `LEXICAL_TERMS_VERSION`. BM25, dense, RRF,
reranking e EvidencePack são HBIM-050.

## Serviço isolado de embeddings Qwen3 (HBIM-030)

`Qwen/Qwen3-Embedding-8B` corre num **processo próprio** (Text Embeddings
Inference, imagem e revisão **pinadas**, GPU, **apenas loopback**), consumido
pelo cliente tipado `backend/models/embeddings_qwen3.py`. A API e os indexers
**deixaram de carregar `SentenceTransformer`/`torch` in-process**. Detalhes de
deployment e limpeza segura: `deploy/embeddings/README.md`.

```bash
# arrancar / prontidao / identidade / parar
docker compose -f deploy/embeddings/docker-compose.yml up -d
curl --fail --silent http://127.0.0.1:8081/health && echo READY
curl --fail --silent http://127.0.0.1:8081/info      # model_id + model_sha pinados
docker compose -f deploy/embeddings/docker-compose.yml down

# testes offline (sem GPU, sem modelo, sem rede)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/test_embeddings_qwen3.py -q -o addopts=""

# testes ao vivo (exigem o servico acima; nunca corridos no CI)
HBIM_REQUIRE_EMBEDDING_SERVICE=1 ~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_embeddings_qwen3_service.py -m gpu_service -q -o addopts=""

# benchmark de latencia (p50/p95/max por dimensao) -> backend/eval/reports/ (git-ignored)
PYTHONPATH=backend ~/miniconda3/bin/conda run -n hbim-rag python -m eval.bench.embedding_latency \
  --dimensions 1024,2048,4096 --model-revision <40-hex-revision>
```

**Marcadores.** A suite ao vivo tem `integration` **e** `gpu_service`: sai em
`-m "not integration"` (unit), sai no job de CI (`-m "integration and not
gpu_service"`) e entra apenas com `-m gpu_service`. Sem o servico a correr faz
**skip** com razão explícita; com `HBIM_REQUIRE_EMBEDDING_SERVICE=1` é falha dura.

**Dimensões.** Só `{1024, 2048, 4096}`. A escolha da dimensão de produção por
índice é da **HBIM-031**, não desta issue.

**Rota semântica (transição).** O índice ativo `bim_elements` ainda contém
vetores do espaço legacy; vetores Qwen são um **espaço diferente mesmo com o
mesmo comprimento**. Por isso `get_query_embedding` falha fechado com
`EmbeddingSpaceUnavailableError` e o endpoint degrada para o caminho **não
semântico** — estruturado, exato, agregação e lexical mantêm-se intactos. Pela
mesma razão o `build_actions` do indexer legacy recusa produzir vetores
(`python -m ingestion.index_to_opensearch` já não indexa densamente). A
indexação densa regressa na **HBIM-031**, contra um índice reconstruído.

## Baseline de qualidade de modelo semântico (HBIM-005B)

O gold pré-registado vive em `backend/eval/semantic_gold/` e é **imutável**: o
runner recalcula os cinco hashes e aborta **antes** de contactar qualquer modelo
se um único byte diferir. Corrigir o gold exige nova `dataset_version` e um novo
commit de pré-registo — nunca uma emenda depois de ver resultados.

Perfil ML isolado (não instalado pelos jobs de CI):

```bash
pip install -r backend/requirements.txt -r backend/requirements-ml.txt
```

Correr a baseline (o cache de modelos fica **fora** do repositório):

```bash
docker compose -f deploy/embeddings/docker-compose.yml up -d   # Qwen via TEI

cd backend
HF_HOME="${HBIM_HF_CACHE:-$HOME/.cache/huggingface}" \
EMBEDDING_SERVICE_URL=http://127.0.0.1:8081 \
EMBEDDING_SERVICE_MODEL_ID=Qwen/Qwen3-Embedding-8B \
EMBEDDING_SERVICE_MODEL_REVISION=1d8ad4ca9b3dd8059ad90a75d4983776a23d44af \
  conda run -n hbim-rag python -m eval.run_semantic_baseline \
  --models zembed,qwen --write-baseline
```

`EMBEDDING_SERVICE_MODEL_REVISION` é obrigatório e é um **pin público**
(documentado em `deploy/embeddings/README.md`), não um segredo.

Testes com modelos reais — falham em vez de saltar quando a flag está a 1:

```bash
HBIM_REQUIRE_SEMANTIC_MODELS=1 HBIM_REQUIRE_EMBEDDING_SERVICE=1 \
  conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_semantic_baseline_models.py \
  -q -o addopts="" -m model_service
```

Notas operacionais:

- `zeroentropy/zembed-1` é público, mas um token HF expirado em disco faz
  devolver **401 mesmo em leituras públicas**. Os adaptadores pedem
  explicitamente acesso anónimo, pelo que a baseline não depende do estado de
  credenciais do operador — e nunca lê nem escreve o ficheiro de token.
- O modelo declara um módulo `modeling_zembed.py` próprio, logo é resolvido
  primeiro o snapshot da revisão pinada e só depois carregado a partir dessa
  pasta (`trust_remote_code=True`).
- Os documentos Qwen são enviados **um por pedido**: em lote, dois passes
  idênticos não são reprodutíveis (23/122 vetores iguais) e trocam posições
  quase empatadas. HBIM-031, que mede latência, pode voltar a usar lotes.
- O artefacto `backend/eval/baselines/semantic_model_quality.json` não tem
  timestamp, vetores, hostname nem caminhos absolutos. Saídas volumosas ficam em
  `backend/eval/semantic_reports/` (ignorado pelo Git).

## Benchmark de dimensão e reindexação densa (HBIM-031)

A dimensão de produção do índice `elements` foi selecionada por benchmark
pré-comprometido sobre o gold imutável de HBIM-005B: **4096** (decisão e traço
completos em `backend/eval/baselines/dimension_decision.json`; regra do
seletor em `backend/eval/dim_selector.py`, `hbim-031-1`). O mapping denso é
`backend/canonical/mappings/elements_v2.json` — gerado, nunca editado à mão
(um teste regenera-o e compara byte a byte).

Reproduzir o benchmark (TEI ativo; OpenSearch efémero gerido pelo próprio CLI):

```bash
cd backend
HBIM_REQUIRE_EMBEDDING_SERVICE=1 \
EMBEDDING_SERVICE_MODEL_REVISION=1d8ad4ca9b3dd8059ad90a75d4983776a23d44af \
  conda run -n hbim-rag python -m eval.dim_benchmark --ephemeral --write-artifact
```

Reindexação densa de um JSONL canónico de elementos para o físico v2 (criado
com `migrate create --record-type element --physical-version 2
--mapping-version 2`; o `index.knn` é ativado automaticamente):

```bash
cd backend
EMBEDDING_SERVICE_MODEL_REVISION=1d8ad4ca9b3dd8059ad90a75d4983776a23d44af \
  conda run -n hbim-rag python -m ingestion.indexers.elements_dense \
  --input <elements.jsonl> --physical-version 2 --dimensions 4096 \
  --opensearch-url http://127.0.0.1:<porta>
```

Notas operacionais:

- o preflight recusa qualquer índice cujo `_meta` não declare exatamente o
  espaço `Qwen/Qwen3-Embedding-8B@1d8ad4ca…/d4096` e a projeção `v1` — misturar
  espaços zembed/Qwen é estruturalmente impossível;
- promoção e rollback continuam a ser passos explícitos do `ingestion.migrate`
  (HBIM-021); uma reindexação parcial nunca move o alias;
- a rota semântica da API continua *fail-closed*; a ativação do retrieval denso
  sobre o alias canónico pertence a **HBIM-051** (depois do reranker), não a
  HBIM-050;
- URLs de OpenSearch não-loopback são recusados por ambos os CLIs.

## Retrieval híbrido BM25 + dense + RRF (HBIM-050)

Geração de candidatos determinística: BM25 top-200 + dense Qwen3 top-200 →
**RRF não-ponderado (k=60)** → união completa preservada. É *candidate
generation*, não a qualidade final: o reranker e o gate bloqueante
`nDCG@10 ≥ dense-sozinho` são de **HBIM-051**.

Módulos: `retrieval/{lexical.py (secção BM25 canónica), dense.py, rrf.py,
canonical_filters.py, hybrid.py}`. Seam para HBIM-051:
`retrieval.hybrid.HybridRetriever.retrieve(text, filters=…, top_n=None)`
devolve a união fundida completa (o conjunto que o reranker vai reordenar).

Avaliação diagnóstica no gold imutável de HBIM-005B (TEI ativo; OpenSearch
efémero gerido pelo CLI):

```bash
cd backend
HBIM_REQUIRE_EMBEDDING_SERVICE=1 \
EMBEDDING_SERVICE_MODEL_REVISION=1d8ad4ca9b3dd8059ad90a75d4983776a23d44af \
  conda run -n hbim-rag python -m eval.hybrid_eval --ephemeral --write-report
```

Notas operacionais:

- o relatório (`backend/eval/reports/hybrid_eval.json`, ignorado pelo Git)
  regista BM25-only, dense-only e **RRF-cru** (nDCG@10/Recall@10/MRR@10),
  wins/ties/losses, tamanho da união, sobreposição e flags de saturação;
- **o RRF-cru pode ficar abaixo do dense-sozinho** — é **diagnóstico**, nunca um
  gate; no gold (122 docs < k=200) ambas as fontes saturam o corpus, pelo que a
  fusão não-ponderada não distingue por ausência de fonte;
- o *exit code* do CLI reflete apenas sucesso **operacional** (as duas fontes
  correram, sem pedidos falhados, hashes verificados) — nunca a comparação de
  qualidade;
- os hashes do gold são reverificados antes de qualquer modelo; filtros
  estruturais são idênticos nas duas fontes (um único construtor canónico);
- a rota `Route.HYBRID_SEMANTIC` continua fechada por omissão; a ativação
  restrita (fail-closed) e o reranker são de **HBIM-051** (secção seguinte);
  `FILTER_RESULTS_BATCH` foi **removido** em HBIM-051.

## Serviço isolado Qwen3-Reranker-8B (HBIM-051)

Reranker cross-encoder servido por **vLLM v0.25.1** (imagem pinada por digest),
modelo `Qwen/Qwen3-Reranker-8B` @ `77d193c791ed757ca307ee72715aa132723da912`
(BF16, `--runner pooling`, overrides seq-cls oficiais, template de score
oficial pinado por sha256), **apenas loopback** `127.0.0.1:8082`, com
`VLLM_BATCH_INVARIANT=1` (scores determinísticos independentes do batching).

```bash
cd deploy/reranker
docker compose config --quiet          # validação estática
docker compose up -d                   # arrancar (download ~16 GB na primeira vez)
curl -s http://127.0.0.1:8082/health   # 200 quando o modelo está carregado
curl -s http://127.0.0.1:8082/v1/models | head -c 300   # identidade servida
docker compose down                    # parar (só o operador; nunca código do repo)
```

Notas operacionais:

- a cache Hugging Face monta a RAIZ `${HBIM_HF_HOME:-~/.cache/huggingface}` —
  variável **distinta** de `HBIM_HF_CACHE` (HBIM-030), que aponta para a
  SUBpasta `hub/` do TEI; trocar as duas derrota a cache do modelo;
- coexistência estática com o serviço de embeddings provada por medição
  (`nvidia-smi`), orçamento utilizável = 90 % da VRAM física; **não** há gestor
  de residência (HBIM-032);
- cliente tipado: `models/reranker_qwen3.py` (lazy, import-safe, retries
  determinísticos sem jitter, erros sem texto); settings `RERANKER_*` em
  `shared/config.py`;
- o score servido é usado **verbatim** (`σ(logit_yes − logit_no)` ∈ (0,1));
  nenhuma transformação no cliente.

Avaliação reranked no gold imutável (TEI + reranker ativos; OpenSearch
efémero):

```bash
cd backend
EMBEDDING_SERVICE_MODEL_REVISION=1d8ad4ca9b3dd8059ad90a75d4983776a23d44af \
  conda run -n hbim-rag python -m eval.rerank_eval --ephemeral --write-report
```

Suite live (falha, nunca skip, com `HBIM_REQUIRE_RERANKER_SERVICE=1`):

```bash
conda run -n hbim-rag python -m pytest backend/tests/integration/test_rerank_apply.py \
  -q -o addopts="" -m reranker_service
```

Ativação do caminho híbrido reranked no `/chat` (§19 da spec): **desligada por
omissão**; requer `HYBRID_ACTIVATION_ENABLED=1`, um
`HYBRID_SNAPSHOT_SIGNING_SECRET` dedicado (mínimo 32 caracteres — nunca uma
API key) **e** um alias canónico `hbim_elements` com o espaço de embeddings de
HBIM-031. Sem reranker saudável, identidade validada e preflight `_meta`
aprovado, o pedido degrada para o caminho legacy — **nunca** existe fallback
RRF-cru. O limiar de aceitação (`RERANKER_SCORE_THRESHOLD_MODE` /
`RERANKER_SCORE_THRESHOLD`) tem como default o resultado decidido pelo
protocolo out-of-fold committed em
`backend/eval/baselines/reranker_decision.json`.

Paginação com **snapshot assinado** (§19.3, determinismo v6): a pesquisa
inicial congela a ordem aceite completa num token HMAC-SHA256
(`hs1.<payload>.<assinatura>`, TTL `HYBRID_SNAPSHOT_TTL_SECONDS`, default
3600 s, intervalo [60, 86400]); cada página é uma fatia exata desse snapshot e
**nunca** re-executa embedding, retrieval ou reranking. Um token inválido,
expirado ou com identidades desatualizadas falha fechado com uma mensagem
determinística. Rodar o secret invalida todos os snapshots em circulação
(comportamento operacional documentado, não um defeito). O token transporta
apenas ids e identidades — nunca texto de query/documentos, scores ou vetores.
Nota de honestidade (§10.1): execuções independentes da mesma pesquisa podem
permutar documentos quase-empatados, incluindo na fronteira do top-10; a
estabilidade garantida é **por snapshot**, e a deriva entre execuções é medida
e reportada como diagnóstico — nunca se afirma determinismo de ranking entre
execuções.

### Gestor de residência de VRAM e endpoint de operações (HBIM-032)

O gestor mantém um registo tipado dos serviços de modelo, contabiliza VRAM de
forma **conservadora** e garante o invariante `Σ ≤ orçamento` em **todos** os
estados intermédios de uma transição.

Capacidade real dos serviços atuais (medida, nunca assumida): ambos são
**apenas observáveis** — o TEI não expõe qualquer rota de ciclo de vida e o
vLLM tem o *sleep mode* **desativado** no manifesto pinado (`/sleep`,
`/wake_up`, `/is_sleeping` respondem 404). `GET /load` do vLLM é telemetria
("Get Server Load Metrics"), **nunca** uma operação de residência. Transições
não suportadas **falham fechadas** com um motivo tipado; nunca são simuladas
como bem-sucedidas. O perfil `P-Verify-Hard` (colocar Emb+Rerank em *sleep*)
é provado **apenas em simulação determinística**.

Orçamento: `RESIDENCY_VRAM_BUDGET_MIB` quando definido, senão
`total − RESIDENCY_VRAM_RESERVE_MIB` (reserva por omissão `10240` MiB). Tudo em
MiB inteiros. Neste host (97 887 MiB) o orçamento derivado é 87 647 MiB.
A atribuição de VRAM **por processo** não existe em WSL2
(`--query-compute-apps` devolve `[N/A]`), pelo que é reportada como
`"unavailable"` — nunca `0` e nunca substituída pela fração configurada.

Os manifestos passam a declarar metadados de posse exatos
(`com.hbim.project`, `com.hbim.service`, `com.hbim.milestone`); o gestor só
atua sobre serviços que correspondam aos três por **igualdade exata**. Depois
de atualizar os manifestos, recrie os contentores para que as etiquetas fiquem
ativas:

```bash
docker compose -f deploy/embeddings/docker-compose.yml up -d
docker compose -f deploy/reranker/docker-compose.yml up -d
```

Endpoint de operações — **desligado por omissão**. Ativa com
`OPS_ENDPOINT_ENABLED=1`; fica sempre atrás da autenticação existente
(`X-API-Key`). Sem a flag as rotas **não existem** (404).

```bash
curl -sS -H "X-API-Key: <chave>" http://127.0.0.1:8000/ops/residency
curl -sS -H "X-API-Key: <chave>" -H 'Content-Type: application/json' \
  -d '{"profile": "P-Online-Text"}' \
  http://127.0.0.1:8000/ops/residency/ensure
```

O corpo aceita **apenas** um enum fechado de perfis: não é possível indicar um
serviço, contentor ou caminho arbitrário. As respostas não contêm nomes de
contentores, digests, URLs, caminhos absolutos nem texto de modelo. Não existe
adaptador Docker nem exposição do socket do Docker.

Suite live dedicada (falha, nunca skip, com `HBIM_REQUIRE_RESIDENCY_SERVICE=1`):

```bash
conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_residency_apply.py -q -o addopts="" -m residency_service
```

## Serviços locais de desenvolvimento (Docker Compose)

Imagens pinadas: `opensearchproject/opensearch:2.19.1` e `neo4j:5.26.0`.
O compose é **apenas para desenvolvimento local** (portas em loopback,
credencial Neo4j sintética `neo4j/localdevpassword`, OpenSearch sem plugin de
segurança). A suite de testes não o usa — a integração cria containers
efémeros via Testcontainers.

```bash
docker compose -f docker-compose.dev.yml config     # validar
docker compose -f docker-compose.dev.yml up -d      # arrancar
docker compose -f docker-compose.dev.yml ps         # estado/health
curl -s http://127.0.0.1:9200/_cluster/health       # OpenSearch
curl -s -I http://127.0.0.1:7474                    # Neo4j
docker compose -f docker-compose.dev.yml down       # parar
docker compose -f docker-compose.dev.yml down -v    # parar e apagar volumes (destrutivo)
```

## Nota WSL + Docker Desktop

O Docker tem de estar acessível **de dentro do WSL** (Docker Desktop →
Settings → Resources → WSL Integration, ativado para esta distro). Os testes
de integração detetam o daemon via SDK Python pela seguinte ordem:
`DOCKER_HOST` explícito → `/var/run/docker.sock` → socket proxy do Docker
Desktop (`/mnt/wsl/docker-desktop/shared-sockets/host-services/docker.proxy.sock`,
se acessível ao utilizador). Se nenhum funcionar, reiniciar o Docker Desktop
(e, se necessário, `wsl --shutdown` no Windows) costuma recriar
`/var/run/docker.sock`. Sem Docker, os testes de integração fazem **skip**
com razão explícita; em CI (`HBIM_REQUIRE_DOCKER=1`) a mesma condição é uma
falha dura.

## CI

Workflow único em `.github/workflows/ci.yml` (push + pull_request), com
`permissions: contents: read`, sem `secrets.*` e sem `.env`: jobs
`backend-unit`, `ruff`, `mypy` (gate dos módulos tipados + `backend/eval` +
`backend/retrieval`),
`frontend` (`npm ci` + lint + build, Node 22), `integration-opensearch`
(`needs: backend-unit`, `HBIM_REQUIRE_DOCKER=1`) e `evaluation-opensearch`
(`needs: backend-unit`, gate contra a baseline committed + upload do relatório
como artifact).
