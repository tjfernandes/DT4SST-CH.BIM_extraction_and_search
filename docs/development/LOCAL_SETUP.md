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
`backend-unit`, `ruff`, `mypy` (gate dos módulos tipados + `backend/eval`),
`frontend` (`npm ci` + lint + build, Node 22), `integration-opensearch`
(`needs: backend-unit`, `HBIM_REQUIRE_DOCKER=1`) e `evaluation-opensearch`
(`needs: backend-unit`, gate contra a baseline committed + upload do relatório
como artifact).
