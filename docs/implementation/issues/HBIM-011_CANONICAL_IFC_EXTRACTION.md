# HBIM-011 — Conversão IFC para records canónicos

> **Tipo:** especificação executável de issue.
> **Branch obrigatória:** `feat/hbim-011-canonical-ifc-extraction`.
> **Precede:** HBIM-012 (atomização/dedup avançada de PropertyFact).
> **Depende de:** HBIM-010 (schema canónico, merged) — fonte de verdade.

---

## 0. Precedência e fonte de verdade

A HBIM-010 **implementada e merged** (`backend/canonical/`) é a fonte de verdade
e é **imutável** nesta issue para: modelos canónicos, IDs, `fact_id` **sem
valor**, SHA-256 sobre **netstrings**, relações entre records, JSON/JSONL
canónico e `schema_version = "1.0"`.

A descrição antiga do **ROADMAP §M1** (SHA-1, valor dentro do `fact_id`, campos
`space_id`/`name_normalized`/`semantic_label`/`classification_codes`/
`element_text` no `ElementRecord`) está **superada** e **não** é seguida.

Helpers de ID — nomes **reais** de `backend/canonical/ids.py` (nunca `make_*`):

- `element_id(project_id, global_id)`
- `property_fact_id(project_id, element_id, source, container, property_name, occurrence_key)`
- `classification_id(project_id, element_id, system, code, occurrence_key="0")`
- `document_id(project_id, uri)`

Serialização — API pública **reutilizada** de `backend/canonical/serialization.py`:

- `to_canonical_json(model) -> str` (forma canónica por-record: `sort_keys=True`,
  `ensure_ascii=False`, `allow_nan=False`, separadores compactos). Usada para
  **todos** os payloads serializados desta issue, incluindo `CoverageReport` e
  `ExtractionWarning` (§19.3), que por isso são **modelos Pydantic estritos** na
  camada de ingestão — nunca se assume que o helper aceita uma dataclass
  arbitrária.
- `to_jsonl(...)` **não** é usado: exige a lista completa em memória e ordena por
  id-hash; a HBIM-011 escreve em streaming por ordem de `global_id` (§16).

`backend/canonical` **permanece independente** de IfcOpenShell, OpenSearch,
FastAPI e settings. A HBIM-011 só o **importa** (API pública); não o modifica.

---

## 1. Objetivo

```
IFC → IfcOpenShell → records canónicos validados → ficheiros JSONL determinísticos
```

Toda a lógica dependente de IfcOpenShell vive em `backend/ingestion/`. A saída é
um conjunto de ficheiros JSONL canónicos (byte-stable) mais `coverage.json` e
`warnings.jsonl`, publicados **atomicamente por diretório** (§17), adequados a
indexação futura (fora do scope).

### 1.1 Scope — inclui

Leitura do IFC; identidade explícita de projeto/fonte; `ElementRecord` para
**IfcElement candidates (incluindo `IfcOpeningElement`)** e `IfcSpace`;
`SourceRef`; `SpatialLocation`/`SpatialRef` (dois regimes de contenção);
`MaterialRef`; `Metrics`; `ClassificationFact`; `DocumentRef` mínimo com
**agregação muitos-para-muitos** (§14); **`PropertyFact` escalar**; herança de
atributos de tipo (sem `TypeRecord`); serialização JSONL determinística com
**streaming de buffering limitado** e **escrita atómica por diretório**; CLI
reproduzível; testes unitários sintéticos; import-safety.

### 1.2 Scope — exclui (delegado)

Atomização/dedup avançada de PropertyFact; listas/enumerações/tabelas/bounded
values/referências; resolução completa de unidades; aliases e normalização
**semântica** — tudo **HBIM-012**. Mappings/indexação OpenSearch, Neo4j, routing,
hybrid, documentos/OCR/chunks/embeddings, geometria e multimodal — posteriores.
**Não** se altera o extractor legacy, o indexer, o retrieval, a API, o frontend,
os mappings nem a baseline HBIM-005.

---

## 2. Arquitetura e módulos

Modelos e serialização ficam em `canonical/`; lógica IfcOpenShell em
`ingestion/`. Quatro módulos novos:

| Módulo | Responsabilidade |
|---|---|
| `backend/ingestion/canonical_ifc.py` | API pública (`convert_ifc_to_canonical`, `write_canonical_jsonl`), orquestração, identidade, `SourceRef`, `ElementRecord`, `PropertyFact`, `ClassificationFact`, acumulador+emissão de `DocumentRef` (§14), `CoverageReport`/`ExtractionWarning` (Pydantic), erros tipados, escrita atómica por diretório (§17), CLI, e o helper **privado** `_iter_entity_records` (§3.3). |
| `backend/ingestion/ifc_spatial.py` | `SpatialLocation` (dois regimes), cache da cadeia site→building→storey→space, `parent_element`, deteção de órfãos/ciclos. |
| `backend/ingestion/ifc_materials.py` | `IfcMaterial*` → `list[MaterialRef]`, ordem determinística, material sem nome. |
| `backend/ingestion/ifc_values.py` | Leitura segura de escalares, bool/int/float, finitude, unidades básicas, strings, **normalização lexical**, conversão para `PropertyValue`, classificação de valores unsupported. |

**Proibição explícita:** não importar funções privadas de `extract_bim.py`
(`_to_float`, `_length_unit_to_m_factor`, `get_normalized_value`, `sanitize_keys`).
`ifc_values.py` reimplementa esta lógica, tipada e testada. `extract_bim.py`
permanece **intacto**.

`canonical_ifc.py` importa apenas: `ifcopenshell` (+ `ifcopenshell.util.element`),
os três módulos irmãos e a **API pública** de `canonical`. **Nunca** importa
OpenSearch/FastAPI/settings; **nunca** lê `.env`; **nunca** abre sockets no import.

---

## 3. API pública

```python
def convert_ifc_to_canonical(
    source_path: str | Path,
    *,
    project_id: str,
    source_id: str,
    expected_ifc_project_global_id: str | None = None,
) -> CanonicalExtractionResult: ...

def write_canonical_jsonl(
    source_path: str | Path,
    *,
    project_id: str,
    source_id: str,
    output_dir: str | Path,
    expected_ifc_project_global_id: str | None = None,
) -> CoverageReport: ...
```

**Não existe iterator público** nem parâmetro `overwrite`. A iteração é um helper
**privado** `_iter_entity_records` (§3.3). Warnings, coverage e lifecycle são
devolvidos **apenas** pelas duas APIs de lifecycle completo acima. A publicação é
sempre para um **`output_dir` novo** (§17).

### 3.1 Tipos de resultado e payloads serializados

```python
@dataclass(frozen=True, slots=True)
class CanonicalExtractionResult:          # container em memória; não é serializado
    source: SourceRef
    elements: tuple[ElementRecord, ...]
    property_facts: tuple[PropertyFact, ...]
    classification_facts: tuple[ClassificationFact, ...]
    documents: tuple[DocumentRef, ...]
    warnings: tuple[ExtractionWarning, ...]
    coverage: CoverageReport

class ExtractionWarning(BaseModel):       # Pydantic ESTRITO (extra="forbid")
    code: WarningCode                      # enum fechado (§19.1)
    ifc_class: str
    reference: str | None = None           # identificador OPACO (GlobalId / document_id); nunca nome/URI/path
    field: FieldCode | None = None         # enum fechado (§19.1); NUNCA nomes reais
    detail_code: DetailCode | None = None  # enum fechado (§19.1)
    occurrences: int                       # ≥1; agregação de warnings idênticos (§19.2)

class CoverageReport(BaseModel):          # Pydantic ESTRITO (extra="forbid")
    ...                                    # ver §19.3 (só inteiros/categorias/códigos)
```

`ExtractionWarning` e `CoverageReport` são **modelos Pydantic estritos** na
camada de ingestão, serializados por `to_canonical_json` (§19.3). As exceções
tipadas vivem em `canonical_ifc.py`; **nada** disto entra em `backend/canonical`.

### 3.2 Justificação (tipos, memória, ordem, erros, ficheiros, lifecycle)

- **Tipos.** `project_id`/`source_id` obrigatórios não vazios (vazio → aborta,
  §4). `convert_*` devolve resultado **materializado** (tuplos imutáveis);
  `write_*` devolve só o `CoverageReport` e publica os ficheiros num `output_dir`
  novo, atomicamente.
- **Memória.** IfcOpenShell **carrega o modelo inteiro** — o pico é dominado pelo
  IFC. A HBIM-011 **não duplica** essa materialização: mantém apenas (a) um buffer
  **leve** de referências de entidade `(global_id, ifc_class, entity_id)`, (b) o
  **acumulador leve de documentos** `document_id → (metadados, set[element_id])`
  (§14), e (c) o working-set de **uma** entidade de cada vez. `convert_*`
  materializa tudo (fixtures/modelos pequenos); modelos grandes usam `write_*`.
- **Ordem.** Determinística e independente da ordem do IFC (§16).
- **Erros.** Abort tipado vs warning+coverage segundo §19; exceções inesperadas
  **propagam** com a causa (sem `except Exception` amplo por entidade).
- **Ficheiros produzidos.** `write_*` **cria** `output_dir` (que **não** pode
  pré-existir) por rename de um staging (§17), contendo: `elements.jsonl`,
  `property_facts.jsonl`, `classification_facts.jsonl`, `documents.jsonl`,
  `coverage.json`, `warnings.jsonl`.
- **Lifecycle do IFC.** Aberto uma vez com `ifcopenshell.open`; **nunca**
  modificado/reescrito; libertado no fim. Sem locks, sem cópias. O `output_dir`
  e o staging **não** podem coincidir com o ficheiro IFC de origem (§17.1).

### 3.3 Iteração interna (`_iter_entity_records`)

Helper privado, **entity-scoped**: por entidade candidata (na ordem estável do
§16) produz o `ElementRecord` seguido dos seus `PropertyFact` e
`ClassificationFact`. **Não** produz `DocumentRef` (muitos-para-muitos → só após
a passagem completa, §14). Não expõe warnings/coverage — esses pertencem às APIs
de lifecycle. Só é usado internamente por `convert_*`/`write_*`; não há iterator
público, logo o comportamento após interrupção não é contrato público.

---

## 4. Identidade de projeto e fonte

### 4.1 `project_id`
Explícito; identifica o **projeto lógico**; **nunca** derivado silenciosamente,
do checksum ou do `IfcProject.GlobalId`; **estável entre revisões**. Vazio →
**aborta** (`EmptyIdentityError`). Usado verbatim em todos os IDs.

### 4.2 `source_id`
Explícito; identifica a **fonte IFC lógica**; **pode** manter-se entre revisões
da mesma fonte; **não** depende do path absoluto nem do timestamp; **não** é
substituído pelo checksum. Vazio → **aborta**. Vai para `SourceRef.source_id`.

### 4.3 `IfcProject.GlobalId`
- **Zero:** permitido; `SourceRef.external_id = None`.
- **Um:** guardado como proveniência em `SourceRef.external_id`. **Não** é
  comparado automaticamente com `project_id`.
- **>1:** **aborta** (`MultipleIfcProjectError`) — erro só com classe e contagem.
- **Comparação** só se o chamador passar `expected_ifc_project_global_id`;
  divergência (case-sensitive, verbatim) **aborta** (`IfcProjectMismatchError`),
  não é warning.

### 4.4 Checksum
`SHA-256` do conteúdo (leitura em blocos) → `SourceRef.checksum`. Proveniência da
revisão; **nunca** em `element_id`/`fact_id`/`classification_id`/`document_id`;
**não** substitui `project_id`/`source_id`.

### 4.5 `SourceRef`
`source_id` (verbatim), `ifc_schema` (§18), `checksum` (proveniência),
`external_id` (`IfcProject.GlobalId` 0/1), `revision=None` (reservado).
**Estabilidade:** `element_id` depende só de `(project_id, GlobalId)` → estável
mesmo mudando geometria/checksum.

---

## 5. GlobalIds

Verbatim, **case-sensitive**, nunca lowercased/normalizado; obrigatório para
records com identidade IFC. Dois GlobalIds que diferem só em case são **distintos**.

**Entidade relevante sem GlobalId:** não produz `ElementRecord`; warning
`MISSING_GLOBAL_ID` + cobertura; omitida só como condição de dados conhecida.

**GlobalId duplicado no mesmo `project_id`:** **aborta toda a conversão**
(`DuplicateGlobalIdError`), **antes** de qualquer escrita (§16 passo 2, §17). Não
se mantém "o primeiro", não se escolhe pela ordem do IFC. O erro identifica só a
**classe IFC** e o **GlobalId** (identificador seguro), sem paths/conteúdo.

---

## 6. IfcSpace

Emitido como **`ElementRecord`**: `ifc_class="IfcSpace"`,
`element_id(project_id, space.GlobalId)`, com `name`/`description`/`object_type`/
`predefined_type`/`metrics` e `materials`/`PropertyFact`/`ClassificationFact`
quando existirem.

- **Elemento (IfcElement) num espaço:** `location.space` → `SpatialRef` desse
  espaço.
- **No próprio space:** `location.site/building/storey` podem estar preenchidos;
  **`location.space = None`** (sem autorreferência).

Site/building/storey permanecem **`SpatialRef`**, nunca `ElementRecord`.

---

## 7. Resolução espacial (`ifc_spatial.py`)

`SpatialLocation` a partir de `get_container` + `get_aggregate`, com cache da
cadeia ascendente por GlobalId.

- **IFC4:** elemento → `IfcSpace` → `IfcBuildingStorey` → `IfcBuilding` → `IfcSite`.
- **IFC2X3:** elemento → `IfcBuildingStorey` → `IfcBuilding` → `IfcSite` (`space=None`).

```
c = get_container(element)
if   c.is_a("IfcSpace"):          space=c; storey=up(c); building=up(storey); site=up(building)
elif c.is_a("IfcBuildingStorey"): storey=c; space=None; building=up(c); site=up(building)
elif c.is_a("IfcBuilding"):       building=c; site=up(c)
elif c.is_a("IfcSite"):           site=c
else:                             tudo None → ORPHAN_ELEMENT
parent_element = get_aggregate(element) se o pai for IfcElement
```

Casos: direto em building/site; **órfão**; **relação incompleta** (nível em falta
→ `INCOMPLETE_SPATIAL_RELATION`, `field` `SPATIAL_STOREY`/`SPATIAL_BUILDING`/
`SPATIAL_SITE`, `detail_code` `MISSING_STOREY`/`MISSING_BUILDING`/`MISSING_SITE`);
**agregação IfcElement↔IfcElement** (`parent_element`); **ciclos** → guarda de
visitados aborta o loop local com `INCOMPLETE_SPATIAL_RELATION`/`SPATIAL_CYCLE`
(nunca recursão infinita, nunca exceção inesperada).

**`SpatialRef`** distingue, sem dicionários livres: `id`
(`element_id(project_id, node.GlobalId)` quando derivável, senão `None`),
`global_id` (verbatim ou `None`), `name` (vazio → `None`).

---

## 8. Tipos IFC (herança de atributos)

Não se cria `TypeRecord`. Resolvem-se apenas atributos escalares herdados via
`get_type`/`IfcRelDefinesByType`, com precedência:

1. valor **explícito da instância**;
2. valor **resolvido do tipo**;
3. `None`.

Aplica-se a `object_type` (instância `ObjectType`, senão `type.ObjectType`/
`type.Name`) e `predefined_type` (instância `PredefinedType`, senão
`type.PredefinedType`; `USERDEFINED`/`NOTDEFINED` como strings verbatim). Valor
herdado (origem 2) incrementa `inherited_type_attributes` na cobertura (sem
warning; é condição normal).

---

## 9. `ElementRecord` (mapa)

Campos de `canonical.schema.ElementRecord` (v1.0). **Não** se adicionam
`properties`/`quantities`/reverse-links/`semantic_text`/dicionários livres.

| Campo | Origem | Regra | Obr. | Ausente |
|---|---|---|---|---|
| `schema_version` | — | const `"1.0"` | O | — |
| `element_id` | — | `element_id(project_id, GlobalId)` | O | — |
| `project_id` | argumento | verbatim | O | aborta |
| `global_id` | `element.GlobalId` | **verbatim** | O | sem record + `MISSING_GLOBAL_ID` |
| `ifc_class` | `element.is_a()` | verbatim | O | — |
| `name` | `element.Name` | vazio/whitespace → `None` | o | `None` |
| `description` | `element.Description` | vazio → `None` | o | `None` |
| `object_type` | instância/tipo (§8) | precedência | o | `None` |
| `predefined_type` | instância/tipo (§8) | precedência | o | `None` |
| `semantic_label` | — | **sempre `None`** (§9.1) | o | `None` |
| `materials` | `get_material` | §11 | o | `[]` |
| `location` | contentor/agregação | §7 | O | refs `None` |
| `metrics` | psets/qtos + unidade | §10 | O | tudo `None` |
| `source` | ficheiro/schema | §4.5 | O | — |

### 9.1 `semantic_label`
**Fica `None`** (ratificado). Não se cria mapa `IfcClass→semantic_label`
(normalização semântica → HBIM-012). Só seria preenchido com fonte IFC explícita,
que não existe.

### 9.2 IFC `Tag` (ratificado — só cobertura)
O `ElementRecord` v1.0 **não tem campo `tag`**; adicioná-lo alteraria o contrato
frozen e **quebraria** os golden fixtures byte-stable da HBIM-010. A HBIM-011
**não** mapeia `Tag` para o `ElementRecord`; apenas **contabiliza** a sua presença
na cobertura (`tag_present`). **`Tag` queryável exige um schema canónico 1.1
separado** (emenda HBIM-010: novo campo + reemissão de golden fixtures), fora do
scope desta issue.

---

## 10. `Metrics` — candidatos fixos e ordenados

Unidade canónica **SI**; valores convertidos pelo fator de comprimento do modelo
(`ifc_values.length_unit_factor`): `area × f²`, `volume × f³`, `height/thickness × f`.
**Qto tem precedência sobre pset** (prioridades menores). A lista é **fixa nesta
spec** (não é deixada à implementação); expansão pertence à HBIM-012.

| Métrica | Dim. | Unidade | Prio | Source | Nome do candidato |
|---|---|---|---|---|---|
| `area` | L² | m² | 1 | qto | `NetArea` |
| | | | 2 | qto | `GrossArea` |
| | | | 3 | qto | `NetSideArea` |
| | | | 4 | qto | `GrossSideArea` |
| | | | 5 | pset | `Area` |
| `volume` | L³ | m³ | 1 | qto | `NetVolume` |
| | | | 2 | qto | `GrossVolume` |
| | | | 3 | pset | `Volume` |
| `height` | L | m | 1 | qto | `Height` |
| | | | 2 | qto | `NetHeight` |
| | | | 3 | pset | `Height` |
| `thickness` | L | m | 1 | qto | `Width` |
| | | | 2 | qto | `Thickness` |
| | | | 3 | pset | `Thickness` |

**Seleção (determinística):** percorre os candidatos por prioridade crescente;
escolhe o **primeiro presente cujo valor é numérico e finito** (após conversão).
Candidato presente mas não finito → `NON_FINITE_VALUE` (`field` = `METRIC_AREA`/
`METRIC_VOLUME`/`METRIC_HEIGHT`/`METRIC_THICKNESS`, `detail_code` `NAN`/`INF`) e
continua para o seguinte. Nenhum válido → `None`.

**Duplicados:** se ≥2 candidatos da métrica estiverem **presentes** no modelo,
regista-se `METRIC_MULTIPLE_CANDIDATES` (`field` = `METRIC_*`, uma vez por
métrica/elemento); a seleção segue a prioridade (não a ordem do IFC).

**Vetores de teste obrigatórios:**
- qto `NetArea`=5, pset `Area`=9 → `area=5` + `METRIC_MULTIPLE_CANDIDATES(field=METRIC_AREA)`.
- só pset `Area`=9 → `area=9`, sem flag.
- qto `GrossArea`=10, qto `NetArea`=4 → `area=4` (prio 1<2) + flag.
- qto `Height`=Inf, pset `Height`=3 → `height=3` + `NON_FINITE_VALUE(field=METRIC_HEIGHT)`.

Conversão de unidades em `ifc_values.py` (tipado); **não** importada de
`extract_bim.py`.

---

## 11. Materiais (`ifc_materials.py`)

Suporta `IfcMaterial`, `IfcMaterialList`, `IfcMaterialLayer`,
`IfcMaterialLayerSet`, `IfcMaterialLayerSetUsage`, `IfcMaterialConstituent`,
`IfcMaterialConstituentSet` e `IfcMaterialProfile*` quando o schema o suportar.

`MaterialRef`: `name` (**O**, NonEmpty), `role` (layer/constituent/profile),
`ordinal` (índice estrutural ≥0), `name_norm` (**`None`** — material é semântico →
HBIM-012). Ordem determinística por `(ordinal, name)` (validator HBIM-010).
**Material sem nome:** omitido com `MATERIAL_WITHOUT_NAME` + cobertura; **nunca**
string vazia. Sem geometria/composição física.

---

## 12. Classificações

`ClassificationFact` para `IfcRelAssociatesClassification` →
`IfcClassificationReference`.

| Canónico | IFC4 | IFC2X3 |
|---|---|---|
| `system` (**O**) | `ReferencedSource.Name` | idem |
| `code` (**O**) | `Identification` | `ItemReference` |
| `name` | `Name` | `Name` |
| `edition` | `ReferencedSource.Edition` | idem (quando disponível) |
| `location` | `Location` | `Location` |
| `source` | `SourceRef` | idem |

`classification_id(project_id, element_id, system, code)` (`occurrence_key="0"`).
Incompleta (sem `system`/`code`) → sem record inválido; cobertura +
`INCOMPLETE_CLASSIFICATION` (`detail_code` `MISSING_SYSTEM`/`MISSING_CODE`).
Fixtures sintéticas exercem IFC4 e IFC2X3.

---

## 13. `PropertyFact` escalar

Apenas escalares representáveis pela união `PropertyValue`
(`text/int/float/bool/null`).

| Campo | Regra |
|---|---|
| `fact_id` | `property_fact_id(project_id, element_id, source, container, property_name, occurrence_key)` |
| `source` | `"pset"` ou `"qto"` |
| `container` | nome do pset/qto **verbatim** (sem `.`→`_`) |
| `property_name` | **verbatim** |
| `property_name_norm` | §13.1 |
| `occurrence_key` | `"0"` (multi-ocorrência → HBIM-012) |
| `unit` | separado do valor; `None` se ausente |
| `value` | `PropertyValue` discriminado |

`fact_id` usa o `property_name` **original**, nunca o valor nem a normalização.
Nota: `container`/`property_name` verbatim entram nos **records** canónicos (o
consumidor precisa deles), mas **nunca** aparecem em warnings/coverage (§19).

### 13.1 Normalização lexical (ratificada, permanente)
```
property_name_norm = casefold( strip( NFC(property_name) ) )
```
Lexical, não semântica. A HBIM-012 pode acrescentar normalização semântica
**noutro campo**, sem alterar o significado deste. **Edge case:** se a norma
resultar vazia (nome só com whitespace), o facto **não** é emitido; cobertura +
`EMPTY_NORMALIZED_PROPERTY_NAME` (`field`=`PROPERTY_NAME`).

### 13.2 Limite HBIM-012
Listas, enumerações, bounded values, tabelas, referências, atomização de
múltiplos valores, resolução completa de unidades, dedup avançada, aliases e
normalização semântica → **HBIM-012**. Valores não escalares: **nunca** via
`str()`; não produzem facto; entram na cobertura com estado `planned_atomization`
(list/enum/table/bounded) ou `unsupported_v1` (referências); warning
`COMPLEX_PROPERTY_VALUE` (`field`=`PROPERTY_VALUE`, `detail_code` `VALUE_LIST`/
`VALUE_ENUM`/`VALUE_TABLE`/`VALUE_BOUNDED`/`VALUE_REFERENCE`/`VALUE_UNKNOWN`).

---

## 14. `DocumentRef` — agregação muitos-para-muitos

Um documento associado a **vários** elementos produz **exatamente um**
`DocumentRef`, com `linked_element_ids` ordenados e deduplicados. **Não** se
escreve `DocumentRef` durante o processamento do primeiro elemento.

### 14.1 Acumulador leve (estado global)
Durante a passagem pelos elementos, para cada `IfcRelAssociatesDocument`:
- deriva `uri` (`Location`, senão `Identification`); sem `uri` → sem record;
  cobertura + `INCOMPLETE_DOCUMENT` (`field`=`DOCUMENT_URI`, `detail_code`
  `MISSING_URI`).
- `document_id = document_id(project_id, uri)`.
- **acumulador**: `document_id → (title, document_type, source, set[element_id])`.
  Na 1ª ocorrência guarda os metadados estáveis; em cada ocorrência adiciona o
  `element_id` ao set. **Só este estado leve fica em memória** (nº documentos ≪
  elementos).

### 14.2 Conflitos de metadados (determinísticos, não silenciosos)
Se o mesmo `document_id` reaparecer com `title` ou `document_type`
**contraditórios**, **não** se escolhe pela ordem de processamento:
- resolução determinística = **valor lexicograficamente menor** (para `title`
  não-`None`; para `document_type`);
- emite-se `DOCUMENT_METADATA_CONFLICT` (`field`=`DOCUMENT_URI`, `detail_code`
  `TITLE_CONFLICT`/`TYPE_CONFLICT`, `reference` = `document_id` opaco) + cobertura.

### 14.3 Emissão (após a passagem)
Terminada a passagem pelos elementos: para cada `document_id` **ordenado por
`document_id`**, constrói-se um `DocumentRef` único com
`linked_element_ids = sorted(set(...))` (o validator HBIM-010 ordena/dedup) e
`document_type` (§14.4). Escrito no `documents.jsonl` (staging) nessa ordem, **uma
única vez**.

### 14.4 `document_type` (ratificado)
`Scope`/`Purpose` do documento quando existir; caso o IFC **não** forneça tipo,
constante reservada **`"ifc_document"`** (open string, NonEmpty).

Sem leitura/PDF/OCR/páginas/chunks/embeddings/crawling/download/parsing. Fixtures
sintéticas incluem ≥1 relação documental **partilhada por ≥2 elementos**.

---

## 15. `ifc_values.py`

Tipado, no gate mypy bloqueante: `read_scalar` (desembrulha `wrappedValue`;
distingue bool/int/float/str/None; deteta não-finitos; classifica não-escalares
sem `str()`); `to_property_value` (variante correta; `int`≠`float`, `bool`≠`int`
per HBIM-010); `normalize_lexical` (NFC→strip→casefold); `length_unit_factor`/
`to_si` (comprimento/área/volume); `unit_label`. Sem estado global, sem rede, sem
`.env`.

---

## 16. Determinismo e streaming

### 16.1 Tensão resolvida
Escrita incremental **e** ordem global determinística coexistem via **buffering
limitado de referências** (não de records). Não se usa `to_jsonl`; usa-se
`to_canonical_json` por-record na ordem pré-definida, escrevendo para um
**staging directory** (§17) publicado por rename único no fim.

### 16.2 Algoritmo
1. **Referências leves** de candidatos (**IfcElement candidates, incluindo
   `IfcOpeningElement`**, + `IfcSpace`): `(global_id, ifc_class, entity_id)`.
2. **Validar GlobalIds:** ausência → warning+cobertura (omitir); **duplicado →
   aborta** (§5), antes de criar o staging.
3. **Ordenar** por `(global_id, ifc_class)`.
4. **Converter uma entidade de cada vez** (via `_iter_entity_records`); escrever
   `ElementRecord`/`PropertyFact`/`ClassificationFact` nos ficheiros do **staging**;
   alimentar o **acumulador de documentos** (§14).
5. **Ordenar intra-entidade** materiais/properties/classifications por chaves
   estáveis (`(ordinal,name)`; `(container,property_name,occurrence_key)`;
   `(system,code)`).
6. **Após a passagem:** emitir `DocumentRef` do acumulador (ordenado por
   `document_id`); calcular `CoverageReport` e a lista **total-ordenada** de
   `ExtractionWarning` (§19.2); escrever `documents.jsonl`, `coverage.json`,
   `warnings.jsonl` no **staging**.
7. **Publicar atomicamente** (§17). **Nunca** manter todos os records em memória.

`convert_*` usa o **mesmo** ordenamento e produz os mesmos bytes que `write_*`.

### 16.3 Ficheiros produzidos
`elements.jsonl`, `property_facts.jsonl`, `classification_facts.jsonl`,
`documents.jsonl`, `coverage.json`, `warnings.jsonl`.

### 16.4 Ficheiros JSONL vazios
`elements/property_facts/classification_facts/documents/warnings.jsonl` sem
records → **ficheiro de zero bytes** (nunca `null`, nunca `[]`, nunca `"\n"`
sozinho). Com records → cada linha termina em `\n` (o ficheiro termina em `\n`).
`coverage.json` é **sempre** escrito: `to_canonical_json(coverage) + "\n"`.

---

## 17. Escrita atómica (por diretório)

`write_canonical_jsonl` publica o conjunto completo numa **única operação de
diretório**; **nunca** deixa output parcial nem mistura gerações.

1. **Validar primeiro** (sem escrever nada): identidade não vazia; schema na
   allowlist (§18); nº de `IfcProject`; `expected_ifc_project_global_id`;
   GlobalIds (duplicado → aborta). Qualquer abort ocorre **antes** de criar o
   staging.
2. **`output_dir` não pode pré-existir.** Se existir → `OutputDirectoryError`,
   sem escrita. **Não há `overwrite`:** uma nova extração nunca substitui nem se
   mistura com uma anterior.
3. **Staging directory irmão** de `output_dir`, no **mesmo filesystem** (para o
   rename ser atómico): `<output_dir>.hbim011.<nonce>.staging`.
4. **Escrever e `fsync`** os **seis** ficheiros (`elements.jsonl`,
   `property_facts.jsonl`, `classification_facts.jsonl`, `documents.jsonl`,
   `coverage.json`, `warnings.jsonl`) dentro do staging.
5. **`fsync` do staging directory** (garante que os nomes estão em disco).
6. **Renomear** o staging para `output_dir` numa **única** operação
   (`os.rename(staging, output_dir)`), atómica no mesmo filesystem.
7. **Em qualquer falha**, remover o staging por completo; erro tipado
   (`JsonlWriteError`/`OutputDirectoryError`) preservando a causa. **Nunca** há
   `output_dir` parcial: ou existe com os seis ficheiros, ou não existe.

**Não** se afirma atomicidade através de seis `os.replace` independentes; a única
publicação é o **rename do diretório**, o que **remove o risco residual de mistura
entre gerações**.

### 17.1 Segurança de paths de output
O `source_path` do IFC **nunca** pode estar dentro de `output_dir` nem do staging,
nem coincidir com eles (comparação por `realpath`). `output_dir` e o staging têm
de partilhar o filesystem (o staging é criado como irmão). Violação →
`OutputDirectoryError`, sem escrita.

---

## 18. Schemas IFC suportados (allowlist)

Allowlist inicial **fechada**: **`IFC2X3`** e **`IFC4`**. Normaliza-se apenas a
**representação técnica** devolvida por IfcOpenShell (`ifc.schema`, ex.: casing) —
**nunca** dados IFC. Qualquer outro schema → `UnsupportedIfcSchemaError`, **sem
output publicado**. Expansões futuras exigem **fixture e teste específicos**.

---

## 19. Erros, warnings e cobertura

### 19.1 Abortam (hierarquia `CanonicalExtractionError`)
`SourceNotFoundError`; `InvalidIfcError`; `UnsupportedIfcSchemaError`;
`EmptyIdentityError`; `MultipleIfcProjectError`; `IfcProjectMismatchError`;
`DuplicateGlobalIdError`; `OutputDirectoryError`; `JsonlWriteError`. Exceções
**inesperadas** (bug) **propagam** com a causa — **sem** `except Exception` amplo
por entidade.

`WarningCode` (fechado): `MISSING_GLOBAL_ID`, `ORPHAN_ELEMENT`,
`INCOMPLETE_SPATIAL_RELATION`, `MATERIAL_WITHOUT_NAME`, `INCOMPLETE_CLASSIFICATION`,
`INCOMPLETE_DOCUMENT`, `DOCUMENT_METADATA_CONFLICT`, `COMPLEX_PROPERTY_VALUE`,
`NON_FINITE_VALUE`, `EMPTY_NORMALIZED_PROPERTY_NAME`, `INVALID_OPTIONAL_FIELD`,
`METRIC_MULTIPLE_CANDIDATES`.

`FieldCode` (fechado) — **códigos estruturais, nunca nomes reais**:
`PROPERTY_VALUE`, `PROPERTY_NAME`, `METRIC_AREA`, `METRIC_VOLUME`, `METRIC_HEIGHT`,
`METRIC_THICKNESS`, `SPATIAL_STOREY`, `SPATIAL_BUILDING`, `SPATIAL_SITE`,
`DOCUMENT_URI`. **Nunca** `container.property` nem nomes IFC internos.

`DetailCode` (fechado): `MISSING_SYSTEM`, `MISSING_CODE`, `MISSING_URI`,
`MISSING_STOREY`, `MISSING_BUILDING`, `MISSING_SITE`, `SPATIAL_CYCLE`,
`VALUE_LIST`, `VALUE_ENUM`, `VALUE_TABLE`, `VALUE_BOUNDED`, `VALUE_REFERENCE`,
`VALUE_UNKNOWN`, `NAN`, `INF`, `TITLE_CONFLICT`, `TYPE_CONFLICT`.

### 19.2 Warnings — ordenação total e agregação
Chave **total** (todos os componentes são enums/códigos fechados ou identificador
opaco):
```
(code, reference or "", ifc_class, (field.value if field else ""), detail_code or "")
```
Warnings com chave idêntica são **agregados** num só (`occurrences` = contagem),
garantindo **ordenação total** (chaves distintas) e determinismo independente da
ordem do IFC.

**Confidencialidade — obrigatório.** Warnings **e** coverage **nunca** contêm
nomes reais de psets, propriedades, documentos ou entidades, nem paths, nomes de
ficheiro, pessoas, organizações, moradas ou valores de propriedade. `code`/
`ifc_class`/`field`/`detail_code` são **enums/códigos fechados**; `field` é o
`FieldCode` (§19.1), **nunca** `container.property` nem nomes IFC internos;
`reference` é um identificador **opaco** (GlobalId ou `document_id` hash), nunca um
nome/URI/path.

### 19.3 Serialização de `CoverageReport`/`ExtractionWarning`
São **modelos Pydantic estritos** (`extra="forbid"`) na camada de ingestão,
serializados pelo **mesmo** `to_canonical_json` (logo `sort_keys=True`,
`ensure_ascii=False`, `allow_nan=False`, separadores compactos). Newline final
**conforme o tipo**: `warnings.jsonl` uma linha por warning agregado (0 bytes se
não houver); `coverage.json` um objeto + `\n`.

`CoverageReport` (só inteiros, categorias e códigos fechados; **nunca** nomes reais
de psets/propriedades/documentos/entidades, paths ou valores): `manifest_version`,
`ifc_schema`, `project_id_present`, `source_id_present`; contagens `elements`/
`spaces`/`property_facts`/`classification_facts`/`documents`; `warnings_by_code`
(todos os códigos, 0 incluídos); `value_categories` (escalares por tipo; não
escalares por estado `planned_atomization`/`unsupported_v1`);
`inherited_type_attributes`, `tag_present`, `metric_multiple_candidates`,
`document_metadata_conflicts`.

---

## 20. Fixtures e testes

### 20.1 Builders sintéticos separados (`backend/tests/fixtures/ifc_builder.py`)
**Não** existe um builder único que misture casos válidos e condições que abortam.
Funções concretas separadas, totalmente sintéticas, em `tmp_path`, com GlobalIds
determinísticos, repetíveis, **sem** copiar `local_data` e **sem** guardar `.ifc`
no repositório:

- `build_valid_ifc4` / `build_valid_ifc2x3` — casos **válidos** (cobrem a matriz
  §20.2). **Os golden files são gerados exclusivamente por estes.**
- `build_missing_global_id_ifc` — prova `MISSING_GLOBAL_ID`/omissão.
- `build_duplicate_global_id_ifc` — **exclusivamente** para provar
  `DuplicateGlobalIdError` (nunca usado para golden).
- `build_multiple_projects_ifc` — prova `MultipleIfcProjectError`.
- `build_project_mismatch_ifc` — prova `IfcProjectMismatchError`.

(Só se adicionam outros builders inválidos estritamente necessários.)

### 20.2 Matriz coberta pelos builders válidos
Project/site/building/storey/space; wall/slab/beam/column/door/proxy/**opening**;
containment em space e direto em storey; parent element; elemento órfão;
materiais simples/layers/constituents; type relation; classification; **document
relation partilhada por ≥2 elementos**; text/int/float/bool/null; Unicode;
unidade; property name com ponto; complexos (list/enum) para cobertura; GlobalIds
case-sensitive; IFC2X3; IFC4.

### 20.3 Golden files (`backend/tests/fixtures/canonical/ifc_extraction/`)
`elements.jsonl`, `property_facts.jsonl`, `classification_facts.jsonl`,
`documents.jsonl`, `warnings.jsonl`, `coverage.json` — **byte-stable**, gerados
pelo **mesmo writer atómico** a partir dos **builders válidos** (nunca de IFC
real). Testes asseram igualdade **byte a byte** com o golden committed;
`warnings.jsonl` com newline final quando há warnings e **0 bytes** quando não há;
ordenação total (§19.2). **Nenhum `.ifc` é committed.**

### 20.4 Ficheiros de teste
`test_canonical_ifc.py` (API, identidade, IfcSpace, ElementRecord, PropertyFact
escalar, complexos→cobertura, **DocumentRef multi-elemento + conflitos
determinísticos**, classifications, metrics+vetores §10, herança de tipo, JSONL
determinístico + vazio 0-bytes, `warnings.jsonl` golden, **escrita atómica por
diretório: `output_dir` não pré-existe; staging→rename único; cleanup do staging;
no-partial; sem mistura de gerações; `output_dir` existente → erro**, round-trip,
ordem-independente, allowlist, warnings/coverage sem nomes reais, erros
abortantes); `test_ifc_spatial.py`; `test_ifc_materials.py`; `test_ifc_values.py`;
`test_canonical_ifc_import_safety.py`.

### 20.5 Validação de `local_data` (fora do CI)
**Não** se cria teste pytest que fique *skipped* por falta dos IFCs privados. A
validação é **manual via CLI**:
```
python -m ingestion.canonical_ifc --source <path> --project-id <id> \
    --source-id <id> --output-dir <dir> [--summary]
```
`output_dir` **não** pode pré-existir. `--summary` imprime **apenas** contagens/
categorias/códigos de warning — **nunca** nomes internos, paths ou conteúdo do IFC.

---

## 21. Compatibilidade legacy

Não se altera `extract_bim.py`/`index_to_opensearch.py`/`search.py`/mappings. A
comparação legacy↔canónico existe só em teste sintético ou ferramenta separada.
**Coincidem:** GlobalIds de **todos os `IfcElement` candidates (incluindo
`IfcOpeningElement`)** e as suas contagens — a comparação legacy cobre **todos os
`IfcElement`** (o extractor legacy itera `by_type("IfcElement")`, que os inclui).
**Diferenças intencionais:** `IfcSpace` só no canónico; spatial hierarchy
**corrigida** (espaço vs storey); `semantic_text` inexistente; propriedades fora
do `ElementRecord`. **Sem** igualdade byte-a-byte entre formatos.

---

## 22. Ficheiros previstos

**Criar:**
`backend/ingestion/canonical_ifc.py`, `ifc_spatial.py`, `ifc_materials.py`,
`ifc_values.py`; `backend/tests/fixtures/ifc_builder.py` (com as funções §20.1);
`backend/tests/test_canonical_ifc.py`, `test_ifc_spatial.py`,
`test_ifc_materials.py`, `test_ifc_values.py`, `test_canonical_ifc_import_safety.py`;
`backend/tests/fixtures/canonical/ifc_extraction/`
`{elements,property_facts,classification_facts,documents,warnings}.jsonl` +
`coverage.json`; esta spec.

**Modificar (na implementação):** `pyproject.toml` (4 módulos → gate mypy);
`.github/workflows/ci.yml` (comando mypy estendido; sem novo job);
`backend/tests/test_import_safety.py` (**só ampliar** cobertura);
`docs/development/LOCAL_SETUP.md`; `docs/implementation/IMPLEMENTATION_STATUS.md`.

**Não tocar:** `extract_bim.py`, `index_to_opensearch.py`, `backend/api`,
`backend/eval`, `frontend`, mappings, `backend/canonical` (salvo **import** da
API pública, justificado), `.gitignore`.

---

## 23. Tooling, CI e baseline

Módulos novos totalmente tipados no **gate mypy bloqueante** (`canonical_ifc`,
`ifc_spatial`, `ifc_materials`, `ifc_values`), removidos por especificidade do
override não-bloqueante `ingestion.*`; `ifcopenshell.*` já com
`ignore_missing_imports`. **Ruff** limpo. Testes sintéticos no job
**`backend-unit`**; **sem novo job CI**. Sem IFC local, sem secret, sem variável
OpenSearch, sem rede, sem dependência ML. **Baseline HBIM-005 byte-identical:**
registar `sha256` de `backend/eval/baselines/current_system.json` antes e depois
(não muda).

---

## 24. Critérios de aceitação

Cada critério mapeia para teste/comando/evidência.

1. **API tipada com identidade obrigatória** — `convert_*`/`write_*` exigem
   `project_id`/`source_id` não vazios; vazio → `EmptyIdentityError`. Sem
   parâmetro `overwrite`, sem iterator público.
2. **IFC2X3 e IFC4** — `build_valid_ifc4`/`build_valid_ifc2x3` convertem com
   records válidos.
3. **Allowlist de schemas** — só `IFC2X3`/`IFC4`; outro → `UnsupportedIfcSchemaError`
   **sem output publicado**.
4. **ElementRecord de IfcElement candidates** — wall/slab/beam/column/door/proxy/
   **opening** (incl. `IfcOpeningElement`) → `ElementRecord`;
   `element_id(project_id, GlobalId)`; GlobalId verbatim.
5. **IfcSpace como ElementRecord sem autorreferência** — space emitido;
   `location.space is None` nele; elementos contidos apontam-lhe.
6. **Dois regimes de containment** — IFC4 (space→storey→…) e IFC2X3 (storey→…).
7. **Parent elements** — partes agregadas com `location.parent_element`.
8. **Identidade e GlobalIds duplicados** — case-sensitive distintos; duplicado →
   `DuplicateGlobalIdError` (só classe+GlobalId), **sem output**.
9. **SourceRef e proveniência** — `source_id`/`ifc_schema`/`checksum`/`external_id`;
   checksum nunca em IDs; `expected_*` divergente e >1 IfcProject → abortam.
10. **Materials** — todos os tipos; ordem determinística; sem nome → omitido+cobertura.
11. **Type inheritance** — `object_type`/`predefined_type` instância→tipo→None;
    herdados contabilizados na cobertura.
12. **Metrics** — candidatos fixos §10; SI; qto→pset; NaN/Inf→None+cobertura;
    múltiplos→flag; **os 4 vetores de teste do §10 passam**.
13. **PropertyFact escalar** — 5 tipos; `fact_id` sem valor; `property_name`
    verbatim; `property_name_norm` = NFC→strip→casefold; unit separado; nome com
    ponto preservado.
14. **Limites HBIM-012** — complexos/list/enum nunca via `str()`; sem facto;
    cobertura (`planned_atomization`/`unsupported_v1`).
15. **Classifications** — IFC4 (`Identification`) e IFC2X3 (`ItemReference`);
    incompleta → cobertura+warning.
16. **DocumentRef multi-elemento** — documento partilhado por ≥2 elementos →
    **um** `DocumentRef` com `linked_element_ids` ordenados+dedup, emitido após a
    passagem (nunca no primeiro elemento).
17. **Conflitos documentais determinísticos** — `title`/`document_type`
    contraditórios → resolução lexicográfica + `DOCUMENT_METADATA_CONFLICT`
    (nunca por ordem de processamento); `document_type` ausente → `"ifc_document"`.
18. **Warnings/coverage sem nomes reais** — `field` é `FieldCode` fechado
    (`PROPERTY_VALUE`/`PROPERTY_NAME`/`METRIC_*`/`SPATIAL_*`/`DOCUMENT_URI`);
    `reference` opaco; nenhum nome real de pset/propriedade/documento/entidade,
    path ou valor; chave total `(code, reference, ifc_class, field, detail_code)`
    agregada com `occurrences`, ordenação total independente da ordem do IFC.
19. **Coverage/warnings serializados** — Pydantic estrito via `to_canonical_json`
    (`sort_keys`/`ensure_ascii=False`/`allow_nan=False`/compacto); `coverage.json`
    sempre; `warnings.jsonl` golden byte-stable (0 bytes quando vazio).
20. **JSONL determinístico + streaming limitado** — ordem `(global_id, ifc_class)`;
    ordem-independente; round-trip; vazios = 0 bytes; `write_*` não materializa
    todos os records.
21. **Escrita atómica por diretório** — `output_dir` **não pode pré-existir**
    (existente → `OutputDirectoryError`); escreve+`fsync` os seis ficheiros num
    **staging irmão**; `fsync` do staging; **rename único** staging→`output_dir`;
    **cleanup do staging em falha**; **nunca output parcial nem mistura de
    gerações**; `output_dir`/staging não coincidem com o IFC.
22. **Builders separados** — válidos (golden) distintos dos inválidos; o de
    GlobalId duplicado só prova o erro.
23. **Fixtures sintéticas** — matriz §20.2; golden byte-stable; nenhum `.ifc`
    committed.
24. **Iterator resolvido** — sem iterator público; `_iter_entity_records` privado;
    warnings/coverage só nas APIs de lifecycle.
25. **Import-safety** — módulos novos sem OpenSearch/FastAPI/settings/`.env`/
    sockets; correm sem env OpenSearch; `canonical` sem ifcopenshell.
26. **Legacy e baseline intactos** — legacy/indexer/retrieval/mappings inalterados;
    comparação legacy cobre **todos os `IfcElement`**; baseline HBIM-005 `sha256`
    idêntico antes/depois.
27. **Ruff/mypy/suite/CI** — Ruff limpo; 4 módulos no gate mypy; suite verde em
    ≥2 ordens; sem novo job CI.
28. **Nenhum IFC real committed** — `git status`/`diff` sem `.ifc`; `local_data/`
    continua ignorado.

---

## 25. Riscos residuais

- **`property_name_norm` lexical** pode colidir nomes que a HBIM-012 distinguirá
  semanticamente — aceitável (estável, documentado).
- **~682 MB não validado ao vivo** — pico de memória é do IfcOpenShell; mitigado
  por `write_*`/CLI; validação manual fora do CI.
- **Deteção de complexos** depende do aplainamento do `get_psets` — cobertura
  pode sub-reportar; testado com casos sintéticos explícitos.
- **Atomicidade por diretório** — o rename único do staging para `output_dir` é
  atómico no mesmo filesystem; **não** há janela de mistura entre gerações (ao
  contrário de múltiplos `os.replace`). Resíduo: staging órfão após crash antes do
  rename, removível com segurança (nunca publicado, prefixo dedicado).
- **Herança de tipo** só resolve escalares; `TypeRecord` completo é posterior.

---

## 26. Decisões finais ratificadas

- IFC `Tag` fica **apenas em cobertura** nesta issue; `Tag` queryável exige
  **schema canónico 1.1 separado**.
- `document_type` usa **`"ifc_document"`** quando a fonte IFC não fornece tipo.
- Múltiplos candidatos de métrica → **prioridade fixa** (§10) + cobertura.
- **GlobalId duplicado aborta** (sem output).
- **IfcSpace é ElementRecord** sem self-reference.
- **`semantic_label` fica `None`**.
- **`property_name_norm` = NFC → strip → casefold** (lexical, permanente).
- **DocumentRef** agregado muitos-para-muitos, emitido após a passagem; conflitos
  resolvidos por ordem lexicográfica + warning (nunca por ordem de processamento).
- **Escrita atómica por diretório**: `output_dir` **não pré-existe**; staging irmão
  + **rename único**; sem output parcial; sem mistura de gerações; **sem
  `overwrite`** na API nem no CLI.
- **Warnings/coverage sem nomes reais**; `field` é `FieldCode` fechado
  (`PROPERTY_VALUE`/`PROPERTY_NAME`/`METRIC_*`/`SPATIAL_*`/`DOCUMENT_URI`).
- **Candidatos = todos os `IfcElement` (incluindo `IfcOpeningElement`) +
  `IfcSpace`**; comparação legacy cobre **todos os `IfcElement`**.
- Allowlist de schemas **fechada** em `IFC2X3`/`IFC4`.
