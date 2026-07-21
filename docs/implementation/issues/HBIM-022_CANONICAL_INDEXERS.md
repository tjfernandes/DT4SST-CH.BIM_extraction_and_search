# HBIM-022 — Canonical JSONL Indexers and PropertyFact Projection

> **Tipo:** especificação executável de issue.
> **Branch obrigatória:** `feat/hbim-022-canonical-indexers`.
> **Depende de:** HBIM-010/011/012 (schema canónico e produtor de JSONL), HBIM-020
> (quatro mappings estáticos), HBIM-021 (lifecycle e aliases) — todos merged.
> **Bloqueia:** HBIM-030 (serviço de embeddings) e, indiretamente, HBIM-040+.
> **Revisão:** incorpora todas as correções do review adversarial
> (3 CRITICAL, 5 HIGH, 6 MEDIUM, LOW materiais).

---

## 1. Contexto

A HBIM-011/012 produz **JSONL canónico validado**; a HBIM-020 definiu **o que cada
índice aceita**; a HBIM-021 definiu **como os índices físicos nascem e como os
aliases apontam para eles**. Falta a peça que liga as duas metades: **ler o JSONL
canónico e escrevê-lo, projetado, nos índices físicos**.

Hoje o repositório tem índices vazios (ou inexistentes) e ficheiros JSONL que
ninguém consome. O único indexer existente (`ingestion/index_to_opensearch.py`) é
**legacy**: escreve o blob monolítico em `bim_elements`, carrega um
`SentenceTransformer` in-process e usa `_id = f"{project_id}_{id}"`. Não é o
caminho canónico e **não é convertido nesta issue**.

A HBIM-022 fecha o ciclo de ingestão estrutural:

```
IFC ──HBIM-011/012──► JSONL canónico ──HBIM-022──► índices físicos ──HBIM-021──► aliases
```

A peça crítica e não trivial é a **projeção do `PropertyFact.value`**: o record
canónico tem um objeto polimórfico (`{"value": <str|int|float|bool|null>,
"value_type": <T>}`) que o OpenSearch **não consegue mapear** num único path. A
HBIM-020 §5 já ratificou o contrato de projeção tipada e disjunta; a HBIM-022
**implementa-o e garante as invariantes que o mapping não consegue exprimir**.

---

## 2. Precedência de fontes

Em caso de divergência, prevalece por esta ordem:

1. **Código e testes atualmente implementados** (`backend/canonical/**`,
   `backend/ingestion/index_lifecycle.py`, `backend/ingestion/canonical_ifc.py`,
   `backend/tests/**`) e o código **instalado** de `opensearch-py 3.1.0`
   (`helpers/actions.py`), cuja semântica real esta spec cita.
2. **HBIM-020** — `docs/implementation/issues/HBIM-020_STATIC_INDEX_MAPPINGS.md`
   (contrato dos mappings e da projeção, §5).
3. **HBIM-021** — `docs/implementation/issues/HBIM-021_ALIAS_MIGRATION.md`
   (registry, naming físico, lifecycle, CLI, exceções, import-safety).
4. `docs/implementation/IMPLEMENTATION_STATUS.md`.
5. **Auditoria read-only HBIM-022 + review adversarial** (decisões ratificadas
   nesta spec).
6. `docs/implementation/ROADMAP.md` — **apenas orientação geral**.

**Âmbito de ficheiros desta issue** (wording exato; ver também §30):

- **Cria:** o pacote `backend/ingestion/indexers/`, os testes offline e de
  integração, fixtures sintéticas adicionais quando necessárias
  (`backend/tests/fixtures/canonical/indexing/`), e esta spec.
- **Modifica apenas:** `pyproject.toml` (gate mypy), `.github/workflows/ci.yml`
  (gate mypy), `docs/development/LOCAL_SETUP.md` (secção operacional) e — **só no
  fim, depois de todos os gates** — `docs/implementation/IMPLEMENTATION_STATUS.md`.
- **Não altera nada mais.** `backend/canonical/{schema,ids,serialization,__init__}.py`,
  os quatro mappings HBIM-020 e `backend/ingestion/{index_lifecycle,migrate}.py`
  ficam **byte-idênticos**.

### 2.1 Divergências do ROADMAP e resolução

| # | ROADMAP | Realidade implementada | Resolução nesta spec |
|---|---|---|---|
| A | §805–809: indexers `elements / property_facts / documents / **chunks**` | HBIM-020 §3 e §52 criaram **quatro** mappings **sem chunks** (não existe `ChunkRecord` canónico); chunks adiado para HBIM-070. HBIM-021 §3 fixou o registry de quatro | **`chunks` é substituído por `classification_fact`.** Reconciliação já ratificada em HBIM-020 §3/§20(a) e HBIM-021 §2/§40 — **não é decisão arquitetural nova** |
| B | §793: título de HBIM-020 nomeia chunks | idem | idem |
| C | §321–356 (M2): `migrate.py` "lê `bim_elements` e converte via canónico" | HBIM-021 §2 declarou-o fora de scope | **Fora de scope** (§4) |
| D | §351 (M2): "Aliases funcionam; **API lê por alias**" | API/retrieval continuam em `bim_elements` (HBIM-021 §17.1, §28) | **Fora de scope** (§4). Ver §2.2 |
| E | §333 (M2): `elements_v2` com `embedding_qwen3`, `evidence_refs`, `relations_summary`, `classification_codes` | HBIM-020 §2 declarou estes campos **superados**; `test_index_mappings.py::FORBIDDEN_FIELD_NAMES` proíbe-os | Superado; a projeção deriva **exclusivamente** de `model_fields` |
| F | §341 (M2): "`fact_id`/`chunk_id` são `_id`" | `fact_id` confirmado; `chunk_id` inexistente | Confirma §10, menos chunks |

### 2.2 Lacuna de roadmap assinalada (não pertence à HBIM-022)

**Nenhuma issue do backlog possui atualmente a migração da API/retrieval para os
aliases `hbim_*`.** HBIM-021 §28 empurrou-a para "HBIM-022 ou posterior"; o scope
da HBIM-022 exclui-a explicitamente; HBIM-030/031 tratam de embeddings e
HBIM-040/041/042 de routing e parsing. Depois da HBIM-022, os quatro índices
estarão **populados e verificados**, mas `api/search.py` continuará a ler
`config.OPENSEARCH_INDEX` (`bim_elements`).

**Esta spec assinala a lacuna e não a resolve.** Deve ser criada uma issue
própria (p.ex. `HBIM-023 — API/retrieval sobre os aliases canónicos`) antes ou em
conjunto com HBIM-040+. Alterar `backend/api/**` nesta issue é uma violação de
scope (§30). *(Critério verificado por review, não por teste — §31.)*

### 2.3 Desvio menor já documentado

HBIM-021 §4.1 exige um guardrail de limite superior (`> 10000`) para
`physical_version`; `index_lifecycle.validate_physical_version` implementa
**"inteiro positivo, sem limite superior"** (docstring explícita), e o
`IMPLEMENTATION_STATUS.md` regista-o. A CLI da HBIM-022 usa **a semântica do
código** (`migrate.py::_positive_int`), não a da spec HBIM-021, para não divergir
entre as duas CLIs.

---

## 3. Objetivos

A HBIM-022 entrega:

1. **Leitura em streaming** dos quatro JSONL canónicos (nunca `read()`/`readlines()`).
2. **Validação** de cada linha pelo modelo Pydantic correto.
3. **Projeção** determinística de cada record para o respetivo mapping HBIM-020.
4. **Ações OpenSearch determinísticas** (função pura de `(ficheiro, target)`).
5. **`_id` canónico** verbatim, sem recomputação e sem concatenação.
6. **Indexação direta em índices físicos** compostos pelo registry HBIM-021.
7. **Zero promoção de aliases** — a promoção continua exclusiva do `migrate.py`.
8. **Rerun idempotente** por `_op_type=index` + `_id` canónico.
9. **Relatório determinístico** de contagens, estados e falhas, sem segredos.
10. **Verificação pós-indexação**: contagens, round-trip por `_id` e alias
    inalterado.
11. **Arquitetura de duas passagens com digest de estabilidade** (§8) que impede
    escrita remota quando um input local é inválido **ou foi alterado** desde a
    validação.

---

## 4. Fora de scope

Explicitamente **fora**, sem exceção:

- embeddings, vetores, `knn_vector`, kNN, Qwen3, qualquer modelo ML;
- OCR, parsing documental, conteúdo de documento;
- **chunks** e `ChunkRecord` (HBIM-070; não existe contrato canónico);
- **promoção automática de aliases** (HBIM-021 `migrate.py promote*`);
- **API/retrieval a consumir os novos aliases** (ver §2.2 — lacuna assinalada);
- **conversão do índice legacy `bim_elements`**;
- `_reindex` / `reindex` / `update_by_query` / `delete_by_query`;
- **criação ou eliminação de índices** (criação é HBIM-021; eliminação não existe
  em produção — só teardown de Testcontainers);
- alteração do schema canónico, dos mappings HBIM-020 ou do lifecycle HBIM-021;
- denormalização de `ifc_class` em `property_fact`/`classification_fact`
  (assinalada em HBIM-020 §7/§18/§20(e); continua adiada);
- reparação de aliases em conflito (fica exclusiva de intervenção manual /
  tooling HBIM-021 futuro; esta issue apenas **deteta e recusa** — §16–§17);
- novas variáveis de ambiente, novos serviços, novas dependências.

---

## 5. Registry dos indexers

Registry **fechado**, derivado do registry HBIM-021 (`index_lifecycle.RECORD_TYPES`
e `index_lifecycle.get_spec`) — **nunca redefinido nem duplicado**:

| `record_type` | ficheiro de entrada | modelo canónico | campo `_id` | alias (HBIM-021) | físico (HBIM-021) |
|---|---|---|---|---|---|
| `element` | `elements.jsonl` | `ElementRecord` | `element_id` | `hbim_elements` | `hbim_elements_v<N>` |
| `property_fact` | `property_facts.jsonl` | `PropertyFact` | `fact_id` | `hbim_property_facts` | `hbim_property_facts_v<N>` |
| `classification_fact` | `classification_facts.jsonl` | `ClassificationFact` | `classification_id` | `hbim_classification_facts` | `hbim_classification_facts_v<N>` |
| `document` | `documents.jsonl` | `DocumentRef` | `document_id` | `hbim_documents` | `hbim_documents_v<N>` |

- A **ordem determinística** de processamento é `index_lifecycle.RECORD_TYPES`
  (`element`, `property_fact`, `classification_fact`, `document`).
- Os aliases e os nomes físicos **não são redeclarados** em `ingestion/indexers/`;
  vêm sempre de `il.get_spec()` / `il.physical_index_name()`.
- Os ficheiros `coverage.json` e `warnings.jsonl` (também produzidos pela
  HBIM-011) **não são records indexáveis** e são **ignorados**.
- **Não existe `chunk`.** Um teste offline assere que o registry tem exatamente
  quatro entradas e nenhuma referência a chunks.

---

## 6. Layout

```
backend/ingestion/indexers/
  __init__.py                        # re-exports públicos apenas
  __main__.py                        # raise SystemExit(main())
  common.py                          # TODA a maquinaria partilhada
  registry.py                        # record_type -> IndexerSpec
  elements_indexer.py                # fino
  property_facts_indexer.py          # fino, mas com a projeção do valor (§12)
  classification_facts_indexer.py    # fino
  documents_indexer.py               # fino
  cli.py                             # argparse, cliente em runtime, output, exit codes
```

### 6.1 Responsabilidades

**`common.py`** — exceções (§24), leitor streaming e **digest de estabilidade**
(§7–§8), `validate_input`/`InputValidationResult` (§9.4), poda de `None` (§13),
validação de ranges (§14), deteção de duplicados (§15), builder de ações bulk,
runner bulk (§18), preflight de target (§16–17), verificação final (§22),
`IndexReport` e serialização estável (§21). **Não importa** os quatro indexers.

**`registry.py`** — liga `record_type` a `IndexerSpec(record_type, input_filename,
model, id_field, project)`. Importa `common` **e** os quatro indexers.

> **Porquê um `registry.py` separado do `__init__.py`:** `common.py` não pode
> importar os quatro indexers (eles importam `common` → ciclo). `registry.py`
> importa ambos os lados e mantém o grafo de imports **acíclico e explícito**.
> Razão técnica, não cerimónia. O `__init__.py` fica re-export puro, como
> `canonical/__init__.py`. *(Critério verificado por review — §31.)*

**Os quatro `*_indexer.py`** — **finos**, contendo exatamente três coisas:
constante `RECORD_TYPE`, binding do modelo Pydantic, e `project(record) -> dict`.
Para `element`, `classification_fact` e `document` o `project()` é uma linha
(`prune_nulls(record.model_dump(mode="json"))`). Só
`property_facts_indexer.py` tem lógica substancial (§12).
**Nenhum** deles fala com o OpenSearch, faz I/O de ficheiros ou define exceções.

**`cli.py`** — apenas argparse, construção do cliente **em runtime** (import
diferido de `shared.opensearch`), output determinístico (§23), mapeamento para
exit codes. Padrão idêntico a `migrate.py` (HBIM-021 §5.2).

**`__main__.py`** — uma linha, para permitir `python -m ingestion.indexers`.

### 6.2 Separação obrigatória

A lógica testável (leitura, digest, validação, projeção, planeamento de ações,
relatório) vive em `common.py` + indexers e é exercitável **sem argparse e sem
rede**. O runner bulk é uma **função separadamente invocável**
(`run_bulk(client, target, actions, options) -> BulkOutcome`), o que permite
testar falha parcial injetando uma ação sintética inválida (§26).

---

## 7. Input contract

Entrada **exclusivamente** por `--input-dir <DIR>`. **Não existe** opção de path
por record type; os nomes dos quatro ficheiros vêm do registry fechado (§5) — o
mesmo princípio anti-traversal da HBIM-021 §6.

| Situação | Comportamento |
|---|---|
| `--input-dir` inexistente ou não é diretório | `InputError` → exit 1 |
| Ficheiro obrigatório ausente | `MissingInputFileError` → exit 1. Em `index` (os quatro), falha se **qualquer** dos quatro faltar |
| Ficheiro com **zero bytes** | **Input local válido**: `lines_read=0`, `records_valid=0`, `expected_count=0`. **Os gates remotos continuam a aplicar-se** e falham se `actual_count != 0` (§19, §22). Ver §7.3 |
| Linhas vazias ou só-whitespace | **Ignoradas semanticamente**; contadas em `lines_blank`; **não** contam para `lines_read`; **não entram no digest** (§7.2) |
| Última linha com ou sem `\n` final | Ambas válidas e equivalentes (inclusive para o digest — §7.2) |
| UTF-8 inválido | `InputDecodeError` com `record_type` e `line_number`; **nunca** os bytes nem o conteúdo. Termina o scan desse ficheiro; `validate_input` devolve resultado parcial com `ok=false` (§9.4) |
| JSON inválido | `RecordParseError` com `record_type` e `line_number`; **nunca** o conteúdo |
| `schema_version` errada | `RecordValidationError` (já garantido por `SchemaVersion = Literal["1.0"]`) |
| Record do tipo errado no ficheiro errado | `RecordValidationError` (garantido por `extra="forbid"` + campos obrigatórios em falta) |
| `coverage.json` / `warnings.jsonl` presentes | Ignorados |
| Ficheiros extra no diretório | Ignorados (não é erro) |

### 7.1 Streaming obrigatório

Leitura por iteração de handle:

```python
with path.open("r", encoding="utf-8", newline="") as handle:
    for line_number, line in enumerate(handle, start=1):
        ...
```

- `encoding="utf-8"` com `errors` **strict** (default) — UTF-8 inválido levanta.
- **Proibido** `read()`, `readlines()`, `Path.read_text()` sobre o ficheiro de
  entrada, e proibido materializar a lista completa de records ou de docs
  projetados.
- Estruturas residentes em memória, e só estas: contadores, o **digest
  incremental** (§7.2), o **conjunto de `_id` vistos** (§15), a amostra
  determinística de verificação (§22) e, na segunda passagem, **um batch** de
  ações (§18).

> **Nota factual:** o produtor `canonical_ifc._stream_run_to_staging` escreve
> `elements`/`property_facts`/`classification_facts` por **ordem de iteração**,
> **não ordenados** (confirmado nas fixtures `ifc_extraction/`); só `to_jsonl()`
> ordena. O determinismo desta issue **não depende** de o ficheiro estar
> ordenado: vem de *ficheiro → ações* ser uma função pura que preserva a ordem
> de ficheiro.

### 7.2 Digest de estabilidade (obrigatório)

Contagens e `_id` **não chegam** para detetar mutações do input: um ficheiro pode
ser alterado mantendo o mesmo número de linhas, os mesmos `_id`, JSON válido e
projeções válidas, mudando apenas **valores de campos**. A estabilidade entre
passagens é garantida por um **digest SHA-256 incremental por ficheiro**, sobre o
conteúdo significativo normalizado:

```python
digest = hashlib.sha256()
for line in handle:                      # streaming, O(1) de memória
    stripped = line[:-2] if line.endswith("\r\n") else (
        line[:-1] if line.endswith("\n") else line)   # remove SÓ o terminador
    if stripped.strip() == "":
        continue                          # linha em branco: fora do digest
    b = stripped.encode("utf-8")
    digest.update(len(b).to_bytes(8, "big"))          # prefixo de comprimento
    digest.update(b)
```

Propriedades exigidas (todas testadas — §25):

- **streaming**, memória O(1); calculado como subproduto da mesma iteração que
  valida (Fase A) ou projeta (Fase C) — nunca uma leitura `read()` extra;
- **indiferente** à presença do newline final e a linhas em branco
  (`lines_blank` muda, o digest não);
- **sensível** a qualquer alteração de um byte significativo;
- o **prefixo de comprimento** (8 bytes big-endian) remove ambiguidade de
  concatenação — mesmo truque de `canonical/ids.py::_netstring`;
- **nunca expõe conteúdo** (o valor reportável é o hex do digest);
- **não** se usa mtime, tamanho ou inode: granularidade de mtime e edições do
  mesmo tamanho não são detetáveis por metadados.

O diretório de input é **contratualmente imutável durante a execução**. Qualquer
divergência de digest é `InputError` fail-closed, com `record_type` e a indicação
de qual comparação falhou (fase), **nunca** conteúdo. Os pontos exatos de
comparação estão em §8.

### 7.3 Input com zero records

Um ficheiro de zero bytes (ou só com linhas em branco) é **input local válido**
com `expected_count=0` e digest calculado sobre zero linhas significativas.
Consequências obrigatórias:

- **não** se chama `streaming_bulk` nem `client.bulk` com zero ações
  (short-circuit explícito; verificado também no código instalado:
  `_chunk_actions` com iterável vazio não produz nenhum chunk);
- `records_indexed=0`, `records_failed=0`, `bulk_batches=0`;
- refresh/count/verificação (§22) **executam normalmente**;
- a amostra de round-trip é vazia e os passos 4–5 de §22 são saltados;
- target vazio → sucesso (`ok=true`);
- target com documentos → `actual_count != 0` → **`VerificationError`**;
- `--require-empty` com target não vazio → **`TargetNotEmptyError`** no
  preflight, antes de qualquer escrita.

---

## 8. Duas passagens (obrigatório)

A indexação tem **duas passagens locais** sobre cada ficheiro, com um **digest de
estabilidade** a ligá-las. Nenhuma escrita remota acontece antes de a validação
local estar completa **e** de a estabilidade do input estar confirmada.

### 8.1 Fases

**Fase A — validação local (primeira passagem), sem cliente:**
para **cada** record type pedido, via `validate_input` (§9.4):

1. abrir o ficheiro de entrada;
2. **calcular o digest** (§7.2) como subproduto da iteração;
3. validar **todas** as linhas (parse + Pydantic), até ao fim do ficheiro;
4. **projetar** todos os records;
5. validar os **ranges numéricos** (§14);
6. detetar e contar **todas** as ocorrências de `_id` duplicados (§15);
7. calcular `lines_read`, `lines_blank`, `records_valid`, `records_invalid`,
   `duplicate_ids`, `expected_count`;
8. calcular a **amostra determinística** de verificação (§22.5);
9. devolver um `InputValidationResult` imutável com tudo isto — **sem levantar**
   (§9.4).

A Fase A **descarta** os docs projetados (não os materializa); o que retém é
contadores, digest, o conjunto de ids e a amostra.

Se **qualquer** `InputValidationResult` tiver `ok=false`, o orquestrador **não
constrói cliente, não faz preflight e não escreve**: constrói os `IndexReport`,
anexa-os à exceção tipada correspondente ao **primeiro** erro (ordem:
`RECORD_TYPES`, depois `line_number`) e termina (§9.4).

**Fase B — preflight remoto (§16, §17), para todos os targets pedidos:**
existência, `_meta.record_type`, compatibilidade de mapping, **conflitos de
alias (fail-closed)**, estado live, `--require-empty`. Captura o **snapshot do
alias** (§16.1.8) para a verificação §22.6.

**Fase B′ — imediatamente antes da primeira ação bulk de toda a execução:**
recalcular o digest de **todos os ficheiros pedidos** (releitura barata, sem
parsing, só a iteração do digest) e comparar com os digests da Fase A. Qualquer
divergência ⇒ **`InputError`**, **zero escrita em qualquer target**.

**Fase C — escrita (segunda passagem), por record type:**

1. **imediatamente antes** do primeiro bulk desse record type, recalcular o
   digest **desse ficheiro** e comparar com a Fase A; divergência ⇒
   `InputError`, esse target **não recebe escrita**, execução aborta (§20).
   *(Para o primeiro record type, esta verificação é adjacente à Fase B′ — sem
   nenhuma escrita entre as duas — e a implementação pode reutilizar o resultado
   de B′ para esse ficheiro.)*
2. reler o JSONL, revalidar, reprojetar e produzir as ações **em streaming**,
   batch a batch (§18), **recalculando o digest** como subproduto e contando
   `actions_produced`;
3. no fim da leitura, exigir **ambas** as pós-condições:
   - `digest_fase_c == digest_fase_a`;
   - `actions_produced == expected_count`;
   qualquer divergência ⇒ **`InputError`** e o gate desse record type falha
   (a escrita desse tipo pode já ter ocorrido — ver §8.3).

**Fase D — verificação (§22):** refresh, `count`, round-trip da amostra, alias
inalterado.

### 8.2 Ordem para `index` (os quatro record types)

```
A(element) → A(property_fact) → A(classification_fact) → A(document)
    ↓  qualquer ok=false ⇒ zero escrita remota, exit 1
B(element) → B(property_fact) → B(classification_fact) → B(document)
    ↓  qualquer falha ⇒ zero escrita remota, exit 1
B′(os quatro digests)
    ↓  qualquer divergência ⇒ zero escrita remota, exit 1
C(element) → D(element) → C(property_fact) → D(property_fact)
    → C(classification_fact) → D(classification_fact) → C(document) → D(document)
```

**Todas** as validações locais correm antes de todos os preflights, **todos** os
preflights antes de B′, e B′ antes da primeira ação bulk. Isto elimina escrita
parcial causada por JSON inválido, `schema_version` errada, record type errado,
duplicados, projeção inválida **ou mutação do input entre a validação e a
escrita** — inclusive na última linha do quarto ficheiro, e inclusive quando o
quarto ficheiro é alterado depois de os três primeiros terem sido validados.

Os estados finais possíveis por target em caso de falha estão enumerados em §20.

### 8.3 O que as duas passagens garantem — e o que não garantem

**Garantido:** nenhum problema **pré-existente** do input (sintaxe, schema,
duplicados, projeção, ranges) e nenhuma mutação ocorrida **antes da primeira
escrita** produz qualquer escrita remota.

**Não garantido:** uma alteração externa **concorrente com a própria leitura da
Fase C** ainda pode causar escrita parcial desse record type. Esse caso:

- é **detetado** no próprio record type — pelo digest final divergente, por
  `actions_produced != expected_count`, ou por erro de validação na releitura;
- é uma **falha de runtime** (`InputError`), com gate falhado e exit 1;
- **nunca** é visível através de aliases: o target não é promovido;
- o rerun com o input correto **converge** (`_op_type=index` + `_id` canónico);
- o diretório de input é contratualmente imutável durante a execução (§7.2) —
  este caminho é violação de contrato detetada, não comportamento suportado.

O **round-trip (§22) não deteta mutações do input** e não é apresentado como se
o fizesse: valida apenas `_id`, `_source`, mapping, transporte e o documento
efetivamente indexado. A estabilidade do input é responsabilidade exclusiva do
digest.

Falhas de **rede/bulk** durante a Fase C também podem produzir escrita parcial;
gestão em §18.3 e §20.

### 8.4 Custo

A Fase C repete parse + validação + projeção; a Fase B′ acrescenta uma releitura
só-digest. É deliberado: é o preço de "zero escrita com input inválido ou
mutado" sem materializar o dataset. O caminho de código de leitura/validação/
projeção é **exatamente o mesmo** nas duas passagens (uma única função
`iter_projected(spec, path)`), pelo que não há risco de divergência entre elas.

---

## 9. Validação canónica

### 9.1 Política

**Caminho feliz:** `Model.model_validate_json(line)` — uma única passagem de
parse, um único tipo de erro, sem `dict` intermédio (a projeção parte do modelo
validado).

**Caminho de erro:** só **depois** de um `ValidationError`, um diagnóstico
controlado distingue as duas causas:

```
try:
    record = spec.model.model_validate_json(line)
except ValidationError:
    try:
        json.loads(line)
    except (json.JSONDecodeError, ValueError):
        -> falha classificada como RecordParseError(record_type, line_number)
    -> senão, RecordValidationError(record_type, line_number)
```

Custo zero no caminho feliz; diagnóstico preciso na falha. **O conteúdo da linha
nunca entra na mensagem de erro.** Na Fase A este resultado é **registado** no
`InputValidationResult` (§9.4), não levantado; a exceção tipada é criada pelo
orquestrador no fim.

### 9.2 Equivalência verificada

A auditoria comparou empiricamente `model_validate_json(line)` com
`json.loads(line)` + `model_validate(...)` no schema real com `pydantic 2.12.5`
(o pin exato de `backend/requirements.txt`). Os dois caminhos são
**semanticamente idênticos** em todos os casos relevantes:

| Caso | `model_validate_json` | `json.loads` + `model_validate` |
|---|---|---|
| `{"value_type":"float","value":15.0}` | OK `FloatPropertyValue` | OK `FloatPropertyValue` |
| `{"value_type":"float","value":15}` (int JSON) | **ValidationError** | **ValidationError** |
| `{"value_type":"int","value":3.0}` | **ValidationError** | **ValidationError** |
| `{"value_type":"int","value":"3"}` | **ValidationError** | **ValidationError** |
| `{"value_type":"int","value":true}` (bool ≠ int) | **ValidationError** | **ValidationError** |
| `NaN` / `Infinity` / `1e400` | **ValidationError** | **ValidationError** |
| chave desconhecida (`extra="forbid"`) | **ValidationError** | **ValidationError** |
| `2**70` num slot `int` | **OK** (⚠ ver §14) | **OK** (⚠ ver §14) |
| chaves JSON duplicadas | *last-wins* silencioso | *last-wins* silencioso |

A escolha de `model_validate_json` é, portanto, uma decisão de **eficiência e
simplicidade**, não de semântica. Um teste offline **replica esta tabela** para
que uma futura mudança de versão do Pydantic que quebre a equivalência falhe o
CI (§25).

### 9.3 Risco residual assinalado

**Chaves JSON duplicadas na mesma linha** resolvem-se por *last-wins* silencioso
em **ambos** os caminhos. Detetá-lo exigiria um parser com `object_pairs_hook`,
desproporcionado para o benefício: o produtor canónico nunca as gera. Registado
em §33.

### 9.4 Interface de validação e relatórios em falha

A Fase A tem uma interface **que não levanta** por erros de conteúdo, para que o
relatório completo exista mesmo quando a validação falha:

```python
@dataclass(frozen=True)
class ValidationFailureRef:
    record_type: str
    line_number: int
    error_type: str            # "RecordParseError" | "RecordValidationError" |
                               # "ProjectionError" | "DuplicateRecordIdError" |
                               # "InputDecodeError"
    record_id: str | None      # _id quando conhecido; nunca conteúdo

@dataclass(frozen=True)
class InputValidationResult:
    record_type: str
    input_file: str            # nome do ficheiro (nunca path absoluto)
    lines_read: int
    lines_blank: int
    records_valid: int
    records_invalid: int
    duplicate_ids: int         # ocorrências além da primeira (§15)
    expected_count: int
    sample_ids: tuple[str, ...]
    digest: str                # hex SHA-256 (§7.2)
    failure_sample: tuple[ValidationFailureRef, ...]   # máx. 10, ordem de encontro
    first_error_type: str | None
    ok: bool
```

Contrato de `validate_input(spec, path) -> InputValidationResult`:

- **não levanta** por erros de linha, validação Pydantic, projeção, range ou
  duplicados — regista, conta e **continua até ao fim do ficheiro**;
- erros que impedem tecnicamente a continuação — I/O (`OSError`) ou UTF-8
  inválido (`UnicodeDecodeError`) — terminam o scan **desse ficheiro**, mas a
  função devolve na mesma um resultado **parcial** com `ok=false` e
  `first_error_type` preenchido;
- `failure_sample` guarda no máximo **10** entradas sanitizadas, por ordem de
  encontro (determinística);
- o mesmo `validate_input` serve `validate` e `index` — **um único caminho de
  código**, sem variante que aborte na primeira falha.

Orquestração:

1. recolhe os `InputValidationResult` de **todos** os record types pedidos, pela
   ordem `RECORD_TYPES`;
2. constrói os `IndexReport` (§21) a partir deles;
3. se **algum** tem `ok=false`: **não constrói cliente, não faz preflight, não
   escreve**; cria a exceção tipada correspondente ao **primeiro** erro (ordem:
   record type, depois linha) e **anexa os reports**;
4. a CLI **imprime sempre os reports** — humanos ou JSON (§23) — antes de mapear
   a exceção para exit 1.

A classe base transporta os relatórios:

```python
class IndexingError(Exception):
    reports: tuple[IndexReport, ...] = ()
```

O atributo contém **apenas** relatórios sanitizados (§21) — nunca records,
linhas, `_source` ou payloads de erro brutos.

---

## 10. Política `_id`

O `_id` do documento OpenSearch é o **campo de identidade do próprio record,
verbatim**:

| Record | `_id` | Fórmula do produtor (`canonical/ids.py`) |
|---|---|---|
| `ElementRecord` | `element_id` | `"el_" + sha256_128(project_id, global_id)` |
| `PropertyFact` | `fact_id` | `"pf_" + sha256_128(project_id, element_id, source, container, property_name, occurrence_key)` |
| `ClassificationFact` | `classification_id` | `"cf_" + sha256_128(project_id, element_id, system, code, occurrence_key)` |
| `DocumentRef` | `document_id` | `"doc_" + sha256_128(project_id, uri)` |

Regras absolutas:

- **Nunca recomputar.** `ingestion.indexers` **não importa `canonical.ids`**.
- **Nunca concatenar `project_id`.** Os quatro ids já incorporam `project_id` no
  hash, logo já são globalmente únicos entre projetos.
- **Nunca** usar `global_id` como `_id`.
- **Nunca** o padrão legacy `f"{project_id}_{id}"`
  (`index_to_opensearch.py:263`), que fica confinado ao indexer legacy.

### 10.1 Justificação decisiva

`ClassificationFact` **não tem campo `occurrence_key`** (`schema.py:212–227`), mas
`classification_id()` recebe-o como componente. Logo `classification_id` é
**impossível de recomputar** a partir do record. Recomputar seria não-uniforme
entre os quatro tipos e criaria uma segunda fonte de verdade para a identidade.
O id armazenado é a identidade.

### 10.2 Limites

`el_` + 32 hex = 35 caracteres; o mais longo (`doc_` + 32) tem 36. Muito abaixo
do limite de 512 bytes de `_id` do OpenSearch. Um teste offline assere o prefixo
e o comprimento dos quatro tipos.

---

## 11. Projeções

O mapping HBIM-020 descreve o **documento projetado**, não o JSONL canónico
(HBIM-020 §5). A cobertura de campos de cada mapping é **igual a `model_fields` do
record** — provado por `test_index_mappings.py::test_root_field_coverage_matches_model_fields`.

### 11.1 `element` → `elements_v1.json`

`model_dump(mode="json")` + poda de `None` (§13). Estrutura resultante:

```
schema_version, element_id, project_id, global_id, ifc_class,
name?, description?, object_type?, predefined_type?, semantic_label?,
materials: [ { name, name_norm?, role?, ordinal? } ... ],
location?: { site?|building?|storey?|space?|parent_element?: { global_id?, id?, name? } },
metrics?: { area?, volume?, height?, thickness? },
source: { source_id, ifc_schema?, checksum?, external_id?, revision? }
```

`materials` mantém a ordem já imposta pelo validador do modelo
(`sorted by (ordinal or 0, name)` — `schema.py:190–194`). O indexer **não
reordena**.

### 11.2 `classification_fact` → `classification_facts_v1.json`

`model_dump(mode="json")` + poda. Dez campos, identidade estrutural com
`ClassificationFact`. `location` é aqui um `keyword` escalar (`str | None`) — sem
colisão com o objeto `location` de `element`, porque os índices são separados.

### 11.3 `document` → `documents_v1.json`

`model_dump(mode="json")` + poda. Nove campos. `linked_element_ids` já vem
deduplicado e ordenado pelo validador (`schema.py:246–250`); é preservado como
array, **incluindo quando vazio** (§13).

### 11.4 `property_fact` → `property_facts_v1.json`

Ver §12. O objeto `value` **nunca é enviado**
(`test_index_mappings.py::FORBIDDEN_FIELD_NAMES` inclui `"value"`).

### 11.5 Garantia contra `dynamic: strict`

Nenhum campo desconhecido pode chegar a um mapping `dynamic:strict` porque a
projeção deriva de `model_fields` e o mapping **é** `model_fields`. Esta garantia
é verificada **estaticamente e offline** (§25): para cada record type, o conjunto
de paths de chave do doc projetado (recursivo, incluindo elementos de arrays) tem
de ser **subconjunto** dos paths declarados no mapping correspondente. Sem
OpenSearch, sem Docker.

---

## 12. PropertyFact

A parte crítica desta issue. As variantes reais de `PropertyValue`
(`schema.py:63–107`, união discriminada por `value_type`, modelos
`extra="forbid", strict=True, frozen=True`):

| Classe | `value_type` | Tipo de `value` | Campo projetado | Tipo OpenSearch |
|---|---|---|---|---|
| `TextPropertyValue` | `"text"` | `StrictStr` | `value_text` | `text` + `fields.keyword(256)` |
| `IntegerPropertyValue` | `"int"` | `StrictInt` (rejeita `bool` e strings numéricas) | `value_integer` | `long`, `coerce:false` |
| `FloatPropertyValue` | `"float"` | `float` + `_require_finite_float` (rejeita `int`/`bool`/NaN/±Inf) | `value_number` | `double`, `coerce:false` |
| `BooleanPropertyValue` | `"bool"` | `StrictBool` | `value_boolean` | `boolean` |
| `NullPropertyValue` | `"null"` | `None` | *(nenhum)* | — |

### 12.1 Projeção fechada

```
value_type    = record.value.value_type          # SEMPRE; "text"|"int"|"float"|"bool"|"null" verbatim
value_is_null = (value_type == "null")           # SEMPRE; bool literal
+ EXATAMENTE UM de: value_text | value_integer | value_number | value_boolean   (não-null)
+ ZERO payloads                                                                (null)
```

Campos escalares preservados **verbatim**, sem re-normalização:
`schema_version`, `fact_id`, `project_id`, `element_id`, `source` (`"pset"`/`"qto"`),
`container`, `property_name`, `property_name_norm`, `occurrence_key`, `unit?`.

> `property_name_norm` é *as-is*, sem dupla normalização (HBIM-020 §7). O indexer
> **não** normaliza nada.

### 12.2 Invariantes garantidas no código, antes de qualquer chamada ao OpenSearch

O mapping **não consegue** exprimir estas invariantes (HBIM-020 §5.1); são
responsabilidade desta issue:

1. `value_type` **sempre** presente.
2. `value_is_null` **sempre** presente (`False` para não-null, `True` para null).
3. `value_type == "null"` ⇒ **zero** payloads.
4. `value_type != "null"` ⇒ **exatamente um** payload (XOR).
5. Coerência `value_type` → payload (nunca `value_type="int"` com `value_text`).
6. **`bool` nunca tratado como `int`.** Garantido em dois níveis: `StrictBool`/
   `StrictInt` no schema, **e** despacho pelo discriminador `value_type` via
   `dict[str, str]` `value_type → nome do campo` — **nunca** por
   `isinstance(v, int)` (armadilha: `bool` é subclasse de `int`). O despacho por
   dicionário torna a armadilha estruturalmente impossível.
7. **`int` nunca tratado como `float`.** `IntegerPropertyValue` → `value_integer`
   (`long`); `FloatPropertyValue` → `value_number` (`double`). Nunca partilham
   campo — é por isso que HBIM-020 §7 manda consultar **ambos** em ranges.
8. **`float` sempre finito.** Já garantido por `_require_finite_float`; a projeção
   reafirma-o (defesa em profundidade, custo desprezável).

Qualquer violação ⇒ `ProjectionError`, na Fase A, **antes** de qualquer bulk.

### 12.3 Preservação de informação

`value_type` + (o único payload presente, ou `value_is_null`) reconstrói o `value`
canónico exatamente. Confirma HBIM-020 §5: o `_source` do doc projetado basta;
**não** se duplica o objeto `value` original. Sobre os limites exatos desta
afirmação para os restantes records, ver §13.1.

---

## 13. Null e omissão

**Uma regra recursiva única:**

> **Podar apenas `None`** (comparação `is None`, nunca *truthiness*). Depois,
> **omitir qualquer valor de campo-objeto que tenha ficado `{}`**. Valores
> *falsy* que não sejam `None` (`False`, `0`, `0.0`, `""`, `[]`) são
> **preservados**.

Precisões obrigatórias:

- A omissão de `{}` aplica-se **apenas a valores de campos objeto**
  (`location`, `location.site`, …, `metrics`, `source`).
- **Listas vazias são preservadas** (`materials: []`, `linked_element_ids: []`).
- **Elementos de listas nunca são silenciosamente removidos.** Se um elemento de
  lista ficar `{}` após a poda, isso é **`ProjectionError`** (fail-closed). Este
  cenário é atualmente **inalcançável** para `MaterialRef` — `name` é
  `NonEmptyStr` obrigatório — mas a regra permanece fail-closed para resistir a
  evolução futura do schema; o teste alimenta o helper de poda com um dict
  sintético (§25).

Consequências (todas testadas):

| Entrada canónica | Doc projetado |
|---|---|
| `"description": null` | campo **ausente** |
| `"location": {"site":null, …, "parent_element":null}` | `location` **ausente** |
| `"metrics": {"area":null,"volume":null,"height":null,"thickness":null}` | `metrics` **ausente** |
| `"metrics": {"area":12.0,"volume":null,…}` | `{"area":12.0}` |
| `"materials": []` | `[]` **preservado** |
| `"linked_element_ids": []` | `[]` **preservado** |
| `"value_is_null": false` | **preservado** (crítico) |
| `"value_boolean": false` | **preservado** (crítico) |
| `"materials":[{"name":"x","ordinal":0,…}]` | `ordinal: 0` **preservado** |

### 13.1 Preservação de informação (formulação exata)

> A projeção preserva toda a informação semanticamente transportada, **à exceção
> da equivalência intencional entre um sub-objeto ausente e um sub-objeto cujos
> campos estão todos a `None`**.

Esta classe de equivalência é **real e alcançável**: `ifc_spatial.py:117–125`
produz `SpatialRef(global_id=None, id=None, name=None)` quando o nó espacial
existe mas não tem `GlobalId` nem `Name` utilizável. `storey=None` e
`storey=SpatialRef()` projetam para o **mesmo** documento — ambos os valores não
transportam informação, e a reconstrução canónica (`SpatialLocation()`/
`Metrics()`/`SpatialRef()` a partir da ausência) é semanticamente equivalente.
Um teste offline documenta explicitamente esta classe de equivalência (§25).

### 13.2 Justificação

1. É exatamente o que HBIM-020 §5 já manda para os payloads não selecionados
   ("payloads dos outros tipos são omitidos").
2. É o **precedente estabelecido**: `test_index_mappings_apply.py` escreve docs
   com opcionais **omitidos** e assere `_source == doc`.
3. `_source` menor e sem ruído.

### 13.3 Alternativa considerada e rejeitada

Enviar `null` verbatim (`model_dump(mode="json")` puro, sem poda). Também é aceite
pelo `dynamic:strict` (um `null` num campo declarado **não** é rejeitado, e
`coerce:false` **não** é acionado por `null`) e evita a classe de equivalência de
§13.1 — mas contraria o precedente HBIM-020 e enche o `_source` de nulls.
Registada aqui para que a decisão seja auditável.

---

## 14. Ranges numéricos

O Pydantic valida tipos, **não** os limites dos tipos OpenSearch. A auditoria
verificou empiricamente que `{"value_type":"int","value":2**70}` **valida com
sucesso** em ambos os caminhos Pydantic — mas o mapping declara `value_integer:
long` (int64), pelo que o OpenSearch rejeitaria com um `mapper_parsing_exception`
opaco, **remoto e tardio**, no meio de um bulk.

A projeção valida localmente, na Fase A:

| Campo | Tipo OpenSearch | Range validado |
|---|---|---|
| `value_integer` | `long` | `-(2**63) <= v <= 2**63 - 1` |
| `materials.ordinal` | `integer` | `0 <= v <= 2**31 - 1` (o schema já garante `>= 0`) |

Violação ⇒ **`ProjectionError`** com `record_type`, `line_number` e `_id`, **antes
de qualquer bulk**. Nunca o valor.

**Completude verificada** (auditoria sobre os quatro mappings committed):
`value_integer` é o **único** campo `long` e `materials.ordinal` é o **único**
campo `integer` em todos os quatro mappings. Não existem outros campos
`integer`/`long` que exijam range guard nesta issue. Um teste offline lê os
quatro JSON e assere esta unicidade, para que um mapping futuro com novos campos
inteiros falhe o teste e force a extensão do guard (§25).

`value_number` e `metrics.*` são `double` e o `float` de Python **é** um double
finito (garantido por `_require_finite_float`) — não precisam de validação de
range adicional.

---

## 15. Duplicados

**IDs duplicados dentro de qualquer JSONL são sempre erro fail-closed.** Semântica
única, igual para `validate` e `index` (mesmo caminho de código — §9.4):

- A Fase A **percorre sempre o ficheiro inteiro**; **não** levanta na primeira
  colisão.
- Deteção exata com um `set[str]` dos `_id` já vistos; cada ocorrência **além da
  primeira** incrementa `duplicate_ids`.
- **`duplicate_ids` = número de ocorrências além da primeira.** Exemplo:
  `A, A, A, B, B` ⇒ `duplicate_ids = 3` (duas ocorrências adicionais de `A`,
  uma de `B`).
- A validação desse ficheiro é marcada `ok=false` **só depois** de o ficheiro
  terminar; o orquestrador cria então **`DuplicateRecordIdError`** (§9.4) — com
  **zero escrita** em qualquer target.
- Retido para o relatório/mensagem: contagem total, **primeiro** `_id` duplicado
  e **primeira** linha de colisão. **Nunca** conteúdo.
- **Não existe `--allow-duplicate-ids`.** Não existe modo permissivo nesta issue.
- Precedente: HBIM-012 já falha fechado em `FactIdCollisionError`.

**Custo de memória:** O(nº de records), não O(bytes do ficheiro) — o conjunto de
ids de ≤36 caracteres é ordens de grandeza menor do que "carregar o ficheiro".
Documentado em §33. O `set` da Fase A é também a fonte da amostra determinística
de verificação (§22), pelo que não é estrutura extra.

Como duplicados invalidam a execução, vale sempre a identidade
`expected_count == records_valid - duplicate_ids == número de _id únicos` — e em
qualquer execução que escreva, `duplicate_ids == 0` e
`expected_count == records_valid`.

---

## 16. Targets físicos

O utilizador fornece **apenas** `record_type` e `physical_version`. O nome físico
é **sempre composto**:

```python
physical = il.physical_index_name(record_type, physical_version)   # valida rt e versão
```

Regras absolutas:

- **Nunca** aceitar um nome de índice arbitrário (nem por flag, nem por env).
- **Nunca** escrever através do **alias** — sempre no índice **físico**.
- **Nunca** criar índices (é HBIM-021 `migrate create`/`create-all`).
- **Nunca** apagar índices, documentos ou aliases.
- **Nunca** promover aliases (não existe subcomando `promote`; nenhuma chamada a
  `indices.update_aliases`, `put_alias` ou `delete_alias`).
- **Nunca** reparar aliases em conflito — deteção e recusa apenas (§4).

### 16.1 Preflight remoto (Fase B), fail-closed, por esta ordem

Para cada record type pedido:

1. `physical = il.physical_index_name(record_type, physical_version)`
2. `client.indices.exists(index=physical)` → senão **`MissingTargetIndexError`**
3. `effective = client.indices.get_mapping(index=physical)[physical]["mappings"]`
4. `effective.get("_meta", {}).get("record_type") != record_type` →
   **`TargetRecordTypeMismatchError`**
5. `not il.is_mapping_compatible(il.load_mapping(record_type), effective)` →
   **`IncompatibleTargetMappingError`**
6. `st = il.status(client, record_type)[0]`. **Se `st.conflicts` não estiver
   vazio** (`multiple_targets`, `alias_concrete_index_collision`,
   `record_type_mismatch`, `incompatible_mapping` — qualquer um) →
   **`TargetIndexError`** fail-closed. Um alias em conflito **nunca** é
   interpretado como ausente, **nunca** é interpretado como "target não-live", e
   **nenhum target de nenhum record type recebe escrita** (a Fase B completa
   antes de B′/C — §8.2). *(Nota: `alias_missing` em `st.conflicts` não é um
   conflito — significa apenas que o alias ainda não existe; segue para 7.)*
7. **Enumeração real dos targets do alias** — a única fonte autoritativa do
   estado live:
   ```python
   try:
       alias_targets = sorted(client.indices.get_alias(name=spec.alias).keys())
   except NotFoundError:
       alias_targets = []          # alias ausente ⇒ conjunto vazio
   ```
   O target é **live** sse `physical in alias_targets` (§17). Live sem os dois
   flags → **`LiveTargetError`**.
8. **Snapshot do alias** para §22.6: o par
   `(alias_targets, st.is_write_index)`.
9. Se `--require-empty` e `client.count(index=physical)["count"] != 0` →
   **`TargetNotEmptyError`**.

### 16.2 APIs usadas — fronteira exata

- **Naming, mappings committed, compatibilidade e status** usam exclusivamente a
  superfície **pública** de `index_lifecycle`: `RECORD_TYPES`, `get_spec`,
  `physical_index_name`, `validate_physical_version`, `load_mapping`,
  `is_mapping_compatible`, `status`, `AliasStatus` e as constantes `CONFLICT_*`.
- **A enumeração completa dos targets do alias** (16.1.7) usa a API **pública do
  cliente** `client.indices.get_alias` — porque `AliasStatus.current_target` é
  `None` tanto para "alias ausente" como para "alias com múltiplos targets"
  (`index_lifecycle.py:647–651`) e **não chega** para decidir live.
- **Nenhum helper privado** de `index_lifecycle` (`_alias_targets`,
  `_effective_mapping`, `_assert_compatible`, …) é usado, e as exceções da
  HBIM-021 não são reutilizadas para condições da HBIM-022 (§24).

**Consequência: `backend/ingestion/index_lifecycle.py` fica byte-idêntico** (§30).

---

## 17. Targets live

Um índice físico é **live** quando pertence ao **conjunto real de targets** do
alias correspondente: `physical in alias_targets`, com `alias_targets` enumerado
por `client.indices.get_alias` (§16.1.7). `NotFoundError` ⇒ conjunto vazio.

**A determinação de live só acontece depois de o preflight confirmar
`st.conflicts` vazio** (§16.1.6). Semântica completa, todas as combinações:

| Estado do alias | Flags | Resultado |
|---|---|---|
| Sem conflitos; `physical ∉ alias_targets` (alias ausente) | nenhuns | prossegue sem confirmação |
| Sem conflitos; alias aponta para **outro** físico | nenhuns | **não live** — prossegue sem confirmação |
| Sem conflitos; `physical ∈ alias_targets` | nenhuns | **`LiveTargetError`** → exit 1 |
| Sem conflitos; `physical ∈ alias_targets` | `--allow-live-target --yes` | permitido |
| Qualquer estado | só `--allow-live-target` | **exit 2** (erro de utilização, antes de qualquer cliente) |
| Qualquer estado | só `--yes` | **exit 2** (idem) |
| `st.conflicts` ⊇ `{multiple_targets}` — mesmo que `physical` seja um dos targets | quaisquer | **`TargetIndexError`** → exit 1; **nunca** interpretado como "não live" |
| `st.conflicts` ⊇ `{alias_concrete_index_collision}` | quaisquer | **`TargetIndexError`** → exit 1 |

- Um alias que aponta para outro índice físico **não** torna o target pedido
  live.
- Um alias ausente significa que **nenhum** target está live.
- Targets físicos **ainda não promovidos** não precisam de qualquer confirmação
  (escrever neles não é visível a nenhum consumidor do alias) — coerente com
  HBIM-021, onde `create`/`create-all` não pedem confirmação e só
  `promote`/`rollback` pedem.
- `--dry-run` e `validate` **nunca constroem cliente** e **nunca verificam estado
  remoto**; a verificação de live **não se aplica** e o output declara
  explicitamente que o estado remoto **não foi consultado** (precedente
  `migrate.py::_emit_create_plan`).

### 17.1 Fluxo operacional recomendado

```
1. python -m ingestion.migrate   create-all --physical-version N
2. python -m ingestion.indexers  index --input-dir DIR --physical-version N
3. (verificação automática dentro do passo 2)
4. python -m ingestion.migrate   promote-all --physical-version N --yes
```

A indexação acontece **sempre** num físico não promovido; a promoção é um ato
separado e explícito. `--allow-live-target --yes` existe para correções
deliberadas (p.ex. reindexar um lote em falta num índice já promovido), nunca
como fluxo normal — e é a **única** exceção operacional em que um estado parcial
poderia ser visível através de um alias (§20).

---

## 18. Bulk

`opensearchpy.helpers.streaming_bulk`, com contrato fixo (semântica confirmada no
código **instalado** de `opensearch-py 3.1.0`, `helpers/actions.py`):

| Parâmetro | Valor | Semântica real verificada |
|---|---|---|
| `raise_on_error` | **`False`** | Impede o `BulkIndexError` construído a partir de **respostas de itens**; os itens falhados são cedidos individualmente |
| `raise_on_exception` | **`False`** | Converte **apenas `TransportError` e subclasses** (`ConnectionError`, `ConnectionTimeout`, `SSLError`) em erros por-ação. **Não** converte todas as exceções — ver §18.3 |
| `yield_ok` | **`False`** | O helper cede **apenas falhas**; sucessos não produzem eventos |
| `chunk_size` | = tamanho do batch externo (`--batch-size`, default **500**) | Determinístico |
| `max_chunk_bytes` | **`10 * 1024 * 1024`** (10 MiB), explícito | ⚠ O default da biblioteca é **100 MB**, exatamente igual ao `http.max_content_length` default do OpenSearch — um chunk no limite seria rejeitado. Pode subdividir um batch externo em vários pedidos HTTP; o accounting externo (§18.1) mantém-se exato |
| `max_retries` | **3** | Só reenvia itens `429`; cada ação falhada é cedida **no máximo uma vez**, após os retries (verificado: os 429 em attempts não-finais vão para `to_retry` e não são cedidos) |
| `initial_backoff` | **2** | ⚠ `time.sleep` **bloqueante** — os testes nunca exercitam retries reais (§25) |
| `max_backoff` | **60** | |
| `request_timeout` | `--request-timeout`, default **60** s, passado por `**kwargs` | Propagado até `client.bulk` (verificado) |
| `_op_type` | **`"index"`** (default de `expand_action`) | Upsert por `_id` ⇒ rerun idempotente (§19) |
| `refresh` | **nunca** por request | Um único `indices.refresh(target)` no fim (§22) |

Forma da ação: `{"_index": <físico>, "_id": <id canónico>, "_source": <doc projetado>}`.

**Zero ações ⇒ `streaming_bulk` não é chamado** (§7.3).

### 18.1 Batching externo e accounting

As ações **não** são entregues como um único gerador. `common.run_bulk` agrupa-as
em batches de `batch_size` e faz **uma chamada `streaming_bulk` por batch**,
consumindo o gerador até ao fim antes de creditar contagens:

```
para cada batch (tamanho real = actual_batch_size ≤ batch_size):
    batch_failures = consumo iterativo do helper (§18.2)
    # SÓ apos a iteração terminar normalmente:
    batch_successes  = actual_batch_size - batch_failures
    records_indexed += batch_successes
    records_failed  += batch_failures
    bulk_batches    += 1
```

Esta aritmética é **válida** porque, com os flags de §18, cada ação falhada é
cedida **no máximo uma vez** depois dos retries (verificado no código
instalado) — logo `actual_batch_size - batch_failures` conta exatamente as ações
não-falhadas do batch.

**Definição normativa:**

> **`records_indexed` é o número de ações contabilizadas como sucesso em batches
> cuja iteração do helper terminou normalmente nesta execução.**

**Não** se afirma que cada sucesso foi individualmente confirmado —
`yield_ok=False` não produz eventos de sucesso; a contagem é por diferença, por
batch concluído. A verdade independente é o `count` do servidor (§22).

Num **batch interrompido** (exceção ou interrupção a meio da iteração):

- `records_indexed` recebe **zero** desse batch;
- `bulk_batches` **não** incrementa;
- documentos desse batch **podem ter sido aplicados remotamente**;
- os batches concluídos anteriormente mantêm o seu accounting;
- `records_indexed` é, portanto, um **limite inferior** da execução parcial, com
  subestimação **limitada ao tamanho do batch em voo**.

Memória: O(`batch_size`) ações — o mesmo custo que a biblioteca já teria num
chunk.

### 18.2 Consumo iterativo dos erros (obrigatório)

**Proibido** materializar o output do helper (nenhum `list(streaming_bulk(...))`,
nenhuma acumulação dos dicts). Motivo verificado no código instalado: no caminho
`TransportError`, **cada** dict de erro contém `data` (o `_source` **completo** do
documento), `exception` (o objeto de exceção **vivo**) e `error = str(exc)` (que
pode embeber a resposta do servidor). Materializá-los viola memória **e**
segurança (§29).

Consumo obrigatório — iterativo, com sanitização imediata e descarte:

```python
batch_failures = 0
for _ok, raw_info in streaming_bulk(client, batch, **BULK_KWARGS):  # só falhas (yield_ok=False)
    batch_failures += 1
    if len(failure_sample) < _MAX_FAILURE_SAMPLE:                   # 10
        op, item = next(iter(raw_info.items()))
        error = item.get("error")
        failure_sample.append({
            "_id": item.get("_id"),
            "status": item.get("status"),
            # resposta normal: error é um dict com "type";
            # caminho TransportError: error é uma string -> tipo fixo
            "error_type": error.get("type") if isinstance(error, dict) else "transport_error",
        })
    del raw_info                                                     # descarta data/exception/reason
```

Regras absolutas:

- só sobrevivem **`_id`**, **`status`** e **`error_type`**;
- `failure_sample` contém apenas as **primeiras 10** falhas, por ordem de
  encontro (determinística, porque a ordem das ações é determinística);
- `records_failed` conta **todas** as falhas;
- **proibido**: guardar os dicts brutos; converter `raw_info` para `str`/`repr`;
  guardar `raw_info[...]["data"]`; guardar `raw_info[...]["exception"]`; guardar
  `reason`; guardar `caused_by`; guardar `_source`; guardar bodies de pedido ou
  resposta; **anexar dicts brutos a exceções**; **logar dicts brutos**.

### 18.3 Exceções fora do contrato por-item

Semântica real (verificada): `_process_bulk_chunk` só apanha **`TransportError`**.
`SerializationError` **não** é subclasse de `TransportError` e é levantada em
`_ActionChunker.feed`, durante a serialização do chunk, **fora** do bloco
protegido — **propaga sempre**. Outras exceções inesperadas também propagam.

`run_bulk` envolve por isso **toda** a iteração do helper:

```python
try:
    for _ok, raw_info in streaming_bulk(...):
        ...                                   # §18.2
except Exception as exc:
    raise BulkIndexingError(
        error_type=type(exc).__name__,        # NUNCA str(exc)
        reports=partial_reports,              # sanitizados (§21)
    ) from None                               # sem __cause__: o objeto bruto não viaja
```

- **Nunca** `str(exc)` — apenas o nome da classe.
- `from None` é deliberado: a exceção original (que pode transportar payloads e
  bodies) **não** é anexada nem via `__cause__` nem via traceback impresso.
- O batch em voo credita **zero** e `bulk_batches` não incrementa (§18.1).

**`KeyboardInterrupt`** é tratado explicitamente pela orquestração/CLI:

- produz o **relatório parcial** (todos os record types pedidos, com `state`
  refletindo o ponto de interrupção — §21);
- assume o target do record type em curso como **potencialmente parcial**;
- o alias permanece **inalterado**;
- devolve **exit 1**;
- **não** imprime traceback nem conteúdo sensível.

---

## 19. Idempotência

O target **não tem de estar vazio por defeito**: um rerun depois de uma
interrupção tem de ser possível.

- **`--require-empty`** é um guard **opcional**: assere `count(físico) == 0` no
  preflight (§16.1.9) e falha com **`TargetNotEmptyError`** caso contrário.
  Serve para cargas limpas garantidas.
- **`_op_type=index` + `_id` canónico** ⇒ rerun substitui o mesmo documento; não
  cria duplicados; a mesma entrada converge para a mesma contagem. O `_version`
  interno incrementa e é irrelevante.
- **Documentos extra no target** (ids que não estão no JSONL) **não são
  apagados** — a proibição de delete é absoluta (§4, §16). São detetados por
  `actual_count != expected_count` ⇒ **`VerificationError`**, exit 1, relatório
  com o delta.
- **Remediação para documentos extra:** criar uma **nova versão física**
  (`migrate create --physical-version N+1`) e reindexar do zero. Nunca delete,
  nunca `delete_by_query`.

---

## 20. Falhas parciais e estados finais por target

| Origem | Momento | Efeito |
|---|---|---|
| Input inválido (JSON, UTF-8, schema, record type, projeção, range, duplicados) | Fase A | **Zero escrita remota.** Exit 1, com reports (§9.4) |
| Target ausente / record type errado / mapping incompatível / **alias em conflito** / live / não vazio | Fase B | **Zero escrita remota** em qualquer target. Exit 1 |
| Digest divergente | Fase B′ ou pré-C | **Zero escrita** (B′) ou **zero escrita nesse target** + abort (pré-C). Exit 1 |
| Digest final ou `actions_produced` divergentes | Fim da Fase C | Escrita desse tipo já ocorreu; gate falha; **abort**. Exit 1 |
| Item rejeitado pelo OpenSearch | Fase C | Contado em `records_failed`; a execução desse tipo **continua** até ao fim do input; gate final falha ⇒ **abort** após D desse tipo. Exit 1 |
| `TransportError` num chunk | Fase C | Convertido em erros por-item (§18); idem |
| `SerializationError` / exceção inesperada | Fase C | `BulkIndexingError` sanitizado; batch em voo credita zero; **abort**. Exit 1 |
| `KeyboardInterrupt` | Qualquer | Relatório parcial; alias intacto; exit 1 (§18.3) |
| Contagem, round-trip ou alias divergentes | Fase D | **`VerificationError`**; **abort**. Exit 1 |

### 20.1 Ordem e estados finais (explícito)

A ordem é `C(rt) → D(rt)` intercalada (§8.2). Consequências enumeradas:

- **Falha em `D(record_type)` aborta imediatamente** a execução. Os record types
  seguintes **não recebem escrita** — os seus targets ficam exatamente como
  estavam (vazios, no fluxo normal pós-`create-all`).
- **Falha em `C(document)`** depois de três tipos passarem ⇒ três targets
  **completos e verificados**, o target de documentos **parcial ou vazio**.
- **Falha em `D(element)`** ⇒ o target de elements pode estar **completo ou
  parcial** (a verificação é que falhou), e os outros três ficam **intactos**.
- **Nenhum target é promovido automaticamente**, portanto **nenhum estado
  parcial é visível através de aliases** ainda não promovidos. A única exceção
  operacional é um target live deliberadamente permitido com
  `--allow-live-target --yes` (§17.1) — nesse caso o operador aceitou
  explicitamente esse risco.
- **Rerun com o mesmo input converge** (`_op_type=index` + `_id` canónico).

### 20.2 `state` por record type

O relatório (§21) inclui, por record type, um campo `state` com um destes
valores:

```
not_started | validated | preflighted | indexing | indexed | verified | failed
```

- `state` regista o **estádio mais avançado alcançado**; `failed` marca o record
  type onde a execução abortou (o estádio atingido é dedutível dos contadores e
  do objeto de erro).
- Record types **não executados** por abort anterior: `state="not_started"`,
  `ok=false`, `records_indexed=0`, `bulk_batches=0`.
- Numa execução completa com sucesso, todos terminam `state="verified"`.
- O output contém **sempre** reports para **todos** os record types pedidos
  (§21) — incluindo os `not_started`.

---

## 21. Reporting

Relatório **determinístico**: chaves ordenadas, **sem timestamps**, sem valores
voláteis, sem segredos. Precedente: `index_lifecycle.status_to_json`.

**Regra única de presença:** todos os campos definidos estão **sempre
presentes**; um campo não aplicável tem o valor **`null`** (nunca é omitido).

### 21.1 Campos por record type (17)

```json
{
  "record_type": "property_fact",
  "state": "verified",
  "target_index": "hbim_property_facts_v1",
  "input_file": "property_facts.jsonl",
  "lines_read": 812,
  "lines_blank": 0,
  "records_valid": 812,
  "records_invalid": 0,
  "duplicate_ids": 0,
  "records_indexed": 812,
  "records_failed": 0,
  "expected_count": 812,
  "actual_count": 812,
  "batch_size": 500,
  "bulk_batches": 2,
  "dry_run": false,
  "ok": true
}
```

O output final é a **lista ordenada** por `il.RECORD_TYPES` dos relatórios de
**todos** os record types pedidos. `target_index` é um nome composto pelo
registry — não é segredo. **Nunca** aparecem host, porta, utilizador, password,
URL ou body. A amostra de falhas (§18.2), quando existir, acompanha o relatório
com apenas `_id`/`status`/`error_type` por entrada.

### 21.2 Semântica exata de `records_indexed`

> **`records_indexed` é o número de ações contabilizadas como sucesso em batches
> cuja iteração do helper terminou normalmente nesta execução** (§18.1) — não
> `records_valid - records_failed`, e não uma confirmação individual por
> documento.

- Execução completa: `records_indexed + records_failed == records_valid`.
- Execução interrompida: o batch em voo credita **zero** ⇒ `records_indexed` é
  um **limite inferior**, com subestimação limitada ao tamanho do batch em voo;
  `bulk_batches` conta apenas batches **concluídos**.
- `records_invalid` só é não-zero em relatórios de `validate`/Fase A falhada —
  uma execução que chegou a escrever tem sempre `records_invalid == 0`.

Em `--dry-run` e `validate`: `records_indexed = 0`, `bulk_batches = 0`,
`actual_count = null`, `state = "validated"` (ou `"failed"`), `dry_run = true`
(em `validate`: `dry_run = null`), e o output declara que o estado remoto **não
foi consultado**.

### 21.3 Gates de sucesso

`ok` é `true` **só** se, para esse record type:

```
records_invalid  == 0
duplicate_ids    == 0
records_failed   == 0
records_indexed  == records_valid
actual_count     == expected_count   (== records_valid)
digest fase C    == digest fase A    e    actions_produced == expected_count
round-trip da amostra determinística OK
alias inalterado (snapshot §16.1.8 == estado pós-indexação)
state            == "verified"
```

Exit `0` só se **todos** os record types pedidos tiverem `ok == true`.
Em `--dry-run`/`validate`, os gates aplicáveis são apenas os locais
(`records_invalid == 0`, `duplicate_ids == 0`), com `state == "validated"`.

### 21.4 Relatórios em falha

A interface que garante relatório completo mesmo em falha está definida em §9.4:
`validate_input` nunca levanta por conteúdo; o orquestrador constrói os
`IndexReport` e anexa-os a `IndexingError.reports`; a CLI imprime-os **sempre**
antes de devolver exit 1 (§23).

---

## 22. Verificação

Depois do bulk, para cada record type (Fase D):

1. **`client.indices.refresh(index=<físico>)`** — um único refresh; sem isto o
   `count` fica atrasado e o gate falharia espuriamente.
2. **`client.count(index=<físico>)["count"]`** → `actual_count`.
3. **`actual_count == expected_count`** → senão `VerificationError`.
4. **Round-trip por `_id`** de uma **amostra determinística** (§22.5). Qualquer
   um destes casos ⇒ **`VerificationError`** (com `record_type`, `_id` e
   `target_index`):
   - `NotFoundError` do `client.get`;
   - resposta com `found == false`;
   - resposta **sem `_source`**;
   - `_source` **diferente** da projeção esperada.
5. **Comparação exata** do `_source`: igualdade estrutural completa; **arrays
   preservam ordem** (a ordem de `materials` e `linked_element_ids` é imposta
   pelos validadores canónicos e preservada pela projeção).
6. **Alias inalterado**: recomputar `st = il.status(client, record_type)[0]` e a
   enumeração `client.indices.get_alias` (§16.1.7); o par
   `(alias_targets, st.is_write_index)` tem de ser **idêntico** ao snapshot da
   Fase B (§16.1.8), e `st.conflicts` tem de continuar sem conflitos. Qualquer
   diferença → `VerificationError`.

**O round-trip não substitui o digest de estabilidade** (§7.2/§8.3): valida o
documento efetivamente indexado contra a projeção da Fase C, não a fidelidade do
input à Fase A.

### 22.5 Amostra determinística

Calculada na **Fase A**, a partir do conjunto ordenado de `_id`
(`ordered = sorted(ids)`):

- `len(ordered) == 0` → amostra vazia; passos 4–5 **saltados**;
- `len(ordered) <= 3` → **todos**;
- caso contrário → `{ordered[0], ordered[len(ordered) // 2], ordered[-1]}`
  (primeiro, mediano por ordenação lexical, último).

**Nunca** seleção aleatória, nunca dependente do relógio, nunca dependente da
ordem de ficheiro. A amostra é reproduzível a partir do input.

O `_source` esperado para os records da amostra é reconstruído na Fase C (quando
a linha correspondente é reprojetada) e retido apenas para os ≤ 3 ids da amostra
— memória O(1). A fidelidade da Fase C ao input validado é garantida pelo
digest (§8.1), não pelo round-trip.

---

## 23. CLI

```
python -m ingestion.indexers validate \
  --input-dir DIR [--record-type RT] [--json]

python -m ingestion.indexers index \
  --input-dir DIR --physical-version N \
  [--batch-size 500] [--request-timeout 60] \
  [--require-empty] [--allow-live-target --yes] \
  [--dry-run] [--json]

python -m ingestion.indexers index-one \
  --input-dir DIR --record-type RT --physical-version N \
  [--batch-size 500] [--request-timeout 60] \
  [--require-empty] [--allow-live-target --yes] \
  [--dry-run] [--json]
```

| Aspeto | Regra |
|---|---|
| Subparsers | `dest="command", required=True` |
| `--record-type` | restrito a `il.RECORD_TYPES` (`choices=`) |
| `--physical-version` | inteiro positivo, mesma função de validação que `migrate.py::_positive_int` |
| `--batch-size` | inteiro positivo, default `500` |
| `--request-timeout` | inteiro positivo, default `60` |
| `validate` | **nunca** constrói cliente. **Sem `--record-type`, exige os quatro ficheiros presentes** e valida os quatro; com `--record-type`, só esse. Percorre sempre os ficheiros até ao fim (§9.4) |
| `--dry-run` | Fase A completa + plano; **nunca** constrói cliente; declara que o estado remoto não foi consultado |
| `--allow-live-target` sem `--yes`, ou `--yes` sem `--allow-live-target` | **erro de utilização → exit 2**, detetado na validação de argumentos, **antes** de qualquer cliente |
| `--json` | contrato de output em §23.2 |
| `main` | `main(argv: list[str] \| None = None) -> int`; `raise SystemExit(main())` |
| Cliente | construído **em runtime**, dentro da execução do comando, por import diferido de `shared.opensearch` (precedente `migrate.py:124–128`) |
| `KeyboardInterrupt` | tratado (§18.3): relatório parcial, exit 1, sem traceback |
| **Não existe** | `--max-failures`, `--allow-duplicate-ids`, `--index-name`, `promote`, `create`, `delete` |

### 23.1 Exit codes

| Código | Significado |
|---|---|
| `0` | Sucesso: todos os gates (§21.3) passaram |
| `1` | Falha operacional: qualquer `IndexingError` — input, validação, projeção, duplicados, **`TargetIndexError` (incl. alias em conflito e `TargetNotEmptyError`)**, **`LiveTargetError`**, bulk, verificação — **ou** `OpenSearchException`, **ou** `KeyboardInterrupt` |
| `2` | Argumentos/configuração: erro argparse, **combinação inválida de flags de confirmação** (§17), **cliente não construível** |

### 23.2 Contrato de output

**Com `--json` (uma vez o parser reconhecido o comando e a flag):** stdout contém
**exatamente um documento JSON e nada mais**, em **qualquer** caminho — sucesso,
falha de validação, falha de target, falha de bulk, falha de verificação, e
**erro de configuração pós-parse** (p.ex. cliente não construível, exit 2):

```json
{ "reports": [ ...um por record type pedido, ordem RECORD_TYPES... ],
  "error": null }
```

```json
{ "reports": [ ... ],
  "error": { "type": "DuplicateRecordIdError",
             "record_type": "property_fact",
             "line_number": 812,
             "_id": "pf_...",
             "target_index": null } }
```

Regras:

- `error` é `null` em sucesso; em falha contém `type` (nome da classe da
  exceção) e os campos de contexto permitidos (§24.1), com a **regra única**:
  campos sempre presentes, `null` quando não aplicáveis;
- chaves ordenadas; sem timestamps; sem host, URL, porta, username, password ou
  body;
- **stdout não recebe texto humano**; mensagens humanas e diagnóstico sanitizado
  vão para **stderr**; **nenhum traceback**;
- os testes fazem `json.loads(stdout)` em todos os caminhos (§25).
- *Exceção única:* erros de **uso do argparse** (flags desconhecidas, tipos
  inválidos) ocorrem antes de `--json` ser conhecido — seguem o comportamento
  standard do argparse (usage em stderr, exit 2), sem envelope JSON.

**Sem `--json`:** relatórios humanos determinísticos (uma linha por record type)
em stdout; erro sanitizado em stderr.

### 23.3 Sanitização

```python
except OpenSearchException as exc:                     # nunca str(exc)
    print(f"OpenSearch error ({type(exc).__name__})", file=sys.stderr)
    return 1
except Exception as exc:                               # construção do cliente
    print(f"configuration error building the OpenSearch client ({type(exc).__name__})", file=sys.stderr)
    return 2
```

Idêntico a `migrate.py:235–248`. Com `--json`, estes caminhos emitem também o
envelope JSON (§23.2) com `error.type` = nome da classe.

---

## 24. Exceções

Hierarquia pública, no padrão de `CanonicalExtractionError` (HBIM-011),
`PropertyFactError` (HBIM-012) e `IndexLifecycleError` (HBIM-021):

```
IndexingError(Exception)                    # base; atributo reports: tuple[IndexReport, ...] = ()
├── InputError                              # --input-dir ausente/não-dir/ilegível; digest divergente (§8)
│   ├── MissingInputFileError
│   └── InputDecodeError                    # UTF-8 inválido
├── RecordParseError                        # JSON inválido
├── RecordValidationError                   # Pydantic: schema_version, record type, campos
├── ProjectionError                         # XOR violado, range int64/int32, elemento de lista vazio
├── DuplicateRecordIdError
├── TargetIndexError                        # base de problemas de target; TAMBÉM levantada
│   │                                       #   diretamente para alias em conflito (§16.1.6)
│   ├── MissingTargetIndexError
│   ├── TargetRecordTypeMismatchError
│   ├── IncompatibleTargetMappingError
│   └── TargetNotEmptyError                 # --require-empty com target não vazio
├── LiveTargetError                         # target ∈ alias_targets, sem os dois flags
├── BulkIndexingError                       # falha de bulk/transporte/serialização, sanitizada
└── VerificationError                       # contagem, round-trip ou alias divergentes
```

O atributo `reports` da base transporta **apenas** relatórios sanitizados (§21)
— nunca records, linhas, `_source`, dicts de erro brutos ou exceções originais.
`BulkIndexingError` é sempre criado com `from None` (§18.3).

### 24.1 Conteúdo permitido nas mensagens

**Permitido:** `record_type`, `line_number`, `_id`, `target_index`, `error_type`,
nome do ficheiro de entrada (nome, não path absoluto), contagens, hex de digest.

**Proibido, sem exceção:** conteúdo JSON ou fragmentos da linha; valores de
propriedades; `_source`; `data`/`exception` dos dicts de erro do helper; host;
URL; porta; username; password; request body; response body; `error.reason` e
`error.caused_by` do OpenSearch; paths absolutos do sistema de ficheiros;
tracebacks de exceções de transporte.

Um teste offline percorre a hierarquia e assere que nenhuma mensagem produzida
pelos caminhos de erro contém tokens proibidos.

---

## 25. Testes offline

`backend/tests/test_canonical_indexers.py` (e, se crescer,
`test_canonical_indexers_cli.py`). **Sem OpenSearch, sem Docker, sem rede, sem
ML, sem IFC, sem sleeps reais** — os testes nunca exercitam retries com
`time.sleep`: o runner testável usa `max_retries=0` ou um cliente falso que nunca
devolve 429, e os **kwargs de produção** (incl. `max_retries=3`,
`initial_backoff=2`, `max_backoff=60`, `max_chunk_bytes=10 MiB`) são verificados
**por inspeção da chamada**, nunca por execução real. Cliente falso em memória no
padrão de `test_index_lifecycle.py`.

**Registry e layout**
1. Registry tem exatamente quatro entradas, na ordem `il.RECORD_TYPES`.
2. Nenhuma referência a `chunk`/`chunks` em nenhum módulo do pacote.
3. Filenames vêm do registry; nenhum path do utilizador é aceite.
4. Aliases e nomes físicos derivam de `il`, não são redeclarados.

**Input**
5. `--input-dir` inexistente → `InputError`.
6. Ficheiro obrigatório ausente → `MissingInputFileError`.
7. Ficheiro de 0 bytes → válido, `lines_read=0`, `expected_count=0`.
8. Linhas vazias/whitespace ignoradas e contadas em `lines_blank`.
9. Última linha sem `\n` → válida e equivalente.
10. UTF-8 inválido → resultado parcial `ok=false` com
    `first_error_type="InputDecodeError"` e `line_number`; orquestrador levanta
    `InputDecodeError`.
11. JSON inválido → `RecordParseError` com `line_number`.
12. `schema_version` errada → `RecordValidationError`.
13. Record do tipo errado no ficheiro errado → `RecordValidationError`.
14. **Streaming**: um handle falso que levanta em `read()`/`readlines()` não
    quebra o leitor.
15. `coverage.json`/`warnings.jsonl`/ficheiros extra ignorados.

**Digest e estabilidade**
16. Digest calculado em streaming (sem `read()` total), determinístico entre
    execuções.
17. Digest **indiferente** à presença do newline final.
18. Digest **indiferente** a linhas em branco: `lines_blank` muda, digest não.
19. Digest **sensível** à alteração de um único byte significativo.
20. **Mesmos `_id`, mesmas contagens, valores alterados** entre A e B′ ⇒
    divergência ⇒ `InputError`, **zero ações bulk** (cliente falso assere zero
    chamadas).
21. **Quarto ficheiro alterado** depois de A(4)/B(4) ⇒ detetado em B′ **antes da
    primeira escrita** ⇒ zero escrita em qualquer target.
22. Ficheiro alterado antes do **próprio** C(rt) (com tipos anteriores já
    escritos no fake) ⇒ pré-C deteta ⇒ esse target sem escrita, abort.
23. Mutação durante C (simulada no fake entre batches) ⇒ digest final da Fase C
    e/ou `actions_produced != expected_count` ⇒ `InputError`, gate falha.

**Validação**
24. A tabela de equivalência §9.2 replicada como teste (regressão de Pydantic
    falha o CI).
25. `RecordParseError` vs `RecordValidationError` distinguidos corretamente.
26. `validate_input` **nunca levanta** por conteúdo; percorre o ficheiro inteiro;
    conta **todos** os erros (`records_invalid` > 1 com múltiplas linhas más);
    `failure_sample` ≤ 10, sanitizada.

**`_id`**
27. `_id` == campo de identidade verbatim, para os quatro tipos.
28. Prefixos (`el_`/`pf_`/`cf_`/`doc_`) e comprimento ≤ 36.
29. `canonical.ids` **não** é importado por nenhum módulo do pacote.
30. Nenhuma concatenação com `project_id`; padrão legacy ausente.

**Projeção**
31. Projeção dos quatro tipos contra **golden** determinístico.
32. **Chaves projetadas ⊆ paths do mapping** (recursivo, incluindo elementos de
    arrays), para os quatro — verificação estática, sem OpenSearch.
33. `materials` mantém a ordem do validador; o indexer não reordena.
34. `property_name_norm` e `unit` preservados verbatim, sem re-normalização.

**PropertyFact**
35. As **cinco** variantes de `PropertyValue` projetam para o campo certo.
36. **XOR**: não-null ⇒ exatamente um payload; null ⇒ zero payloads.
37. Coerência `value_type` → payload.
38. **`bool` nunca vai para `value_integer`** (`True` → `value_boolean`).
39. **`int` nunca vai para `value_number`** (`5` → `value_integer`; `5.0` →
    `value_number`).
40. `value_type` e `value_is_null` sempre presentes, nas cinco variantes.
41. `float` sempre finito.

**Null/omissão**
42. `None` podado recursivamente (`is None`, não *truthiness*); objetos vazios
    omitidos (`location`, `metrics`).
43. **`False`, `0`, `0.0`, `""`, `[]` preservados** — em particular
    `value_is_null: False`, `value_boolean: False` e `materials[].ordinal: 0`.
44. **`SpatialRef` totalmente vazio** (todos os campos `None`) projeta igual à
    ausência — classe de equivalência de §13.1 documentada, com referência a
    `ifc_spatial.py:117–125`.
45. **Elemento de lista que fique `{}` após poda ⇒ `ProjectionError`** (dict
    sintético alimentado diretamente ao helper de poda).

**Ranges**
46. `value_integer` fora de int64 → `ProjectionError`, **antes de qualquer bulk**.
47. `materials.ordinal` fora de `[0, 2**31 - 1]` → `ProjectionError`.
48. **Unicidade dos campos inteiros**: os quatro mappings contêm exatamente um
    campo `long` (`value_integer`) e um campo `integer` (`materials.ordinal`) —
    um mapping futuro com novos inteiros falha este teste.

**Duplicados**
49. `A,A,A,B,B` ⇒ `duplicate_ids == 3` (ocorrências além da primeira); ficheiro
    percorrido até ao fim; `DuplicateRecordIdError` só no fim; zero escrita.
50. Mensagem/relatório retêm apenas contagem + primeiro `_id` + primeira linha.
51. Nenhuma flag do parser permite duplicados (`--allow-duplicate-ids` inexistente).

**Fase A / relatórios em falha**
52. `validate` com N erros heterogéneos ⇒ relatório completo (todas as
    contagens), impresso, exit 1.
53. `IndexingError.reports` anexado, sanitizado, com um report por record type
    pedido.
54. Reports incluem `state`; record types não executados aparecem com
    `state="not_started"`, `ok=false`, `records_indexed=0`, `bulk_batches=0`.

**Zero escrita / duas passagens**
55. Linha inválida no **fim** do ficheiro ⇒ zero ações bulk enviadas.
56. **Quarto record type falha validação ⇒ zero escrita em qualquer dos quatro
    targets.**
57. **Um dos quatro preflights falha ⇒ zero escrita em qualquer target.**
58. Duplicado descoberto na última linha ⇒ zero escrita.

**Target e live target**
59. Target ausente → `MissingTargetIndexError`.
60. `_meta.record_type` errado → `TargetRecordTypeMismatchError`.
61. Mapping incompatível → `IncompatibleTargetMappingError`.
62. **Alias com dois targets** (fake) ⇒ `st.conflicts` ⊇ `{multiple_targets}` ⇒
    `TargetIndexError`, **zero escrita** — mesmo quando `physical` é um dos
    targets (**nunca** interpretado como "não live").
63. **Colisão alias/índice concreto** ⇒ `TargetIndexError`, zero escrita.
64. Alias **ausente** (`NotFoundError` ⇒ conjunto vazio) ⇒ não live ⇒ prossegue.
65. Alias a apontar para **outro** físico ⇒ não live ⇒ prossegue sem confirmação.
66. Target **live** (single target) sem flags → `LiveTargetError`.
67. Target live com `--allow-live-target --yes` → permitido.
68. `--allow-live-target` sem `--yes` → **exit 2**; `--yes` sem
    `--allow-live-target` → **exit 2** (antes de qualquer cliente).
69. `--require-empty` com target não vazio → `TargetNotEmptyError`.
70. Nome físico sempre composto por `il.physical_index_name`; nenhuma flag aceita
    um nome de índice.

**Bulk e ações**
71. Forma da ação (`_index`/`_id`/`_source`), `_op_type` default `index`.
72. Ordem das ações determinística (função pura do ficheiro).
73. Batching por `batch_size`; `bulk_batches` conta batches concluídos.
74. **Kwargs por inspeção**: `raise_on_error=False`, `raise_on_exception=False`,
    `yield_ok=False`, `chunk_size`, `max_chunk_bytes=10*1024*1024`,
    `max_retries=3`, `initial_backoff=2`, `max_backoff=60`, `request_timeout` —
    sem executar retries reais, sem sleeps.
75. **Consumo iterativo**: ≥ 50 falhas ⇒ `records_failed == 50`,
    `failure_sample` com exatamente 10 entradas `{_id, status, error_type}`;
    nenhum objeto retido contém `data`, `exception`, `reason`, `caused_by` ou
    `_source`; nenhuma lista completa de erros existe (assert estrutural sobre o
    estado do runner).
76. **`TransportError` falso** ⇒ erros por-item; `error_type == "transport_error"`
    quando `error` é string; relatório sem tokens proibidos.
77. **`SerializationError` falso** ⇒ `BulkIndexingError` com `error_type` apenas
    (classe, nunca `str(exc)`), `from None`, batch em voo credita zero.
78. **Batch interrompido** (exceção a meio da iteração) ⇒ `records_indexed`
    credita zero desse batch; `bulk_batches` não incrementa; batches anteriores
    mantêm accounting.
79. **Zero ações ⇒ nenhuma chamada** a `streaming_bulk`/`client.bulk`;
    `bulk_batches == 0`.

**Reporting**
80. Relatório JSON estável (chaves ordenadas, sem timestamps; campos sempre
    presentes, `null` quando não aplicável).
81. `records_indexed` sob interrupção: **não** é `records_valid -
    records_failed`; é o acumulado dos batches concluídos.
82. Gates §21.3 avaliados corretamente, incluindo `actual_count !=
    expected_count`, `actions_produced != expected_count`, e **round-trip com
    `_id` da amostra ausente** (cliente falso: `NotFoundError` e `found=false`)
    ⇒ `VerificationError`.
83. Relatório e mensagens sem host/porta/utilizador/password/URL/body.
84. `state` transita corretamente (`validated` → `preflighted` → `indexing` →
    `indexed` → `verified`; `failed` no tipo abortado).

**CLI**
85. `validate`, `index --dry-run`, `index-one`; exit 0/1/2; `main([...])`
    testável.
86. `--dry-run` e `validate` **não constroem cliente** (recorder assere zero
    construções) e declaram que o estado remoto não foi consultado.
87. **`--json` parseável em todos os caminhos**: `json.loads(stdout)` com
    exatamente um documento em — sucesso; erro de validação; erro de target;
    erro de bulk; erro de verificação; **erro de configuração pós-parse**
    (cliente não construível, exit 2). Stdout sem texto humano.
88. Erros OpenSearch mostram **apenas** o nome da classe.
89. `validate` sem `--record-type` exige os quatro ficheiros.
90. `KeyboardInterrupt` no runner (monkeypatch) ⇒ relatório parcial impresso,
    exit 1, sem traceback.

**Exceções**
91. Hierarquia completa e pública (incl. `TargetNotEmptyError`,
    `LiveTargetError`); base transporta `reports`; nenhuma mensagem contém
    tokens proibidos (§24.1).

**Import-safety** (§27)
92. Interpretador fresco (subprocess) confirma a lista de módulos proibidos.
93. A guarda autouse de sockets confirma que nenhum import abre rede.

### 25.1 Fixtures

- **Reutilizar** `backend/tests/fixtures/canonical/*.jsonl` (cobrem as cinco
  variantes de `PropertyValue`, unicode, `unit`, `occurrence_key` repetido,
  materials com `role`/`ordinal`, refs espaciais, documento com
  `linked_element_ids`).
- **Proibido modificá-las**: são goldens byte-stable da HBIM-010
  (`test_canonical_serialization.py::test_golden_fixtures_are_byte_stable`).
- Casos novos (duplicados, UTF-8 inválido, JSON inválido, overflow, ficheiro
  vazio, mutação entre passagens) vivem num **novo** diretório
  `backend/tests/fixtures/canonical/indexing/`, ou são construídos em `tmp_path`.
  Verificado: nenhum teste existente faz *glob* de `fixtures/canonical/`; todos
  leem por nome explícito — acrescentar um subdiretório é seguro.

---

## 26. Integração

`backend/tests/integration/test_canonical_indexers_apply.py`,
`pytestmark = pytest.mark.integration`, usando **exclusivamente** o fixture
`opensearch_client` (host/porta mapeados do container, `use_ssl=False`, sem
credenciais, loopback — a guarda `_LoopbackOnlySocket` já o força). Imagem pinada
**`opensearchproject/opensearch:2.19.1`**. **Nunca** `OpenSearchSettings`,
**nunca** `.env`, **nunca** host remoto. Sem ML, sem IFC.

Cobertura obrigatória:

1. Criar os **quatro índices físicos** via `il.create_all` (HBIM-021).
2. **Indexar os quatro JSONL** escritos em `tmp_path` a partir das fixtures
   sintéticas.
3. **Contagens corretas** nos quatro (`count == expected_count`).
4. **`get` por `_id`** nos quatro; `found == True`.
5. **`_source == doc projetado`** (round-trip lossless; arrays em ordem).
6. **Projeções PropertyFact**: `range` sobre `value_integer` **e** `value_number`
   independentemente; `term` sobre `value_boolean`; `match` sobre `value_text`;
   `term` sobre `value_type` e `value_is_null`.
7. **`materials` nested**: correlação `name` + `role` no **mesmo** material.
8. **Classificações**: `terms` sobre `system` e `code`.
9. **Checksums**: `source.checksum` e `document.checksum` term-searchable.
10. **Rerun idempotente**: segunda execução ⇒ mesma contagem, zero duplicados,
    relatório `ok`.
11. **Falha parcial**: ação sintética inválida injetada **diretamente no
    `run_bulk`** (a projeção válida não a consegue produzir) ⇒ `records_failed`
    correto, execução continua até ao fim do input, gate falha.
12. **Alias não promovido**: snapshot do alias antes e depois idêntico; num
    cluster limpo, alias ausente permanece ausente; **nenhuma** chamada a
    `update_aliases`.
13. **Target errado rejeitado**: índice em `hbim_property_facts_v<N>` com
    `_meta.record_type = "element"` ⇒ `TargetRecordTypeMismatchError`.
14. **Mapping incompatível rejeitado** ⇒ `IncompatibleTargetMappingError`.
15. **Alias com múltiplos targets real**: criar dois físicos e apontar o alias
    para ambos (setup de teste via `update_aliases` direto) ⇒ preflight recusa
    com `TargetIndexError`; **zero escrita**; mesmo quando o `physical` pedido é
    um dos dois targets.
16. **Target live**: promover um físico (via `il.promote`) e indexar nele sem
    flags ⇒ `LiveTargetError`; com `--allow-live-target --yes` ⇒ sucesso.
17. **`--require-empty`** com target não vazio ⇒ `TargetNotEmptyError`.
18. **Input vazio + target vazio** ⇒ `ok=true`; **zero chamadas bulk**.
19. **Input vazio + target não vazio** ⇒ `VerificationError`; documentos extra
    **continuam lá** (nenhum delete).
20. **Documentos extra**: indexar um doc sintético fora do JSONL ⇒
    `actual_count != expected_count` ⇒ `VerificationError`; o documento extra
    permanece.
21. **Mutação do input durante a execução real**: cliente wrapper cujo primeiro
    `bulk` acrescenta uma linha válida ao ficheiro de entrada ⇒ o digest final
    da Fase C (e/ou `actions_produced`) diverge ⇒ `InputError`; gate falha;
    alias inalterado; rerun com o ficheiro final converge.
22. **Falha em D(element)** (simulada por documento extra pré-inserido em
    `hbim_elements_v<N>`) ⇒ `VerificationError` em element; **os outros três
    targets ficam vazios** (`count == 0`) — nenhum recebeu escrita.
23. **Rerun após execução parcial converge**: interromper (simulado) e reexecutar
    ⇒ contagem final correta.
24. **Índice legacy `bim_elements` inalterado** (nem apagado, nem alterado, nem
    criado por esta issue).
25. **Nenhum modelo carregado**: `torch` e `sentence_transformers` ausentes de
    `sys.modules` no fim.
26. **Cleanup**: apenas os índices sintéticos da própria suite.

### 26.1 Isolamento cross-módulo (obrigatório)

⚠ `test_index_lifecycle_apply.py` usa **os mesmos** nomes físicos (`hbim_*_v*`) no
container de sessão partilhado, e `pytest-randomly` altera a ordem dos testes.
A suite HBIM-022 tem de ter a **sua própria fixture `autouse`** que purga **antes
e depois de cada teste**, com a mesma disciplina de namespace da HBIM-021:

- purgar **apenas** os padrões `<alias>_v*` dos quatro aliases do registry, os
  aliases em si quando existem como índices concretos, e `bim_elements` quando
  criado pelo teste;
- **nunca** um glob largo `hbim_*` — `hbim_smoke_test` e `hbim_eval_baseline_v1`
  pertencem a outras suites no mesmo container e **têm de sobreviver**.

> **Nota sobre delete:** a eliminação em teardown de testes é permitida e
> obrigatória para cleanup. A proibição de `delete` aplica-se ao **código de
> produção**, nunca ao cleanup de Testcontainers (idêntico a HBIM-021 §20).

---

## 27. Import-safety

Importar **qualquer** módulo de `ingestion.indexers` **não pode** importar nem
criar:

| Proibido | Porquê |
|---|---|
| `shared.config` | settings/`.env` no import |
| `shared.opensearch` | construção de cliente |
| `dotenv` | leitura de `.env` |
| `ifcopenshell` | `ingestion.canonical_ifc` importa-o no topo |
| `torch`, `sentence_transformers` | ML |
| `ingestion.canonical_ifc` | puxa `ifcopenshell` |
| `ingestion.index_to_opensearch` | avalia `shared.config` no import (linha 12) |
| `api.*`, `eval.*` | camadas superiores |
| Qualquer cliente OpenSearch ou socket | rede no import |

**Requerido e permitido:** `canonical.schema` (os modelos são necessários),
`ingestion.index_lifecycle`, `opensearchpy` (tipos, exceções e `helpers`),
`hashlib` — exatamente o que a implementação precisa.

> ⚠ **Atenção ao copiar a lista da HBIM-021:** aquele teste proíbe
> `canonical.schema` porque o *loader* do lifecycle não pode importá-lo. A
> HBIM-022 **precisa** de `canonical.schema`. As listas são diferentes por razão
> técnica; não as unificar.

O cliente é criado **apenas** no caminho runtime da CLI, por import diferido
dentro da função que executa o comando (precedente `migrate.py:124–128`).
Verificação por **subprocess em interpretador fresco**, no padrão
`test_index_lifecycle.py:555–566`, mais a guarda autouse de sockets.

---

## 28. CI e mypy

- **Sem job CI novo.** Testes offline no job `backend-unit`; integração reutiliza
  `integration-opensearch` (Testcontainers já existente). `evaluation-opensearch`
  inalterado. Sem serviços externos, sem ML, sem `requirements-ml.txt`.
- **Ruff** limpo sobre `backend`. `known-first-party` já inclui `ingestion` — sem
  alteração de configuração.
- **Mypy bloqueante em DOIS sítios** (precedente HBIM-021 §24 — o gate só é
  efetivo se ambos listarem os módulos):

  **1. `pyproject.toml`**, `[[tool.mypy.overrides]] disallow_untyped_defs = true`:
  ```
  "ingestion.indexers",
  "ingestion.indexers.common",
  "ingestion.indexers.registry",
  "ingestion.indexers.elements_indexer",
  "ingestion.indexers.property_facts_indexer",
  "ingestion.indexers.classification_facts_indexer",
  "ingestion.indexers.documents_indexer",
  "ingestion.indexers.cli",
  ```

  **2. `.github/workflows/ci.yml`**, lista explícita do `python -m mypy`:
  ```
  backend/ingestion/indexers/__init__.py
  backend/ingestion/indexers/__main__.py
  backend/ingestion/indexers/common.py
  backend/ingestion/indexers/registry.py
  backend/ingestion/indexers/elements_indexer.py
  backend/ingestion/indexers/property_facts_indexer.py
  backend/ingestion/indexers/classification_facts_indexer.py
  backend/ingestion/indexers/documents_indexer.py
  backend/ingestion/indexers/cli.py
  ```

> **Verificado:** o override legacy existente `module = [..., "ingestion.*"]` com
> `disallow_untyped_defs = false` **não** anula o strict — o mypy aplica a
> correspondência **mais específica**, e é exatamente assim que
> `ingestion.index_lifecycle` e `ingestion.migrate` já funcionam hoje.

- Todo o código novo é **totalmente tipado**, sem `Any` desnecessário, sem
  `# type: ignore` novos, sem dívida de tipagem.

---

## 29. Segurança

- **Nenhuma variável de ambiente nova.** O cliente vem de
  `get_opensearch_client()` em runtime; `OpenSearchSettings`/`SecretStr` ficam
  intocados.
- **Nunca** abrir, ler, imprimir ou resumir `backend/.env`.
- **Nunca** host, porta, utilizador, password, URL, request body ou response body
  em código, testes, documentação, relatório ou mensagens de erro.
- Erros OpenSearch sanitizados para o **nome da classe** (§23.3); exceções de
  bulk convertidas com `from None` (§18.3) — o objeto bruto nunca viaja em
  `__cause__` nem em traceback impresso.
- Erros de item bulk: **apenas** `_id` (hash, não sensível), `status` e
  `error_type` sobrevivem (§18.2). **Proibido** reter, logar, serializar ou
  anexar os dicts brutos do helper — em particular `data` (o `_source`
  completo), `exception` (objeto vivo), `error` string de `TransportError`,
  `reason` e `caused_by`.
- Mensagens de erro de input identificam `record_type` e `line_number`, **nunca**
  o conteúdo (que pode conter dados do projeto).
- Testes usam **exclusivamente** valores sintéticos e domínios `.example.test`.
- **Nenhum IFC real**, nenhum download, nenhum modelo ML, nenhum cluster remoto.
- `backend/.env` continua git-ignored e não tracked; `local_data/` continua
  ignorado; nenhum `.ifc` é committed.

---

## 30. Proteções

**Têm de permanecer byte-idênticos**, salvo blocker explícito documentado e
**aprovado antes** de qualquer alteração:

- `backend/canonical/schema.py`
- `backend/canonical/ids.py`
- `backend/canonical/serialization.py`
- `backend/canonical/__init__.py`
- `backend/canonical/mappings/elements_v1.json`
- `backend/canonical/mappings/property_facts_v1.json`
- `backend/canonical/mappings/classification_facts_v1.json`
- `backend/canonical/mappings/documents_v1.json`
- `backend/ingestion/index_lifecycle.py`
- `backend/ingestion/migrate.py`
- `backend/ingestion/canonical_ifc.py` (e `ifc_spatial`, `ifc_materials`,
  `ifc_values`, `ifc_properties`, `property_facts`)
- os goldens canónicos existentes (`backend/tests/fixtures/canonical/*.jsonl`,
  `coverage_manifest.json`, `ifc_extraction/**`)
- `backend/eval/**` e a baseline `backend/eval/baselines/current_system.json`
- `backend/api/**` (API e retrieval)
- `backend/shared/**`
- `frontend/**`
- `.gitignore`
- **`backend/requirements.txt`**, **`backend/requirements-dev.txt`** e
  **`backend/requirements-ml.txt`** (caso exista) — nenhuma dependência nova
  (§4)
- **`backend/ingestion/index_to_opensearch.py`** (indexer legacy)

> **Legacy:** a API/retrieval continuam a usar `bim_elements` através do indexer
> legacy. Esta issue **não** o altera. Se a implementação encontrar uma razão
> concreta para o tocar, é um **blocker** que exige aprovação explícita **antes**
> de qualquer alteração — nunca uma correção oportunista.

**Podem ser alterados (só na implementação):**
- `pyproject.toml` (lista mypy, §28)
- `.github/workflows/ci.yml` (lista mypy, §28)
- `docs/development/LOCAL_SETUP.md` (secção operacional da HBIM-022)
- `docs/implementation/IMPLEMENTATION_STATUS.md` — **só no fim**, depois de todos
  os gates passarem. Se houver bloqueio, deixá-lo **inalterado**.

**Durante esta fase de spec:** `IMPLEMENTATION_STATUS.md` e `ROADMAP.md` **não**
são alterados.

---

## 31. Critérios de aceitação

Cada critério mapeia para teste/ficheiro/evidência. "Offline n" refere a
numeração de §25; "integração n" a de §26.

1. **Quatro indexers** (`element`/`property_fact`/`classification_fact`/`document`);
   **sem chunks**; registry fechado derivado da HBIM-021. (§5; offline 1–4)
2. **Layout** conforme §6: `common.py` concentra a maquinaria; os quatro
   `*_indexer.py` são finos; `registry.py` evita o ciclo de imports.
   *(Verificado por review, não por teste automático.)*
3. **Input só por `--input-dir`**; filenames do registry; nenhum path arbitrário.
   (§7; offline 3, 5–6)
4. **Contrato de input completo**: 0 bytes válido **com gates remotos ativos**,
   linhas vazias contadas, UTF-8 inválido, JSON inválido, `schema_version`,
   record type errado, newline final. (§7; offline 7–13; integração 18–19)
5. **Streaming**: nenhum `read()`/`readlines()`; nenhuma materialização do
   dataset **nem dos erros de bulk**. (§7.1, §18.2; offline 14, 75)
6. **Digest de estabilidade**: SHA-256 incremental com prefixo de comprimento;
   indiferente a newline final e linhas em branco; sensível a um byte; comparado
   na Fase B′ (os quatro), antes de cada C(rt) e no fim de cada C(rt), com
   `actions_produced == expected_count`. (§7.2, §8; offline 16–23; integração 21)
7. **Duas passagens**: **zero escrita remota** quando uma linha inválida está no
   fim do ficheiro, quando o quarto record type falha validação, quando há
   duplicados, quando qualquer preflight falha, **ou quando qualquer digest
   diverge antes da primeira escrita**. (§8; offline 20–22, 55–58)
8. **Validação** por `model_validate_json` com diagnóstico controlado;
   equivalência das duas vias testada; `validate_input` **nunca levanta por
   conteúdo** e percorre o ficheiro inteiro. (§9; offline 24–26)
9. **Relatórios em falha**: `InputValidationResult` + `IndexingError.reports`;
   a CLI imprime sempre os reports; reports para **todos** os record types
   pedidos, incluindo `not_started`. (§9.4, §21.4; offline 52–54)
10. **`_id` verbatim**, nunca recomputado, nunca concatenado; `canonical.ids` não
    importado. (§10; offline 27–30)
11. **Projeções exatas** dos quatro tipos; **chaves projetadas ⊆ paths do
    mapping** verificado estaticamente. (§11; offline 31–34)
12. **PropertyFact**: `value_type` e `value_is_null` sempre; XOR; null ⇒ zero
    payloads; `bool` ≠ `int`; `int` ≠ `float`; `float` finito; `unit`,
    `occurrence_key`, `source` e identidade verbatim. (§12; offline 35–41)
13. **Null/omissão**: poda só de `None` (`is None`); omissão de `{}` **apenas em
    valores de campos objeto**; listas vazias preservadas; elemento de lista
    vazio ⇒ `ProjectionError`; `False`/`0`/`""`/`[]` preservados; classe de
    equivalência de §13.1 documentada. (§13; offline 42–45)
14. **Ranges**: `value_integer` em int64 e `materials.ordinal` em int32 não
    negativo, validados **antes** de qualquer bulk; **unicidade dos campos
    inteiros nos quatro mappings testada**. (§14; offline 46–48)
15. **Duplicados sempre fail-closed**; semântica única (ocorrências além da
    primeira); scan completo; nenhuma flag permissiva. (§15; offline 49–51)
16. **Target composto pelo registry**; nunca nome arbitrário; nunca escrita pelo
    alias; nunca create/delete/promote. (§16; offline 59–61, 70; integração 12)
17. **Alias em conflito fail-closed**: `st.conflicts` não vazio ⇒
    `TargetIndexError`, zero escrita; **nunca** interpretado como não-live nem
    como ausente. (§16.1.6, §17; offline 62–63; integração 15)
18. **Live por enumeração real**: `physical in get_alias(alias)` com
    `NotFoundError` ⇒ vazio; alias noutro físico ⇒ não live; recusado sem os
    dois flags; permitido só com `--allow-live-target --yes`; um flag sem o
    outro ⇒ exit 2. (§17; offline 64–68; integração 16)
19. **Preflight** com APIs **públicas** de `index_lifecycle` + `client.indices.
    get_alias`; nenhum helper privado; `index_lifecycle.py` byte-idêntico.
    (§16.2; §30)
20. **Bulk** com o contrato exato de §18: kwargs verificados por inspeção,
    `max_chunk_bytes` 10 MiB, `_op_type=index`, **consumo iterativo** com
    sanitização imediata (só `_id`/`status`/`error_type`), amostra ≤ 10,
    `records_failed` total. (offline 71–76)
21. **Exceções de bulk**: `raise_on_exception=False` documentado como cobrindo
    **apenas** `TransportError`; `SerializationError`/inesperadas ⇒
    `BulkIndexingError` com nome de classe, `from None`, crédito zero.
    (§18.3; offline 76–78)
22. **Accounting**: `records_indexed` = sucessos contabilizados em batches cuja
    iteração terminou normalmente; batch em voo credita zero; subestimação
    limitada ao tamanho do batch em voo. (§18.1, §21.2; offline 78, 81)
23. **Idempotência**: rerun com os mesmos `_id` converge; `--require-empty` →
    `TargetNotEmptyError`; documentos extra **nunca** apagados. (§19; offline
    69; integração 10, 17, 19–20, 23)
24. **Falhas parciais e estados**: execução continua dentro do record type,
    aborta entre record types; estados finais enumerados; `state` no relatório;
    tipos não executados `not_started`; alias intacto. (§20; offline 54, 84;
    integração 11, 22)
25. **Reporting** determinístico com os **17** campos e a regra única de
    presença/null. (§21; offline 80–83)
26. **Verificação**: refresh, `count`, round-trip da amostra determinística
    (primeiro/mediano/último, todos se ≤ 3, vazia se 0), `NotFoundError`/
    `found=false`/`_source` ausente ⇒ `VerificationError`, comparação exata com
    arrays em ordem, **alias inalterado**; round-trip **não substitui** o
    digest. (§22; offline 82; integração 3–5, 12)
27. **CLI** `validate` / `index` / `index-one`, `--dry-run`, exit `0`/`1`/`2`,
    `main(argv)->int`; sem `--max-failures`; sem `--allow-duplicate-ids`;
    `validate` sem `--record-type` exige os quatro; `KeyboardInterrupt` ⇒
    relatório parcial + exit 1. (§23; offline 85, 89–90)
28. **`--dry-run` e `validate` não constroem cliente** e declaram que o estado
    remoto não foi consultado. (§23; offline 86)
29. **`--json` estritamente parseável**: um único documento JSON em stdout em
    **todos** os caminhos pós-parse (sucesso e todas as classes de falha,
    incluindo configuração); texto humano só em stderr; sem tracebacks.
    (§23.2; offline 87)
30. **Exceções**: hierarquia pública completa (incl. `TargetNotEmptyError` e
    `LiveTargetError`); base com `reports` sanitizados; nenhuma mensagem com
    conteúdo, valores, host, credenciais, body, `reason` ou `caused_by`.
    (§24; offline 91)
31. **Import-safety**: interpretador fresco não puxa `shared.config`,
    `shared.opensearch`, `dotenv`, `ifcopenshell`, `torch`,
    `sentence_transformers`, `canonical_ifc` nem `index_to_opensearch`; nenhum
    socket no import. (§27; offline 92–93)
32. **Integração obrigatória** (Testcontainers 2.19.1, loopback, os 26 pontos de
    §26), com isolamento cross-módulo (§26.1).
33. **Legacy `bim_elements` inalterado**; API/retrieval inalterados; nenhum alias
    novo consumido. (§30; integração 24)
34. **Baseline HBIM-005 byte-idêntica**; mappings HBIM-020, `backend/canonical`,
    lifecycle HBIM-021 e `requirements*.txt` byte-idênticos. (§30)
35. **CI sem job novo**; mypy bloqueante nos **dois** sítios; Ruff limpo. (§28)
36. **Sem ML/IFC/remoto/segredos**: nenhum modelo, download, IFC real, cluster
    externo ou `.env`; testes sem sleeps reais nem dependência do relógio.
    (§25, §29)
37. **Fronteira preservada**: sem chunks, sem embeddings, sem promoção de alias,
    sem migração de API/retrieval, sem conversão do legacy, sem `_reindex`, sem
    delete, sem reparação de aliases. (§4)
38. **Lacuna de roadmap assinalada** na spec e não resolvida. *(Verificado por
    review, não por teste automático.)* (§2.2)

---

## 32. Sequência de implementação

Cada bloco termina com os seus testes verdes antes de o seguinte começar.

1. **Exceções + registry + leitor streaming + digest** (`common.py`,
   `registry.py`). Testes: offline 1–23 (parte local).
2. **Validação + diagnóstico + `validate_input`/`InputValidationResult`**
   (`common.py`). Testes: offline 24–26, 52–53.
3. **Projeções finas** (`elements_indexer`, `classification_facts_indexer`,
   `documents_indexer`) + poda de `None`. Testes: offline 27–34, 42–45.
4. **Projeção PropertyFact + ranges numéricos**
   (`property_facts_indexer.py`). Testes: offline 35–41, 46–48.
5. **Duplicados + amostra determinística + Fase A completa**. Testes: offline
   49–51, 58.
6. **Preflight de target + conflitos + live target** (`common.py`). Testes:
   offline 59–70.
7. **Builder de ações + `run_bulk` (consumo iterativo, accounting por batch,
   exceções)**. Testes: offline 71–79.
8. **Relatório (+`state`) + gates + verificação (Fase D)**. Testes: offline
   80–84.
9. **Orquestração das quatro+duas fases** (A → B → B′ → C/D; zero escrita com
   input inválido ou mutado). Testes: offline 20–23, 55–57.
10. **CLI** (`cli.py`, `__main__.py`, `__init__.py`), incluindo o envelope
    `--json` e `KeyboardInterrupt`. Testes: offline 85–90.
11. **Import-safety** (subprocess). Testes: offline 91–93.
12. **Integração** (`test_canonical_indexers_apply.py`, §26), incluindo a fixture
    de purge isolada (§26.1).
13. **Gates de qualidade**: Ruff, mypy (pyproject + ci.yml), suite completa
    offline em pelo menos duas ordens (`pytest-randomly` + `-p no:randomly`),
    suite de integração.
14. **Documentação**: secção HBIM-022 em `LOCAL_SETUP.md`.
15. **`IMPLEMENTATION_STATUS.md`** — **só no fim**, depois de todos os gates.

### 32.1 Ambiente

⚠ A implementação e **toda** a validação têm de correr em **WSL, filesystem
Linux, conda `hbim-rag`** (`conda run -n hbim-rag`), conforme
`docs/development/LOCAL_SETUP.md`. A auditoria confirmou que um interpretador
Windows não tem `ifcopenshell`, `prometheus_client` nem `testcontainers` e **não
consegue correr a suite completa**. Um relatório de conclusão baseado numa suite
parcial não é aceitável.

---

## 33. Riscos residuais

| Risco | Mitigação |
|---|---|
| **Overflow int64/int32** aceite pelo Pydantic e rejeitado pelo OpenSearch (verificado: `2**70` valida) | Validação de range local na Fase A → `ProjectionError` com `line_number` e `_id`, antes de qualquer bulk (§14); unicidade dos campos inteiros testada |
| **`max_chunk_bytes` default (100 MB) == `http.max_content_length` default** | Fixado explicitamente em 10 MiB (§18) |
| **Chaves JSON duplicadas**: *last-wins* silencioso em ambos os caminhos Pydantic | Aceite e documentado; o produtor canónico nunca as gera; detetá-lo exigiria um parser com `object_pairs_hook`, desproporcionado (§9.3). Nota: afeta igualmente o digest (a linha é hashada como está — a mutação seria detetada, o *last-wins* não) |
| **Mutação concorrente do input durante a própria Fase C** | Não prevenível; **detetada** pelo digest final / `actions_produced` (§8.3); escrita parcial possível nesse record type; alias intacto; rerun converge; input contratualmente imutável |
| **Memória do conjunto de `_id`**: O(nº de records) | Documentado (§15); ordens de grandeza abaixo do ficheiro |
| **Custo da dupla leitura + digest** (A + B′ + C) | Deliberado; preço de "zero escrita com input inválido ou mutado" sem materializar o dataset; mesmo caminho de código nas duas passagens (§8.4) |
| **Escrita parcial por falha de rede** | Não é eliminável. Mitigada por: alias nunca promovido, rerun idempotente, verificação fail-closed, `records_indexed` como limite inferior com subestimação limitada ao tamanho do batch em voo (§18.1) |
| **`SerializationError` fora do contrato por-item** | Documentado (§18.3); `BulkIndexingError` sanitizado com `from None` |
| **Colisão de namespace `hbim_*_v*`** entre suites de integração com `pytest-randomly` | Fixture `autouse` própria com purge antes/depois e namespace restrito; `hbim_smoke_test` e `hbim_eval_baseline_v1` preservados (§26.1) |
| **Documentos extra no target não são apagáveis** | Detetados pelo gate de contagem; remediação = nova versão física + reindexação (§19). Nunca delete |
| **Deriva projeção ↔ mapping** (risco #1 da HBIM-020 §18) | Três camadas: teste estático offline "chaves projetadas ⊆ paths do mapping" (§11.5), preflight de compatibilidade recursiva (§16.1), e integração real (§26) |
| **Aliases em conflito não são reparáveis por esta issue** | Deteção e recusa fail-closed (§16.1.6); reparação fica para tooling HBIM-021 futuro / intervenção manual (§4) |
| **API/retrieval continuam em `bim_elements`** depois desta issue | Lacuna de roadmap explicitamente assinalada (§2.2); exige issue própria |

---

## 34. Definition of Done

A HBIM-022 está concluída quando **todos** os pontos seguintes forem
verdadeiros e evidenciados:

1. Os nove módulos de `backend/ingestion/indexers/` existem, totalmente tipados,
   e respeitam o layout e as responsabilidades de §6.
2. Os quatro record types indexam a partir de `--input-dir`, em streaming, com
   `_id` canónico verbatim e projeção conforme §11–§14.
3. **Nenhuma escrita remota** ocorre quando qualquer input local é inválido,
   quando qualquer preflight falha, **ou quando qualquer digest diverge antes da
   primeira escrita** — provado pelos testes offline 20–22 e 55–58.
4. O **digest de estabilidade** (§7.2) está implementado com as propriedades
   exigidas e comparado nos quatro pontos definidos (A, B′, pré-C, fim-de-C com
   `actions_produced`).
5. As invariantes de `PropertyFact` (§12.2) estão garantidas em código e testadas
   nas cinco variantes; os ranges int64/int32 e a sua unicidade nos mappings
   estão testados.
6. Duplicados são sempre erro, com a semântica única de §15 (ocorrências além da
   primeira, scan completo, exceção no fim); não existe modo permissivo.
7. O indexer **nunca** cria, apaga ou promove índices/aliases, e **nunca** escreve
   através de um alias — provado offline e em integração.
8. **Aliases em conflito são recusados fail-closed** (§16.1.6) e **nunca**
   interpretados como não-live; o estado live é decidido pela **enumeração real**
   `client.indices.get_alias` (§17); recusado por defeito; permitido só com
   `--allow-live-target --yes`; um flag sem o outro ⇒ exit 2.
9. Rerun idempotente converge; `--require-empty` → `TargetNotEmptyError`;
   documentos extra causam `VerificationError` e **nunca** delete.
10. O bulk consome erros **iterativamente** (§18.2): só `_id`/`status`/
    `error_type` sobrevivem, amostra ≤ 10, `records_failed` total, nenhum dict
    bruto retido/logado/anexado; exceções fora do contrato por-item viram
    `BulkIndexingError` com nome de classe e `from None` (§18.3).
11. `records_indexed` tem a semântica de §18.1/§21.2 (batches concluídos; batch
    em voo credita zero; limite inferior com subestimação limitada ao tamanho do
    batch em voo).
12. Relatório determinístico com os **17** campos (incl. `state`), regra única
    de presença/null, reports para todos os record types pedidos, e
    `IndexingError.reports` preenchido em falha; a CLI imprime sempre os reports.
13. Verificação final completa: refresh, `count`, round-trip da amostra
    determinística com os quatro casos de falha de §22.4, comparação exata,
    alias inalterado contra o snapshot; estados finais por target conforme §20.
14. CLI `validate` / `index` / `index-one` com `--dry-run`, exit codes
    `0`/`1`/`2`, envelope `--json` estritamente parseável em todos os caminhos
    pós-parse, `KeyboardInterrupt` tratado; `--dry-run` e `validate` não
    constroem cliente.
15. Hierarquia de exceções pública completa (incl. `TargetNotEmptyError`,
    `LiveTargetError`, `reports` na base); nenhuma mensagem contém conteúdo,
    valores, host, credenciais, body, `reason` ou `caused_by`.
16. Suite **offline** completa verde, em **pelo menos duas ordens** de execução
    (`pytest-randomly` e `-p no:randomly`), sem estado partilhado deixado para
    trás (módulos, globais, caches, mocks, variáveis de ambiente), **sem sleeps
    reais e sem dependência do relógio**.
17. Suite de **integração** verde contra Testcontainers `2.19.1`, loopback, com
    os 26 pontos de §26 e o isolamento cross-módulo (§26.1).
18. **Ruff** limpo; **mypy** bloqueante verde com os módulos novos listados em
    **`pyproject.toml` e `.github/workflows/ci.yml`**; **nenhum job CI novo**.
19. **Todos os ficheiros de §30 byte-idênticos**, verificado por checksum,
    incluindo a baseline HBIM-005, o indexer legacy e os `requirements*.txt`.
20. `git diff --check` limpo; `git status --short --untracked-files=all` sem
    ficheiros fora de scope; `backend/.env` não tracked; nenhum `.ifc` committed;
    nenhum segredo no diff.
21. `docs/development/LOCAL_SETUP.md` com a secção operacional da HBIM-022.
22. `IMPLEMENTATION_STATUS.md` atualizado **apenas no fim**, com o campo
    *Current branch* corrigido.
23. Relatório final no formato do `CLAUDE.md`, incluindo a secção obrigatória
    **`Self-review findings`**, com cada critério de aceitação de §31 avaliado
    como `PASS` / `FAIL` / `PARTIAL` e evidência concreta (ficheiro, símbolo,
    teste).
