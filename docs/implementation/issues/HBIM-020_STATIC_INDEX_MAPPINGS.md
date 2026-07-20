# HBIM-020 — Static OpenSearch Index Mappings

> **Tipo:** especificação executável de issue.
> **Branch obrigatória:** `feat/hbim-020-static-index-mappings`.
> **Depende de:** HBIM-010 (schema v1.0), HBIM-011/012 (records produzidos), todos merged.

---

## 0. Precedência e fonte de verdade

1. **Schema canónico v1.0 implementado** (`backend/canonical/schema.py`) — fonte de verdade dos campos e tipos.
2. Comportamento implementado por HBIM-011/012 (serialização canónica real).
3. Esta decisão de review.
4. ROADMAP antigo (mais baixo; contém campos superados — §2).

**`backend/canonical/{schema,ids,serialization}.py` NÃO são alterados.** Esta issue só adiciona **artefactos de mapping** (JSON) e a sua validação.

---

## 1. Objetivo e fronteiras

Definir o **contrato OpenSearch** para os records canónicos: mappings **estáticos, versionados, `dynamic: strict`**, sem mapping explosion, **sem vetores**, **sem aliases**, **sem criação destrutiva**, sem alteração ao schema, testáveis offline e contra **OpenSearch 2.19.1 efémero** (Testcontainers).

**Fora de scope (fronteiras explícitas):**
- **HBIM-021:** nomes físicos dos índices, aliases, criação/promoção, rollback, **settings operacionais** (`number_of_shards`/`replicas`/`total_fields.limit`/refresh), remoção do `delete+create` legacy.
- **HBIM-022:** *projection code*, indexers separados, leitura de JSONL, bulk indexing, escolha de `_id`, round-trip e contagens.
- **HBIM-030/031:** embeddings/`knn_vector`/dimensão.
- **HBIM-070:** chunks/conteúdo de documento/OCR.

A HBIM-020 define **o que** cada índice aceita; **não** cria índices nem indexa.

---

## 2. Conflitos com o ROADMAP (schema v1.0 é a verdade)

- **Campos do ROADMAP superados** (não existem no schema v1.0): `classification_codes`, `classification_text`, `element_text`, `space_id`/`space_name`, `semantic_text`, `semantic_embedding`/`embedding_qwen3`, `evidence_refs`, `relations_summary`, `geometry`, `project_name`, `material` (singular). O `hbim_elements_v2` do ROADMAP está desatualizado e **não** é seguido.
- **`PropertyFact` e `ClassificationFact` NÃO têm `ifc_class`** (a lista do enunciado inclui-o; o schema não). Filtrar factos por classe exigiria denormalização no indexer (HBIM-022) — **fora do scope**; assinalado.
- **`PropertyFact.source` é uma string** `"pset"/"qto"` (Literal), **não** um `SourceRef`. Nos restantes records, `source` é um objeto `SourceRef`. Como os índices são **separados**, não há conflito — mas o mapping de cada índice reflete o tipo correto.
- **`PropertyFact.value` é polimórfico** (§5) — o problema central que separa mapping (HBIM-020) de projeção (HBIM-022).

---

## 3. Conjunto de mappings (ratificado)

Criar **exatamente quatro** mappings em `backend/canonical/mappings/`:

- `elements_v1.json`
- `property_facts_v1.json`
- `classification_facts_v1.json`
- `documents_v1.json`

**Não** criar `chunks_v1.json` — não existe `ChunkRecord` canónico; o mapping de chunks fica **adiado para HBIM-070**, quando existir um contrato explícito de chunk (nem placeholder vazio, que `dynamic:strict` bloquearia, nem mapping especulativo, que criaria contrato errado).

### 3.1 ClassificationFact tem mapping próprio
`ClassificationFact` **não** é incorporado em `ElementRecord`; **não** se inventam `classification_codes`/`classification_text`; **não** se perdem os records. Justificação:
- muitos factos por elemento (cardinalidade N:1);
- identidade própria (`classification_id`);
- filtros e agregações por `system`/`code`;
- atualizações independentes do elemento;
- **ausência de reverse links** no `ElementRecord` real (proibido inventá-los).

### 3.2 Localização e leitura
JSON versionados em `backend/canonical/mappings/`. A HBIM-020 cria **apenas ficheiros JSON**; os testes leem-nos **diretamente** com `json` + `pathlib`. **Sem loader Python, sem `__init__.py`, sem `load_mapping`/`iter_mapping_files`, sem subpacote `canonical.mappings` importável.** `backend/canonical` **não** ganha código novo nem dependência de OpenSearch — os JSON são dados. **O primeiro loader/consumidor pertence à HBIM-021** (quando os índices forem criados a partir dos mappings).

---

## 4. Forma dos ficheiros (mappings-only)

Cada ficheiro contém **apenas o objeto `mappings`** de um índice (o valor que HBIM-021 passará em `create_index` sob `"mappings"`). **Settings** (`number_of_shards`/`replicas`/`total_fields.limit`/analysis operacional) **não** vivem aqui — são HBIM-021.

```json
{
  "_meta": { "...": "§10 (exato)" },
  "dynamic": "strict",
  "_source": { "enabled": true },
  "properties": { "...": "§6–§9" }
}
```

As **únicas** chaves de topo permitidas são `_meta`, `dynamic`, `_source`, `properties`. `dynamic` é sempre `"strict"`. `_source.enabled` é sempre `true` (retrieval/reindex).

Normalizers/analyzers **locais ao mapping** são permitidos apenas se declarados dentro do próprio mapping via `fields` (multi-field); **não** se usa `normalizer` de índice (que exigiria `settings.analysis`, i.e. HBIM-021). Consequência: os campos keyword de identidade **não** têm normalizer (o que é exatamente o pretendido — ver §6.1).

---

## 5. Contrato de projeção do `PropertyValue` (crítico)

A serialização canónica real é `"value": {"value": <X>, "value_type": <T>}`, onde `value.value` recebe **string / int / float / bool / null** no **mesmo path**. OpenSearch **não** aceita um campo simultaneamente `text`+`long`+`double`+`boolean` → **conflito de mapping**. Logo:

**O mapping descreve o DOCUMENTO PROJETADO, não o JSONL canónico.** O JSONL canónico **não** é indexado diretamente. `property_facts_v1.json` define campos **tipados** e disjuntos:

| Campo no doc projetado | Tipo OpenSearch | Preenchido quando |
|---|---|---|
| `value_type` | keyword | **sempre** (`text`/`int`/`float`/`bool`/`null`) |
| `value_is_null` | boolean | **sempre** (`true` sse `value_type=="null"`, senão `false`) |
| `value_text` | text + `fields.keyword` | `value_type == "text"` |
| `value_integer` | long | `value_type == "int"` |
| `value_number` | double | `value_type == "float"` |
| `value_boolean` | boolean | `value_type == "bool"` |

**Projeção fechada (contrato para HBIM-022, não implementado aqui):**
- `value_type` **sempre** presente;
- `value_is_null` **sempre** presente; `value_is_null=false` para `text`/`int`/`float`/`bool`; `value_is_null=true` para `null`;
- tipos **não-null** têm **exatamente um** payload (`value_text` **xor** `value_integer` **xor** `value_number` **xor** `value_boolean`);
- o tipo `null` tem **zero** payloads (só `value_type="null"` + `value_is_null=true`);
- **nunca** dois payloads em simultâneo.

É **lossless**: `value_type` + (o único payload presente, ou `value_is_null`) reconstrói o `value` canónico. **Range numérico futuro consulta ambos `value_integer` e `value_number`** (int e float são payloads distintos). Consequências decididas:
- **`_source`**: o doc projetado é lossless → o `_source` do doc projetado basta; **não** se duplica o valor. **Não** se inclui uma cópia `enabled:false` do objeto `value` original (evita duplicação; se um consumidor futuro exigir round-trip byte-exato ao objeto canónico, é uma extensão de mapping documentada, não v1).
- **Fronteira**: HBIM-020 **só** define estes campos; a lógica de projeção é HBIM-022. `PropertyFact` e a serialização canónica **não** são alterados.

O mapping `elements_v1`/`classification_facts_v1`/`documents_v1` **não** têm `value` polimórfico e podem seguir mais de perto a forma canónica (com as exceções de tipo já assinaladas: `source` objeto vs string).

### 5.1 Limites de garantia dos mappings

Um mapping OpenSearch **define os campos permitidos e os tipos indexados** e nada mais. Concretamente:

- **`dynamic: "strict"`** rejeita campos **desconhecidos** (topo e internos).
- **`coerce: false`** rejeita **coerção** nos campos numéricos onde declarado.

Um mapping **não**:

- substitui a validação Pydantic do record canónico;
- impõe campos **obrigatórios** (um documento pode omitir campos declarados);
- impõe **cardinalidade** de arrays (0, 1 ou N são todos aceites);
- consegue impor **XOR** entre campos (nada impede dois payloads coexistirem no índice);
- verifica a **coerência** entre `value_type` e o payload presente.

Essas invariantes (presença obrigatória, `value_is_null`, XOR de payload, coerência `value_type`→payload) são responsabilidade da **projeção tipada da HBIM-022**, aplicadas **antes** da chamada ao OpenSearch.

**A HBIM-020 garante apenas** que os **seis** campos da projeção existem com tipos **estáticos e não conflituantes**: `value_type`, `value_is_null`, `value_text`, `value_integer`, `value_number`, `value_boolean`.

---

## 6. `elements_v1.json` (derivado de `ElementRecord`)

Todos os campos de `ElementRecord`; **nenhum** campo dinâmico; **nenhum** campo legacy (sem `semantic_text`, `material`, `spatial_hierarchy`, `project_name`, `semantic_embedding`).

### 6.1 Identidade (keyword, **sem** normalizer — `global_id` case-sensitive)
`schema_version`, `element_id`, `project_id`, `global_id`, `ifc_class` → `keyword`. **Proibido** normalizer lowercase em qualquer ID (corrige o anti-padrão legacy `lc`).

### 6.2 Texto
- `name` → `text` + `fields.keyword` (`ignore_above: 256`).
- `description` → `text`.
- `object_type` → `text` + `fields.keyword` (`ignore_above: 256`).
- `predefined_type` → `keyword`.
- `semantic_label` → `text` + `fields.keyword` (`ignore_above: 256`).

**Sem** analyzers linguísticos (só `standard` implícito no `text`). O mapping deriva-se do **contrato** do campo `semantic_label` (string opcional), independentemente de valores concretos numa dada corrida.

> **Strictness recursiva (obrigatória).** **Cada** `object`/`nested` desta issue declara explicitamente **`"dynamic": "strict"`** e um bloco `properties` explícito — em `materials`, `location`, `site`, `building`, `storey`, `space`, `parent_element`, `metrics` e `source`. Nenhum `object`/`nested` fica com `dynamic` herdado, `dynamic:true`, ou sem `properties`.

### 6.3 `materials` — **nested** (`dynamic:"strict"`)
Array de `MaterialRef` → **`nested`** com **`"dynamic": "strict"`**: `{ name: text+keyword, name_norm: keyword, role: keyword, ordinal: {type: integer, coerce: false} }`. **`materials.ordinal` é numérico com `coerce: false`** (rejeita string→integer).
*Justificação (queries futuras):* com `object` (array aplainado) a query "material com `role=layer` **e** `name=Granito`" casa se **existir** um role=layer **e** existir um name=Granito, **mesmo em materiais diferentes** (falso positivo). Com `nested`, casa só se **um** material tiver ambos. `nested` = correção de correlação por-material.

### 6.4 `location` — **object** (`dynamic:"strict"`)
`SpatialLocation` é single-valued (não array) → **`object`** (não `nested`) com **`"dynamic": "strict"`**: `site`/`building`/`storey`/`space`/`parent_element` → cada um `object` **com o seu próprio `"dynamic": "strict"`** e `{ global_id: keyword, id: keyword, name: text+keyword }`. `null` → sub-objeto ausente (sem `null_value`). IDs keyword sem normalizer.

### 6.5 `metrics` — **object** (`dynamic:"strict"`) de `double`
`object` com **`"dynamic": "strict"`**: `{ area, volume, height, thickness: double }`, `doc_values` (default, para range/agg), **`coerce: false`** (rejeita coerção silenciosa), `null` → campo ausente (sem `null_value`). Sem campos dinâmicos.

### 6.6 `source` — **object** (`dynamic:"strict"`, `SourceRef`)
`object` com **`"dynamic": "strict"`**: `{ source_id: keyword, ifc_schema: keyword, external_id: keyword, revision: keyword, checksum: keyword }`. `checksum` é um hash opaco indexado como **`keyword`** (exact match habilitado); identidade/hash **nunca** como `text`.

---

## 7. `property_facts_v1.json` (derivado de `PropertyFact`, **projetado**)

Campos reais + a projeção do valor (§5). **Sem `ifc_class`** (não no schema).

| Campo | Tipo | Nota |
|---|---|---|
| `schema_version`, `fact_id`, `project_id`, `element_id` | keyword | identidade |
| `source` | keyword | **string** `pset`/`qto` (não objeto) |
| `container` | keyword | nome do pset/qto, filtro/agg exato |
| `property_name` | text + `fields.keyword` | lexical + exato |
| `property_name_norm` | keyword | normalizado pelo produtor (NFC→strip→casefold); **as-is**, sem dupla normalização |
| `occurrence_key` | keyword | discriminador |
| `unit` | keyword | rótulo efetivo ou ausente |
| `value_type`, `value_text`, `value_integer`, `value_number`, `value_boolean`, `value_is_null` | §5 | projeção tipada |

**Pesquisas habilitadas (futuras):** filtro `project_id`/`element_id`/`source`/`container`/`property_name(.keyword)`/`property_name_norm`/`occurrence_key`/`unit`; `range` numérico sobre **`value_integer` e `value_number`** (ambos — int e float são payloads distintos); `term` sobre `value_boolean`; full-text sobre `value_text`; agregações por `property_name_norm`/`container`/`unit`. Filtro por `ifc_class` **não** é possível diretamente (não existe no record) → denormalização opcional em HBIM-022; assinalado.

**Prova anti-explosão (critério de aceitação):** `property_name` é um **valor** de um campo `keyword` fixo, **não** um nome de campo. Três IFCs com nomes de propriedade totalmente diferentes produzem valores diferentes no **mesmo** campo `property_name` → `mapping.total_fields` **constante**. `dynamic:strict` + zero objetos dinâmicos garante-o (contraste com o legacy `properties/quantities dynamic:true`).

---

## 8. `classification_facts_v1.json` (derivado de `ClassificationFact`)

**Sem `ifc_class`** (não no schema). Índice/mapping próprio (§3.1).

| Campo | Tipo |
|---|---|
| `schema_version`, `classification_id`, `project_id`, `element_id` | keyword |
| `system` | keyword |
| `code` | keyword |
| `name` | text + `fields.keyword` |
| `edition` | keyword |
| `location` | keyword |
| `source` | object (`SourceRef`, como §6.6) |

Habilita: filtros `system`/`code`, agregações por classificação (`terms` sobre keyword — corrige o bug legacy de agg sobre `text` nested), muitos por elemento, updates independentes.

---

## 9. `documents_v1.json` (derivado de `DocumentRef`)

**Sem** conteúdo/páginas/OCR/chunks/embeddings (adiado HBIM-070).

| Campo | Tipo |
|---|---|
| `schema_version`, `document_id`, `project_id`, `document_type` | keyword |
| `uri` | keyword |
| `title` | text + `fields.keyword` |
| `checksum` | keyword |
| `linked_element_ids` | keyword (**array**) |
| `source` | object (`SourceRef`, como §6.6) |

---

## 10. `_meta` e versionamento

Quatro versões **distintas**: **schema canónico** (`"1.0"`, contrato do record) ≠ **mapping** (versão do ficheiro, `_v1`) ≠ **índice físico** (`*_vN`, HBIM-021) ≠ **alias lógico** (HBIM-021). HBIM-020 versiona **ficheiros**; **sem** alias swap.

`_meta` — **exatamente** estas quatro chaves (sem `schema_version`, sem `compatibility`, sem timestamps; `created_by` em maiúsculas):
```json
"_meta": {
  "canonical_schema_versions": ["1.0"],
  "mapping_version": "1",
  "record_type": "<tipo>",
  "created_by": "HBIM-020"
}
```
`record_type` ∈ { `element`, `property_fact`, `classification_fact`, `document` }. OpenSearch preserva `_meta` no mapping (não indexado). Alterar o mapping incrementa `mapping_version` e (se incompatível) cria `*_v2.json` — nunca edita o `_v1` já promovido (política de HBIM-021).

---

## 11. Strictness, `_source`, settings

- **`"dynamic": "strict"`** em **todos** os mappings (núcleo anti-explosão + deteção de drift; rejeita campos não declarados).
- **`_source.enabled: true`** (retrieval e reindex futuros).
- **Sem** `knn`/`knn_vector`/campos de embedding/dimensão zembed 640. Uma **nova versão de mapping** adicionará o campo vetorial quando a dimensão for escolhida (HBIM-030/031).
- **Settings de índice** (`number_of_shards`/`number_of_replicas`/`mapping.total_fields.limit`/refresh) **não** estão nos ficheiros — são fornecidos por HBIM-021 no create-index. (Nota: `dynamic:strict` no mapping já impede explosão; `total_fields.limit` baixo será um guardrail adicional em HBIM-021.)

---

## 12. Import-safety

`backend/canonical` mantém-se **livre de OpenSearch**: a HBIM-020 acrescenta **só ficheiros JSON** (dados) e testes; **nenhum** código novo em `canonical/`, **nenhum** subpacote importável, **nenhum** `import opensearchpy`. A import-safety existente (HBIM-010/011/012: importar `canonical` não puxa `opensearchpy`/FastAPI/settings/`.env`) continua **trivialmente** a passar (não há loader).

---

## 13. Validação

### 13.1 Offline (sem OpenSearch) — obrigatória
Testes (leem os JSON com `json`+`pathlib`) que asseram:
1. **Forma**: chaves de topo == `{_meta, dynamic, _source, properties}`; `dynamic=="strict"`; `_source.enabled==true`.
2. **`_meta` exato**: chaves **exatamente** `{canonical_schema_versions, mapping_version, record_type, created_by}`; `canonical_schema_versions == ["1.0"]`; `mapping_version == "1"`; `created_by == "HBIM-020"`; `record_type` correto por ficheiro; **sem** `schema_version`, **sem** `compatibility`, **sem** chave com aparência de timestamp.
3. **Strictness recursiva**: percorrendo **recursivamente** cada `object`/`nested` (materials, location, site, building, storey, space, parent_element, metrics, source), o teste **falha** perante: `"dynamic": true`; qualquer `object`/`nested` **sem** `"dynamic": "strict"`; qualquer `object`/`nested` **sem** `properties`. **Nenhum** `"type": "knn_vector"`; **nenhum** campo de embedding em qualquer nível.
4. **Cobertura de campos == schema**: introspeção dos campos Pydantic (via `model_fields`) confirma que o mapping cobre **todos** os campos e **não tem campos a mais** (com a **expansão de projeção** do `PropertyValue` para `property_facts`: `value_type`/`value_is_null`/`value_text`/`value_integer`/`value_number`/`value_boolean`).
5. **IDs**: `element_id`/`project_id`/`global_id`/`fact_id`/`classification_id`/`document_id` são `keyword` **sem** `normalizer`.
6. **`coerce:false`** em `metrics.*`, **`materials.ordinal`**, `value_integer` e `value_number`.
7. **Determinismo**: os JSON são byte-stable (golden), sem timestamps.

### 13.2 Integração efémera (Testcontainers, OpenSearch 2.19.1) — **obrigatória**
Contra um cluster **local efémero** (loopback-only; **nunca** externo), cobrindo **os quatro mappings**:
- **index/get** de um **doc projetado sintético** por mapping (escrito à mão, já que a projeção é HBIM-022) → **aceitação** de documentos válidos e round-trip. Os testes usam um **`_id` sintético apenas** para validar index/get; isto **não** define o `_id` de produção — a **política real de `_id` pertence à HBIM-022**;
- **term** (ex.: `ifc_class`, `system`), **full-text** (ex.: `name`, `value_text`), **range** (`value_integer`/`value_number`, `metrics.*`), **nested** materials (correlação name+role no mesmo material), **aggregation** de classificação (`terms` sobre `code`/`system`);
- **rejeição de campo desconhecido de topo** e **rejeição de campo interno desconhecido** (ex.: sub-campo novo em `location`/`materials`/`source`) — `dynamic:strict` recursivo;
- **rejeição de coerção** (ex.: string num `value_integer`, `metrics.area` **ou `materials.ordinal`** com `coerce:false`);
- **prova de field-count estável**: indexar N factos com `property_name` diferentes → `mapping.total_fields` **não aumenta**;
- **cleanup**: cada índice de teste é removido no fim.

> **O que a integração NÃO testa (por design, §5.1):** o mapping **não** expressa presença obrigatória, XOR de payloads, nem coerência `value_type`→payload. **Não** se criam testes HBIM-020 que esperem que o OpenSearch **rejeite dois payloads em simultâneo** — essa invariante é garantida pela projeção da HBIM-022, não pelo mapping.

---

## 14. Compatibilidade / não tocar

**Não alterar:** `backend/canonical/{schema,ids,serialization}.py`; `PropertyFact`/serialização canónica; `index_to_opensearch.py` (legacy intacto até HBIM-021/022); `backend/api/**`; `backend/eval/**` e a **baseline HBIM-005**; frontend; `.gitignore`; `local_data/**`; `config.OPENSEARCH_INDEX`/`EMBEDDING_*`. HBIM-020 **só adiciona** ficheiros JSON de mapping + testes.

---

## 15. Ficheiros previstos

**Criar:**
- `backend/canonical/mappings/elements_v1.json`
- `backend/canonical/mappings/property_facts_v1.json`
- `backend/canonical/mappings/classification_facts_v1.json`
- `backend/canonical/mappings/documents_v1.json`
- `backend/tests/test_index_mappings.py` (offline; lê os JSON com `json`+`pathlib`)
- `backend/tests/integration/test_index_mappings_apply.py` (efémero, **obrigatório**)
- esta spec.

**Sem loader Python** (`backend/canonical/mappings/__init__.py`, `load_mapping`, `iter_mapping_files`) — o primeiro consumidor é HBIM-021.

**Modificar (só se necessário):** `docs/development/LOCAL_SETUP.md`, `docs/implementation/IMPLEMENTATION_STATUS.md`. (Não há código de produção novo → sem alteração a `pyproject.toml`/`ci.yml`.)

**Não tocar:** listados em §14.

---

## 16. Tooling e CI

**Sem código de produção novo** (só JSON de dados + testes) → **sem alteração ao gate mypy** e **sem novo módulo** no gate. Ruff limpo (testes); testes offline no job `backend-unit`; validação de integração reutiliza o job `integration-opensearch` (Testcontainers já existente), **sem novo job**; sem serviços externos; sem ML; `evaluation-opensearch` inalterado.

---

## 17. Critérios de aceitação

Cada critério mapeia para teste/ficheiro/evidência.

1. **Quatro mappings** JSON em `backend/canonical/mappings/` (`elements`/`property_facts`/`classification_facts`/`documents`, `_v1`).
2. **Nenhum `chunks`** (nem placeholder nem especulativo).
3. **ClassificationFact separado** (mapping próprio; **sem** `classification_codes`/reverse-links no elemento).
4. **Mappings-only**: cada ficheiro só tem `{_meta, dynamic, _source, properties}` (sem `settings`).
5. **`_meta` exato**: `{canonical_schema_versions:["1.0"], mapping_version:"1", record_type:<tipo>, created_by:"HBIM-020"}` — sem `schema_version`, sem `compatibility`, sem timestamps.
6. **Strictness de topo**: `dynamic:"strict"` + `_source.enabled:true` no topo de cada mapping.
7. **Strictness recursiva**: cada `object`/`nested` (materials/location/site/building/storey/space/parent_element/metrics/source) tem `dynamic:"strict"` e `properties`; testes recursivos falham perante `dynamic:true`, `object/nested` sem strict, ou sem `properties`.
8. **Coverage exata do schema**: cobertura == `model_fields` de cada record, sem campos a mais/legacy/inventados.
9. **Projection PropertyFact (HBIM-020)**: os **seis** campos projetados existem com tipos estáticos disjuntos (`value_type` keyword, `value_is_null` boolean, `value_text`, `value_integer`, `value_number`, `value_boolean`); **nenhum path polimórfico**; coverage mapeia todos os campos projetados.
10. **HBIM-020 aceita doc projetado válido**: a spec **documenta** o contrato obrigatório da projeção e um **doc projetado sintético válido é aceite** (index/get); a HBIM-020 **não** afirma nem testa presença obrigatória, XOR ou coerência de payload.
11. **Invariantes de projeção → HBIM-022** (fronteira): presença obrigatória de `value_type`/`value_is_null`, `value_is_null` correto, **payload XOR** (exatamente um nos não-null, zero no null), **coerência `value_type`→payload**, e impedir documentos projetados inválidos de chegarem ao OpenSearch — implementadas e testadas em HBIM-022. A HBIM-020 **não** testa rejeição de dois payloads em simultâneo (o mapping não expressa XOR — §5.1).
12. **IDs case-sensitive** (`keyword` sem normalizer lowercase).
13. **Checksums indexáveis** (`keyword`, sem `index:false`).
14. **Materials nested**.
15. **`coerce:false`** em `metrics.*`, **`materials.ordinal`**, `value_integer`, `value_number`.
16. **Ausência de vetores**: sem `knn`/`knn_vector`/embeddings/dimensão zembed.
17. **Integração OpenSearch obrigatória** (Testcontainers efémero, os 4 mappings).
18. **Documentos válidos aceites** (index/get round-trip por mapping).
19. **Campos top-level desconhecidos rejeitados**.
20. **Campos internos desconhecidos rejeitados** (sub-campos em object/nested).
21. **Coerção numérica rejeitada** (string num campo numérico com `coerce:false`).
22. **term/full-text/range/nested/aggregation funcionam** sobre os campos declarados.
23. **Field count estável**: N `property_name` diferentes não aumentam `total_fields`.
24. **Ausência de loader/OpenSearch imports**: `backend/canonical` só ganha JSON; sem `__init__.py`/loader novo; sem `opensearchpy`; import-safety inalterada.
25. **Legacy/API/retrieval inalterados** (`index_to_opensearch.py`, `backend/api/**`, `backend/canonical/{schema,ids,serialization}`, serialização canónica).
26. **Baseline byte-idêntica** (HBIM-005 `current_system.json` sha256 inalterado).
27. **CI sem job novo** (integração reutiliza `integration-opensearch`).
28. **Nenhum segredo/IFC real** (sem `.env`, sem `.ifc` committed).
29. **Ruff/testes verdes**; JSON golden byte-stable.

---

## 18. Riscos residuais

- **Mapping ≠ JSONL canónico** (doc projetado): risco de desalinhamento com o indexer HBIM-022 → mitigar com o contrato de projeção §5 explícito + testes de projeção em HBIM-022.
- **`ifc_class` ausente** em property/classification facts → filtros por classe adiados/denormalização (HBIM-022).
- **`nested` materials** tem custo de query (aceitável; correção > performance nesta fase).
- **Settings fora dos ficheiros**: o guardrail `total_fields.limit` só existe em HBIM-021; `dynamic:strict` já cobre a explosão entretanto.

---

## 19. Fora de scope

Índices físicos/aliases/promoção/rollback/settings operacionais/remoção do `delete+create` (HBIM-021); projection code/indexers/JSONL/bulk/`_id`/round-trip (HBIM-022); chunks/documento-conteúdo/OCR (HBIM-070); embeddings/`knn`/dimensão (HBIM-030/031); geometria/relations_summary (posterior); alteração de `PropertyFact`/serialização/`backend/canonical` schema; baseline HBIM-005.

---

## 20. Questões (decisão de review)

Nenhuma bloqueia a implementação; todas ratificadas: (a) 4 índices, chunks adiado HBIM-070; (b) mapping = doc **projetado**, projeção do `PropertyValue` em HBIM-022; (c) `_source` lossless **sem** cópia `enabled:false` do value original; (d) `materials` **nested**, `location`/`source` **object**; (e) `ifc_class` ausente em property/classification facts assinalado, denormalização adiada; (f) **sem loader** na HBIM-020 — só JSON lido diretamente por testes (`json`+`pathlib`); o primeiro consumidor/loader é HBIM-021; (g) settings operacionais em HBIM-021 (ficheiros são mappings-only); (h) `checksum` indexado como `keyword` (exact match), não `index:false`.
